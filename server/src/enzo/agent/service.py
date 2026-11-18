"""Conversations end to end: ingest, compose, run the loop, persist (spec §3 steps 1-7).

A turn runs as a background task that outlives the HTTP request: if the client disconnects, the
agent still finishes and the turn is saved (§6.1, §9.2). Clients read events from a queue.
"""

import asyncio
import logging
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import resources

from enzo.agent.loop import AgentEvent, TurnDone, TurnOutcome, run_turn
from enzo.agent.prompt import compose_prompt
from enzo.brain.models import Message, TextPart
from enzo.brain.router import Router, classify
from enzo.store.repository import ConversationInfo, Store
from enzo.tools.registry import ToolRegistry

log = logging.getLogger("enzo.agent.service")

DEFAULT_SYSTEM_PROMPT = (
    resources.files("enzo.agent").joinpath("system_prompt.md").read_text(encoding="utf-8")
)


class ConversationNotFoundError(Exception):
    pass


class TurnInProgressError(Exception):
    """One turn at a time per conversation, so the transcript stays in order."""


@dataclass(frozen=True, slots=True)
class TurnMeta:
    """Metadata that arrives with a message (spec §3 step 1)."""

    timezone: str | None = None
    device: str | None = None
    received_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class TurnFailed:
    """The turn crashed for a reason the loop could not turn into a stop reason."""

    message: str


type ServiceEvent = AgentEvent | TurnFailed
_END = object()


class ConversationService:
    def __init__(
        self,
        store: Store,
        router: Router,
        registry: ToolRegistry,
        *,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        max_steps: int = 16,
    ) -> None:
        self.store = store
        self.router = router
        self.registry = registry
        self._system = system_prompt
        self._max_steps = max_steps
        self._running: dict[uuid.UUID, asyncio.Task[None]] = {}

    async def require(self, conversation_id: uuid.UUID) -> ConversationInfo:
        info = await self.store.conversation(conversation_id)
        if info is None:
            raise ConversationNotFoundError(str(conversation_id))
        return info

    async def send(
        self, conversation_id: uuid.UUID, text: str, meta: TurnMeta | None = None
    ) -> AsyncGenerator[ServiceEvent]:
        """Start a turn for a new user message; returns its event stream."""
        info = await self._claim(conversation_id)
        history = await self.store.history(conversation_id)
        user = _user_message(text, meta or TurnMeta())
        prompt = compose_prompt(
            system=self._system, developer=None, memory=[], history=history, user_message=user
        )
        return self._start(info, prompt, first_new=len(prompt) - 1)

    async def resume(self, conversation_id: uuid.UUID) -> AsyncGenerator[ServiceEvent]:
        """Continue a paused turn (after an approval decision or an interruption)."""
        info = await self._claim(conversation_id)
        history = await self.store.history(conversation_id)
        prompt = compose_prompt(
            system=self._system, developer=None, memory=[], history=history, user_message=None
        )
        return self._start(info, prompt, first_new=len(prompt))

    async def wait_idle(self, conversation_id: uuid.UUID) -> None:
        task = self._running.get(conversation_id)
        if task is not None:
            await asyncio.shield(task)

    async def _claim(self, conversation_id: uuid.UUID) -> ConversationInfo:
        info = await self.require(conversation_id)
        if conversation_id in self._running:
            raise TurnInProgressError(str(conversation_id))
        return info

    def _start(
        self, info: ConversationInfo, prompt: list[Message], *, first_new: int
    ) -> AsyncGenerator[ServiceEvent]:
        queue: asyncio.Queue[ServiceEvent | object] = asyncio.Queue()
        task = asyncio.create_task(self._drive(info, prompt, first_new, queue))
        self._running[info.id] = task
        task.add_done_callback(lambda _: self._running.pop(info.id, None))
        return _drain(queue)

    async def _drive(
        self,
        info: ConversationInfo,
        prompt: list[Message],
        first_new: int,
        queue: asyncio.Queue[ServiceEvent | object],
    ) -> None:
        try:
            decisions = await self.store.decisions(info.id)
            task, why = classify(self.router.config, prompt)
            log.info("turn task", extra={"conversation": str(info.id), "task": task, "why": why})
            async for event in run_turn(
                self.router,
                self.registry,
                prompt,
                task=task,
                decisions=decisions,
                override=info.brain,
                affinity=str(info.id),
                max_steps=self._max_steps,
            ):
                if isinstance(event, TurnDone):
                    # Persist before telling the client the turn is done.
                    await self._save(info.id, event.outcome, first_new)
                queue.put_nowait(event)
        except Exception as exc:  # report, never leave the client hanging
            queue.put_nowait(TurnFailed(f"{type(exc).__name__}: {exc}"))
            raise
        finally:
            queue.put_nowait(_END)

    async def _save(self, conversation_id: uuid.UUID, outcome: TurnOutcome, first_new: int) -> None:
        await self.store.save_turn(
            conversation_id,
            messages=outcome.messages[first_new:],
            brain_calls=outcome.brain_calls,
            tool_outcomes=outcome.tool_outcomes,
            pending=outcome.pending,
        )


async def _drain(queue: "asyncio.Queue[ServiceEvent | object]") -> AsyncGenerator[ServiceEvent]:
    while (item := await queue.get()) is not _END:
        yield item  # type: ignore[misc]  # everything but the sentinel is a ServiceEvent


def _user_message(text: str, meta: TurnMeta) -> Message:
    """The message plus its context, kept in the user's turn so the cached prefix stays stable."""
    when = meta.received_at.isoformat(timespec="seconds")
    context = (
        f"[sent {when}; timezone: {meta.timezone or 'unknown'}; device: {meta.device or 'unknown'}]"
    )
    return Message(role="user", parts=(TextPart(text=text), TextPart(text=context)))
