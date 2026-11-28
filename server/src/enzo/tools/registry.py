"""Canonical tools and the registry that validates and runs them (spec §4.2, §7.2.1).

A tool is a typed function, not a paragraph in a prompt: name, description, a Pydantic parameter
model (its JSON Schema is what the brain sees) and a Pydantic result model. Every call is validated
before it runs; every failure (bad arguments, unknown tool, exception, timeout, wrong result type)
comes back as an error result the brain can read and fix. A tool failure never crashes the turn.
"""

import asyncio
import dataclasses
import json
import logging
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, cast, get_type_hints

import jsonschema
from pydantic import BaseModel, ConfigDict, Field

from enzo.brain.models import ImagePart, ToolCall, ToolResult, ToolSpec
from enzo.brain.toolcall import ToolCallRejection, ValidatedCall, validate_call
from enzo.tools.fence import fence

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 60.0

#: Added to every approval-gated tool's description (#42).
APPROVAL_NOTE = (
    "The user approves the exact call on a card that Enzo shows: call this directly when they "
    "ask for it, and never ask them to confirm in chat first."
)

type FailureKind = Literal[
    "invalid_arguments",
    "unknown_tool",
    "failed",
    "timeout",
    "bad_result",
    "declined",
    "not_run",
    "stopped",
]

type Handler[P: BaseModel, R: BaseModel] = Callable[[P], Awaitable[R]]


class ToolStoppedError(Exception):
    """The user stopped the step while it ran (Esc closed the browser under it): not a failure."""


class ToolError(BaseModel):
    """The one error shape every tool returns (spec §7.2.1 "plus error shapes")."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    error: FailureKind
    message: str


class ImageResult(BaseModel):
    """A result that shows the brain images (a screenshot): they go with the result, after its
    text, and never into its JSON."""

    images: tuple[ImagePart, ...] = Field(default=(), exclude=True)


class JsonArgs(BaseModel):
    """Arguments of a tool defined by a JSON Schema (an MCP tool), checked with ``jsonschema``."""

    model_config = ConfigDict(extra="allow", frozen=True)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.model_extra or {})


#: How text leaves the computer (spec §12.5, ADR 0029): an email or a post, a web form's field,
#: a search query, or a page's address. It decides which private details count.
type Channel = Literal["message", "field", "search", "address"]


@dataclass(frozen=True, slots=True)
class Outgoing:
    """Text a call sends to someone outside the conversation, checked before it goes."""

    text: str
    channel: Channel
    #: For a form field: what it asks for (label, name, autocomplete hint), e.g. "Delivery
    #: address street-address". A detail of that kind typed into it is what the site needs.
    field: str | None = None


@dataclass(frozen=True, slots=True)
class Prepared[P: BaseModel]:
    """A call made exact before it runs or the user is asked (e.g. pinned to a commit)."""

    args: P
    #: Shown on the approval card with the exact arguments, e.g. the diff about to be merged.
    detail: str | None = None
    #: The exact action in plain words, when the tool's ``summary`` cannot know it from the
    #: arguments alone (a browser step names the button the page really shows).
    summary: str | None = None
    #: For a tool that does not always need approval: True when this exact call does (a
    #: browser click that would place an order). A tool with ``requires_approval`` always asks.
    ask: bool = False
    #: Text the call also sends that is not in its arguments (a forwarded email's own text),
    #: checked for private details with the call's ``outbound`` text (ADR 0029).
    sends: tuple[Outgoing, ...] = ()


@dataclass(frozen=True, slots=True)
class Tool[P: BaseModel, R: BaseModel]:
    name: str
    description: str
    params: type[P]
    returns: type[R]
    handler: Handler[P, R]
    #: Irreversible or outward actions wait for the user's approval of the exact call (§12.3).
    requires_approval: bool = False
    timeout_s: float = DEFAULT_TIMEOUT_S
    #: The result carries outside content (web pages, files): fenced as untrusted data (§12.4).
    untrusted_output: bool = False
    #: For approval-gated tools: the exact action in plain words, shown on the approval card
    #: (§10.3: no tool names in user-facing text; §12.3: approve the exact action).
    summary: Callable[[P], str] | None = None
    #: What the chat shows while the call runs, in plain words: "Reading plan.md" (#40).
    activity: Callable[[P], str] | None = None
    #: Look up what the call will really do and pin it before it runs (and before asking, for a
    #: gated tool). The approval covers the prepared arguments, so if the target changes, the
    #: user is asked again (§12.3 "if the content changes after approval, ask again"). A tool
    #: without ``requires_approval`` decides here, per call, whether this one needs a yes.
    prepare: Callable[[P], Awaitable[Prepared[P]]] | None = None
    #: A JSON Schema for the arguments instead of a Pydantic model (tools from MCP servers).
    schema: dict[str, Any] | None = None
    #: Tools that come and go together, e.g. a connector's tools when its account connects.
    group: str | None = None
    #: Its result always stays whole in the context, however long (it reads stored text itself).
    keep_whole: bool = False
    #: What the call sends to a third party (a query, an email's text, a typed field), from its
    #: prepared arguments: checked for private details before it runs (§12.5, ADR 0029).
    outbound: Callable[[P], Sequence[Outgoing]] | None = None
    #: It shows the brain an image: offered only to a brain that can see (ADR 0033).
    needs_vision: bool = False

    def spec(self) -> ToolSpec:
        # The card is the one confirmation: a model that asks in chat first makes the user
        # confirm twice (#42).
        about = (
            f"{self.description} {APPROVAL_NOTE}" if self.requires_approval else self.description
        )
        if self.schema is not None:
            return ToolSpec(name=self.name, description=about, parameters=self.schema)
        return ToolSpec.from_model(self.name, about, self.params)


def tool[P: BaseModel, R: BaseModel](  # noqa: PLR0913 - keyword-only tool options
    *,
    name: str,
    description: str,
    requires_approval: bool = False,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    untrusted_output: bool = False,
    summary: Callable[[P], str] | None = None,
    activity: Callable[[P], str] | None = None,
    prepare: Callable[[P], Awaitable[Prepared[P]]] | None = None,
    outbound: Callable[[P], Sequence[Outgoing]] | None = None,
) -> Callable[[Handler[P, R]], Tool[P, R]]:
    """Declare a tool from an async function; parameter and result models come from its hints."""

    def wrap(fn: Handler[P, R]) -> Tool[P, R]:
        hints = get_type_hints(fn)
        params = [v for k, v in hints.items() if k != "return"]
        result = hints.get("return")
        if len(params) != 1 or not _is_model(params[0]) or not _is_model(result):
            msg = f"{name}: a tool takes one Pydantic model and returns one Pydantic model"
            raise TypeError(msg)
        return Tool(
            name=name,
            description=description,
            params=cast("type[P]", params[0]),
            returns=cast("type[R]", result),
            handler=fn,
            requires_approval=requires_approval,
            timeout_s=timeout_s,
            untrusted_output=untrusted_output,
            summary=summary,
            activity=activity,
            prepare=prepare,
            outbound=outbound,
        )

    return wrap


def _is_model(value: object) -> bool:
    return isinstance(value, type) and issubclass(value, BaseModel)


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    """A finished call: the result the brain will see, plus what observability needs (§13)."""

    call: ToolCall
    result: ToolResult
    status: Literal["ok"] | FailureKind
    latency_ms: float
    #: When the call started, so the ledger keeps the real order and timing of steps (#43).
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class Retainer(Protocol):
    """Keeps a long result out of the context (spec §5.2 selective retention)."""

    def shorten_result(self, content: str, *, source: str, untrusted: bool) -> str | None:
        """The shortened result (start + pointer, fenced as needed), or None if it fits."""
        ...


class ToolRegistry:
    def __init__(
        self, tools: Iterable[Tool[Any, Any]] = (), *, retain: Retainer | None = None
    ) -> None:
        self._tools: dict[str, Tool[Any, Any]] = {}
        #: Long results are stored whole and shortened in the context (ADR 0024).
        self.retain = retain
        for t in tools:
            self.register(t)

    def register(self, t: Tool[Any, Any]) -> None:
        if t.name in self._tools:
            msg = f"tool {t.name!r} is already registered"
            raise ValueError(msg)
        t.spec()  # validates name and schema now, not mid-conversation
        if t.schema is not None:
            jsonschema.validators.validator_for(t.schema).check_schema(t.schema)
        self._tools[t.name] = t

    def replace_group(self, group: str, tools: Iterable[Tool[Any, Any]]) -> None:
        """Swap a group's tools at once (a connector connected, reconnected or disconnected)."""
        incoming = [dataclasses.replace(t, group=group) for t in tools]
        kept = {n: t for n, t in self._tools.items() if t.group != group}
        clash = sorted(t.name for t in incoming if t.name in kept)
        if clash:
            msg = f"{group} tools clash with existing tools: {', '.join(clash)}"
            raise ValueError(msg)
        staged = ToolRegistry(kept.values())
        for t in incoming:
            staged.register(t)
        self._tools = staged._tools

    def get(self, name: str) -> Tool[Any, Any] | None:
        return self._tools.get(name)

    def only(self, names: Iterable[str], *, ask: Iterable[str] = ()) -> "ToolRegistry":
        """A view with only these tools (a workflow step's allowlist, ADR 0032): any other call
        is an unknown tool and never runs. Tools in ``ask`` always wait for the user's approval
        here, even those that normally do not; a tool that asks always still asks. Tools that
        are not registered now (an account was disconnected) are left out."""
        asking = set(ask)
        view = ToolRegistry(retain=self.retain)
        for name in dict.fromkeys(names):
            t = self._tools.get(name)
            if t is None:
                continue
            if name in asking and not t.requires_approval:
                t = dataclasses.replace(t, requires_approval=True, summary=t.summary or t.activity)
            view._tools[name] = t
        return view

    def specs(self, names: Sequence[str] | None = None, *, vision: bool = True) -> list[ToolSpec]:
        """Specs for the brain; ``names`` loads only the tools a task needs (§3 step 2).
        ``vision``: the brain can see images (else tools that show it one are left out)."""
        chosen = self._tools if names is None else {n: self._tools[n] for n in names}
        return [t.spec() for t in chosen.values() if vision or not t.needs_vision]

    def activity(self, call: ToolCall) -> str:
        """The step in plain words for the chat (#40), never the tool's name (§10.3).

        Arguments come from the brain, so the words are made safe to print: one line, no control
        characters (a terminal would run escape sequences), and not too long.
        """
        t = self._tools.get(call.name)
        describe = (t.activity or t.summary) if t else None
        checked = self.validate(call) if describe else None
        if describe is None or not isinstance(checked, ValidatedCall):
            return GENERIC_ACTIVITY
        try:
            words = describe(checked.args)
        except Exception:  # a describer bug must never break the turn
            log.exception("tool activity failed", extra={"tool": call.name})
            return GENERIC_ACTIVITY
        return plain_words(words) or GENERIC_ACTIVITY

    def validate(self, call: ToolCall) -> ValidatedCall | ToolCallRejection:
        t = self._tools.get(call.name)
        if t is not None and t.schema is not None:
            return _validate_schema(call, t.schema)
        return validate_call(call, {name: t.params for name, t in self._tools.items()})

    async def execute(self, call: ToolCall) -> ToolOutcome:
        """Validate, then run with a timeout. Tool problems become results; cancellation passes."""
        started = time.perf_counter()
        began = datetime.now(UTC)
        checked = self.validate(call)
        if isinstance(checked, ToolCallRejection):
            kind: FailureKind = "invalid_arguments" if self.get(call.name) else "unknown_tool"
            return self._failed(call, kind, checked.reason, started, began)
        t = self._tools[call.name]
        try:
            async with asyncio.timeout(t.timeout_s):
                value = await t.handler(checked.args)
        except TimeoutError:
            return self._failed(
                call, "timeout", f"{call.name} timed out after {t.timeout_s}s", started, began
            )
        except ToolStoppedError as exc:
            return self._failed(call, "stopped", str(exc), started, began)
        except Exception as exc:  # any tool bug becomes data for the brain (§4.2)
            return self._failed(call, "failed", f"{type(exc).__name__}: {exc}", started, began)
        if not isinstance(value, t.returns):
            got = type(value).__name__
            msg = f"{call.name} returned {got}, expected {t.returns.__name__}"
            return self._failed(call, "bad_result", msg, started, began)
        content = value.model_dump_json()
        images = value.images if isinstance(value, ImageResult) else ()
        shortened = (
            self.retain.shorten_result(content, source=call.name, untrusted=t.untrusted_output)
            if self.retain is not None and not t.keep_whole
            else None
        )
        if shortened is not None:  # stored whole; the brain gets the start and a pointer
            content = shortened
        elif t.untrusted_output:
            content = fence(content, source=call.name)
        return ToolOutcome(
            call=call,
            result=ToolResult(call_id=call.id, name=call.name, content=content, images=images),
            status="ok",
            latency_ms=_since(started),
            started_at=began,
        )

    async def execute_all(self, calls: Sequence[ToolCall]) -> list[ToolOutcome]:
        """Run independent calls concurrently (§4.2 parallelism); results keep the calls' order."""
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(self.execute(c)) for c in calls]
        return [t.result() for t in tasks]

    @staticmethod
    def not_executed(call: ToolCall, kind: FailureKind, message: str) -> ToolOutcome:
        """Result for a call that was deliberately not run (declined, truncated input)."""
        return ToolRegistry._failed(call, kind, message, time.perf_counter())

    @staticmethod
    def _failed(
        call: ToolCall,
        kind: FailureKind,
        message: str,
        started: float,
        began: datetime | None = None,
    ) -> ToolOutcome:
        content = ToolError(error=kind, message=message).model_dump_json()
        result = ToolResult(call_id=call.id, name=call.name, content=content, is_error=True)
        return ToolOutcome(
            call=call,
            result=result,
            status=kind,
            latency_ms=_since(started),
            started_at=began or datetime.now(UTC),
        )


def _validate_schema(call: ToolCall, schema: dict[str, Any]) -> ValidatedCall | ToolCallRejection:
    """JSON-Schema tools get the same guarantee as typed ones: nothing runs unless it validates."""
    try:
        data = json.loads(call.arguments_json or "{}")
    except json.JSONDecodeError as exc:
        return ToolCallRejection(call, f"Invalid arguments for {call.name}: not JSON ({exc.msg}).")
    if not isinstance(data, dict):
        return ToolCallRejection(call, f"Invalid arguments for {call.name}: expected an object.")
    validator = jsonschema.validators.validator_for(schema)(schema)
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path))
    if errors:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e.absolute_path) or '(root)'}: {e.message}" for e in errors
        )
        return ToolCallRejection(call, f"Invalid arguments for {call.name}: {problems}.")
    return ValidatedCall(call, JsonArgs(**data))


GENERIC_ACTIVITY = "Working on a step"
MAX_ACTIVITY = 90


def plain_words(text: str, limit: int = MAX_ACTIVITY) -> str:
    """One printable line, cut at ``limit`` characters with an ellipsis."""
    line = " ".join("".join(c if c.isprintable() else " " for c in text).split())
    return line if len(line) <= limit else f"{line[: limit - 1].rstrip()}…"


def _since(started: float) -> float:
    return (time.perf_counter() - started) * 1000
