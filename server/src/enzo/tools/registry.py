"""Canonical tools and the registry that validates and runs them (spec §4.2, §7.2.1).

A tool is a typed function, not a paragraph in a prompt: name, description, a Pydantic parameter
model (its JSON Schema is what the brain sees) and a Pydantic result model. Every call is validated
before it runs; every failure (bad arguments, unknown tool, exception, timeout, wrong result type)
comes back as an error result the brain can read and fix. A tool failure never crashes the turn.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast, get_type_hints

from pydantic import BaseModel, ConfigDict

from enzo.brain.models import ToolCall, ToolResult, ToolSpec
from enzo.brain.toolcall import ToolCallRejection, ValidatedCall, validate_call
from enzo.tools.fence import fence

DEFAULT_TIMEOUT_S = 60.0

type FailureKind = Literal[
    "invalid_arguments", "unknown_tool", "failed", "timeout", "bad_result", "declined", "not_run"
]

type Handler[P: BaseModel, R: BaseModel] = Callable[[P], Awaitable[R]]


class ToolError(BaseModel):
    """The one error shape every tool returns (spec §7.2.1 "plus error shapes")."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    error: FailureKind
    message: str


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

    def spec(self) -> ToolSpec:
        return ToolSpec.from_model(self.name, self.description, self.params)


def tool[P: BaseModel, R: BaseModel](
    *,
    name: str,
    description: str,
    requires_approval: bool = False,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    untrusted_output: bool = False,
    summary: Callable[[P], str] | None = None,
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


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool[Any, Any]] = ()) -> None:
        self._tools: dict[str, Tool[Any, Any]] = {}
        for t in tools:
            self.register(t)

    def register(self, t: Tool[Any, Any]) -> None:
        if t.name in self._tools:
            msg = f"tool {t.name!r} is already registered"
            raise ValueError(msg)
        t.spec()  # validates name and schema now, not mid-conversation
        self._tools[t.name] = t

    def get(self, name: str) -> Tool[Any, Any] | None:
        return self._tools.get(name)

    def specs(self, names: Sequence[str] | None = None) -> list[ToolSpec]:
        """Specs for the brain; ``names`` loads only the tools a task needs (§3 step 2)."""
        chosen = self._tools if names is None else {n: self._tools[n] for n in names}
        return [t.spec() for t in chosen.values()]

    def validate(self, call: ToolCall) -> ValidatedCall | ToolCallRejection:
        return validate_call(call, {name: t.params for name, t in self._tools.items()})

    async def execute(self, call: ToolCall) -> ToolOutcome:
        """Validate, then run with a timeout. Tool problems become results; cancellation passes."""
        started = time.perf_counter()
        checked = self.validate(call)
        if isinstance(checked, ToolCallRejection):
            kind: FailureKind = "invalid_arguments" if self.get(call.name) else "unknown_tool"
            return self._failed(call, kind, checked.reason, started)
        t = self._tools[call.name]
        try:
            async with asyncio.timeout(t.timeout_s):
                value = await t.handler(checked.args)
        except TimeoutError:
            return self._failed(
                call, "timeout", f"{call.name} timed out after {t.timeout_s}s", started
            )
        except Exception as exc:  # any tool bug becomes data for the brain (§4.2)
            return self._failed(call, "failed", f"{type(exc).__name__}: {exc}", started)
        if not isinstance(value, t.returns):
            got = type(value).__name__
            msg = f"{call.name} returned {got}, expected {t.returns.__name__}"
            return self._failed(call, "bad_result", msg, started)
        content = value.model_dump_json()
        if t.untrusted_output:
            content = fence(content, source=call.name)
        return ToolOutcome(
            call=call,
            result=ToolResult(call_id=call.id, name=call.name, content=content),
            status="ok",
            latency_ms=_since(started),
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
    ) -> ToolOutcome:
        content = ToolError(error=kind, message=message).model_dump_json()
        result = ToolResult(call_id=call.id, name=call.name, content=content, is_error=True)
        return ToolOutcome(call=call, result=result, status=kind, latency_ms=_since(started))


def _since(started: float) -> float:
    return (time.perf_counter() - started) * 1000
