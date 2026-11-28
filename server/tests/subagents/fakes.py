"""A brain that answers each chat from its own script, so a parent and its helpers can run at the
same time in a test; and a rig with the real service, store, tools and helpers around it."""

import asyncio
import uuid
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict

from enzo.agent.service import ConversationService
from enzo.brain import Message, TextPart, ToolCall
from enzo.brain.fake import ScriptedBrain, ScriptExhaustedError
from enzo.brain.models import BrainResponse, ToolChoice, ToolSpec
from enzo.schedule.bus import Bus, Event
from enzo.store.repository import Store
from enzo.subagents import HelperStore, Subagents, subagent_tools
from enzo.tools import ToolRegistry, tool
from tests.agent import tools as t
from tests.agent.test_loop import make_router


class RoutedBrain(ScriptedBrain):
    """Each request goes to the script whose key is in the chat's first user message."""

    def __init__(self, routes: dict[str, Sequence[Message]]) -> None:
        super().__init__([])
        self.routes = {k: deque(v) for k, v in routes.items()}
        self.asked: dict[str, int] = dict.fromkeys(routes, 0)
        self._holds: dict[tuple[str, int], asyncio.Event] = {}
        #: Every request that got an answer, with the route it went to.
        self.seen: list[tuple[str, tuple[Message, ...]]] = []

    def hold(self, route: str, call: int) -> asyncio.Event:
        """Keep the route's ``call``-th request (from 1) waiting until the event is set."""
        return self._holds.setdefault((route, call), asyncio.Event())

    def route(self, messages: Sequence[Message]) -> str:
        first = next(m for m in messages if m.role == "user")
        words = first.parts[0].text if isinstance(first.parts[0], TextPart) else ""
        for key in self.routes:
            if key in words:
                return key
        msg = f"no route for {words[:80]!r}"
        raise AssertionError(msg)

    def answered(self, route: str) -> list[tuple[Message, ...]]:
        return [m for r, m in self.seen if r == route]

    async def chat(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSpec] = (),
        tool_choice: ToolChoice = "auto",
        max_output_tokens: int,
    ) -> BrainResponse:
        key = self.route(messages)
        self.asked[key] += 1
        held = self._holds.get((key, self.asked[key]))
        if held is not None:
            await held.wait()
        script = self.routes[key]
        if not script:
            msg = f"route {key!r} got request #{self.asked[key]} but its script is empty"
            raise ScriptExhaustedError(msg)
        self.seen.append((key, tuple(messages)))
        self._script.append(script.popleft())
        return await super().chat(
            messages, tools=tools, tool_choice=tool_choice, max_output_tokens=max_output_tokens
        )


class RecordingBus(Bus):
    def __init__(self) -> None:
        super().__init__()
        self.published: list[Event] = []

    def publish(self, event: Event) -> None:
        self.published.append(event)
        super().publish(event)

    def helper(self, status: str) -> list[dict[str, Any]]:
        return [e.data for e in self.published if e.kind == "helper" and e.data["status"] == status]


class NoteArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str


class NoteOut(BaseModel):
    posted: str


posted: list[str] = []


def posting(args: NoteArgs) -> str:
    return f"Post a note: {args.text}"


@tool(
    name="post_note", description="Post a note publicly.", requires_approval=True, summary=posting
)
async def post_note(args: NoteArgs) -> NoteOut:
    posted.append(args.text)
    return NoteOut(posted=args.text)


@dataclass
class Rig:
    store: Store
    brain: RoutedBrain
    service: ConversationService
    helpers: Subagents
    bus: RecordingBus
    notified: list[str] = field(default_factory=list)

    async def until(self, ok: Callable[[], object], limit_s: float = 5.0) -> None:
        """Wait (with real time: background turns must run) until ``ok()`` is truthy."""
        async with asyncio.timeout(limit_s):
            while not ok():  # noqa: ASYNC110 - polls state that has no event to wait on
                await asyncio.sleep(0.01)

    async def status(self, helper_id: int) -> str:
        found = await self.helpers.store.get(helper_id)
        assert found is not None
        return found.status

    async def settled(self, helper_id: int, *statuses: str) -> None:
        async with asyncio.timeout(5.0):
            while await self.status(helper_id) not in statuses:  # noqa: ASYNC110 - a row
                await asyncio.sleep(0.01)

    async def idle(self) -> None:
        """Every background turn ended (helpers' work and reviews)."""
        await self.until(lambda: not self.helpers.busy)


def rig(
    store: Store,
    routes: dict[str, Sequence[Message]],
    *,
    max_working: int = 4,
    steps: int = 30,
    service: ConversationService | None = None,
) -> Rig:
    brain = RoutedBrain(routes)
    svc = service or ConversationService(
        store, make_router(brain), ToolRegistry([*t.registry()._tools.values(), post_note])
    )
    bus = RecordingBus()
    notified: list[str] = []

    async def notify(_title: str, body: str) -> bool:
        notified.append(body)
        return True

    helpers = Subagents(
        svc, HelperStore(store.engine), bus, max_working=max_working, steps=steps, notify=notify
    )
    for x in subagent_tools(helpers):
        svc.registry.register(x)
    posted.clear()
    made = Rig(store, brain, svc, helpers, bus, notified)
    RIGS.append(made)
    return made


#: Rigs of the running test: closed at its end, so no background turn outlives it.
RIGS: list[Rig] = []


async def close_all() -> None:
    while RIGS:
        await RIGS.pop().helpers.close()


def calls(*made: ToolCall) -> Message:
    return Message.assistant(tool_calls=made)


def call(name: str, call_id: str, **args: object) -> ToolCall:
    import json  # noqa: PLC0415

    return ToolCall(id=call_id, name=name, arguments_json=json.dumps(args))


def delegate(
    call_id: str, title: str, task: str, limits: str = "none given", **more: str
) -> Message:
    return calls(call("delegate", call_id, title=title, task=task, limits=limits, **more))


async def drain(stream: object) -> list[object]:
    return [e async for e in stream]  # type: ignore[attr-defined]


def first_user_text(messages: Sequence[Message]) -> str:
    first = next(m for m in messages if m.role == "user")
    return "\n".join(p.text for p in first.parts if isinstance(p, TextPart))


def last_user_text(messages: Sequence[Message]) -> str:
    last = next(m for m in reversed(messages) if m.role == "user")
    return "\n".join(p.text for p in last.parts if isinstance(p, TextPart))


def new_id() -> uuid.UUID:
    return uuid.uuid4()
