"""The agent loop: call the brain, run the tools it asks for, feed results back, repeat (spec §4).

- Every call is validated before it runs; failures go back to the brain as data (§4.2).
- Independent calls from one reply run in parallel; dependent ones wait for the next step.
- Tools that need approval pause the turn until the user decides on the exact call (§12.3).
- The turn always ends with an explicit stop reason, never a silent loop (§4.2 stop conditions).
- The returned history ends in a consistent state, so an interrupted turn resumes where it stopped
  (§6.1): unanswered tool calls at the end are executed first on the next run.
"""

import asyncio
from collections import Counter
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Literal

from enzo.agent.approvals import ApprovalRequest, approval_request
from enzo.agent.history import open_calls
from enzo.brain.config import TaskKind
from enzo.brain.errors import BrainError
from enzo.brain.models import BrainResponse, Message, TextDelta, ToolCall, ToolSpec
from enzo.brain.router import (
    AllBrainsFailedError,
    CallRecord,
    PromptOverBudgetError,
    RoutedDone,
    Router,
)
from enzo.brain.toolcall import ValidatedCall
from enzo.tools.registry import ToolOutcome, ToolRegistry

DEFAULT_MAX_STEPS = 16

type StopReason = Literal[
    "final_answer",
    "approval_pending",
    "max_steps",
    "refusal",
    "truncated",
    "brain_failure",
    "over_budget",
    "cancelled",
]


@dataclass(frozen=True, slots=True)
class ToolStarted:
    call: ToolCall
    #: The step in plain words for the chat, e.g. "Reading plan.md" (#40).
    label: str


@dataclass(frozen=True, slots=True)
class ToolFinished:
    outcome: ToolOutcome
    label: str


@dataclass(frozen=True, slots=True)
class ApprovalNeeded:
    request: ApprovalRequest


@dataclass(frozen=True, slots=True)
class TurnOutcome:
    stop: StopReason
    #: The final answer, or an honest report of why the turn stopped.
    text: str
    #: Full history including this turn: persist it, and pass it back to resume.
    messages: tuple[Message, ...]
    steps: int
    brain_calls: tuple[CallRecord, ...] = ()
    tool_outcomes: tuple[ToolOutcome, ...] = ()
    pending: tuple[ApprovalRequest, ...] = ()
    #: Why a brain_failure happened (e.g. "not_connected"), so each client can word it (#25).
    failure: str | None = None


@dataclass(frozen=True, slots=True)
class TurnDone:
    outcome: TurnOutcome


type AgentEvent = TextDelta | ToolStarted | ToolFinished | ApprovalNeeded | TurnDone


@dataclass
class _Turn:
    messages: list[Message]
    last: BrainResponse | None = None
    steps: int = 0
    brain_calls: list[CallRecord] = field(default_factory=list)
    tool_outcomes: list[ToolOutcome] = field(default_factory=list)

    def done(
        self,
        stop: StopReason,
        text: str,
        pending: Sequence[ApprovalRequest] = (),
        *,
        failure: str | None = None,
    ) -> TurnDone:
        return TurnDone(
            TurnOutcome(
                stop=stop,
                text=text,
                messages=tuple(self.messages),
                steps=self.steps,
                brain_calls=tuple(self.brain_calls),
                tool_outcomes=tuple(self.tool_outcomes),
                pending=tuple(pending),
                failure=failure,
            )
        )


async def run_turn(  # noqa: PLR0913 - keyword-only turn settings
    router: Router,
    registry: ToolRegistry,
    history: Sequence[Message],
    *,
    task: TaskKind = "streaming",
    tools: Sequence[str] | None = None,
    max_steps: int = DEFAULT_MAX_STEPS,
    max_output_tokens: int = 16_000,
    budget_tokens: int | None = None,
    decisions: Mapping[str, bool] | None = None,
    stop: asyncio.Event | None = None,
    override: str | None = None,
    affinity: str | None = None,
) -> AsyncIterator[AgentEvent]:
    """Run one user turn. ``history`` already ends with the user's message (or a paused turn).

    ``task`` is fixed for the whole turn, so the brain never changes mid-task (spec §1.3.3);
    ``affinity`` (the conversation id) keeps the conversation with one provider (ADR 0010).
    """
    turn = _Turn(messages=list(history))
    ask = _Ask(
        router, registry.specs(tools), task, max_output_tokens, budget_tokens, override, affinity
    )
    decided = decisions or {}
    while True:
        unanswered = open_calls(turn.messages)
        if unanswered:
            gates = dict(
                zip(
                    (c.id for c in unanswered),
                    await asyncio.gather(*(_gate(registry, c) for c in unanswered)),
                    strict=True,
                )
            )
            pending = [
                g.request for g in gates.values() if g.request and g.request.id not in decided
            ]
            if pending:
                for request in pending:
                    yield ApprovalNeeded(request)
                names = ", ".join(r.tool for r in pending)
                yield turn.done("approval_pending", f"Waiting for your approval: {names}.", pending)
                return
            async for event in _execute(registry, unanswered, gates, decided, turn):
                yield event
            continue
        if (halt := _halt(turn, stop, max_steps)) is not None:
            yield halt
            return
        async for event in _ask_brain(ask, turn, stop):
            yield event
            if isinstance(event, TurnDone):
                return
        if (finished := _judge_reply(turn, registry)) is not None:
            yield finished
            return


@dataclass(frozen=True, slots=True)
class _Ask:
    router: Router
    specs: list[ToolSpec]
    task: TaskKind
    max_output_tokens: int
    budget_tokens: int | None
    override: str | None
    affinity: str | None


def _halt(turn: _Turn, stop: asyncio.Event | None, max_steps: int) -> TurnDone | None:
    if stop is not None and stop.is_set():
        return turn.done("cancelled", "Stopped at your request.")
    if turn.steps >= max_steps:
        return turn.done("max_steps", _max_steps_report(turn))
    return None


async def _ask_brain(
    ask: _Ask, turn: _Turn, stop: asyncio.Event | None
) -> AsyncIterator[TextDelta | TurnDone]:
    """Stream one brain reply into the turn; a failure ends the turn with an honest report.

    What already streamed is never thrown away: if the reply breaks off (lost connection, Stop),
    the part the user saw is kept in the conversation (#46).
    """
    routed = None
    partial: list[str] = []
    try:
        async with _stoppable(stop):
            async for event in ask.router.stream(
                ask.task,
                turn.messages,
                tools=ask.specs,
                max_output_tokens=ask.max_output_tokens,
                override=ask.override,
                affinity=ask.affinity,
                budget_tokens=ask.budget_tokens,
            ):
                if isinstance(event, RoutedDone):
                    routed = event.routed
                else:
                    partial.append(event.text)
                    yield event
    except _StoppedError:
        _keep_partial(turn, partial)
        yield turn.done("cancelled", "Stopped at your request.")
        return
    except PromptOverBudgetError as exc:
        yield turn.done("over_budget", f"The conversation is too long for this task: {exc}.")
        return
    except (BrainError, AllBrainsFailedError) as exc:
        turn.brain_calls.extend(a for a in exc.attempts if isinstance(a, CallRecord))
        kind = exc.kind if isinstance(exc, BrainError) else "all_brains_failed"
        if _keep_partial(turn, partial):
            text = (
                f"The reply was cut off ({_why(exc)}). "
                "The part above is kept; send again to continue."
            )
        else:
            text = f"I could not get an answer: {exc}"
        yield turn.done("brain_failure", text, failure=kind)
        return
    if routed is None:  # router.stream always ends in RoutedDone or raises; guard regardless
        yield turn.done("brain_failure", "I could not get an answer: no response.")
        return
    turn.steps += 1
    turn.brain_calls.extend(routed.attempts)
    turn.messages.append(routed.response.message)
    turn.last = routed.response


class _StoppedError(Exception):
    """The user pressed Stop while the brain was answering."""


@asynccontextmanager
async def _stoppable(stop: asyncio.Event | None) -> AsyncIterator[None]:
    """Run the body until it ends or ``stop`` is set; then cancel it where it waits.

    Built on ``asyncio.timeout``: the body keeps running in this task (an HTTP stream must be read
    and closed by the task that opened it), and Stop just makes the deadline expire now.
    """
    if stop is None:
        yield
        return
    try:
        async with asyncio.timeout(None) as deadline:
            watcher = asyncio.create_task(_expire_when_set(stop, deadline))
            try:
                yield
            finally:
                watcher.cancel()
    except TimeoutError:  # the expired deadline turns the cancellation into this, on exit
        if stop.is_set():
            raise _StoppedError from None
        raise


async def _expire_when_set(stop: asyncio.Event, deadline: asyncio.Timeout) -> None:
    await stop.wait()
    deadline.reschedule(asyncio.get_running_loop().time())


def _keep_partial(turn: _Turn, partial: Sequence[str]) -> bool:
    """Keep the words that already reached the user as an assistant message. True if any."""
    text = "".join(partial)
    if not text.strip():
        return False
    turn.messages.append(Message.assistant(text))
    return True


def _why(exc: BrainError | AllBrainsFailedError) -> str:
    kind = exc.kind if isinstance(exc, BrainError) else "all_brains_failed"
    return {
        "connection": "lost the connection to the model",
        "timeout": "the model stopped answering",
    }.get(kind, str(exc))


def _judge_reply(turn: _Turn, registry: ToolRegistry) -> TurnDone | None:
    """Decide whether the reply ends the turn. ``None`` means: run its tool calls next."""
    response = turn.last
    if response is None:
        return turn.done("brain_failure", "I could not get an answer: no response.")
    if response.stop_reason == "refusal":
        return turn.done("refusal", response.text or "The model declined this request.")
    if response.stop_reason == "max_tokens":
        # A cut-off tool input can parse as a valid but wrong call: never run it. Answer each
        # call so the history stays consistent and a resume does not run it either.
        if response.tool_calls:
            outcomes = [
                registry.not_executed(c, "not_run", "Not run: the reply was cut off.")
                for c in response.tool_calls
            ]
            turn.messages.append(Message.tool(*(o.result for o in outcomes)))
        return turn.done("truncated", response.text or "The reply was cut off (output limit).")
    if not response.tool_calls:
        return turn.done("final_answer", response.text)
    return None


@dataclass(frozen=True, slots=True)
class _Gate:
    """What a call needs before it runs, and the exact call that will run."""

    #: The approval to ask for; None when the tool needs none (or the call is invalid).
    request: ApprovalRequest | None
    #: The call to execute: the brain's call, or the prepared one the user approved.
    run: ToolCall
    #: Preparing the call failed: it does not run, and the brain is told why.
    error: str | None = None


async def _gate(registry: ToolRegistry, call: ToolCall) -> _Gate:
    """The approval this exact call needs, if any. Invalid calls need none: they will not run.

    A tool with a ``prepare`` step is made exact first (e.g. a merge pinned to the PR's current
    head commit). The approval id covers the prepared arguments, so when the target has changed
    since the user was asked, the id differs and the user is asked again (§12.3).
    """
    tool = registry.get(call.name)
    if tool is None or not tool.requires_approval:
        return _Gate(None, call)
    checked = registry.validate(call)
    if not isinstance(checked, ValidatedCall):
        return _Gate(None, call)
    args, detail = checked.args, None
    if tool.prepare is not None:
        try:
            async with asyncio.timeout(tool.timeout_s):
                prepared = await tool.prepare(args)
        except Exception as exc:  # a lookup failed: nothing is asked and nothing runs
            return _Gate(None, call, error=f"{type(exc).__name__}: {exc}")
        args, detail = prepared.args, prepared.detail
        call = ToolCall(id=call.id, name=call.name, arguments_json=args.model_dump_json())
    summary = tool.summary(args) if tool.summary else None
    return _Gate(approval_request(call, args, summary, detail), call)


async def _execute(
    registry: ToolRegistry,
    calls: Sequence[ToolCall],
    gates: Mapping[str, _Gate],
    decided: Mapping[str, bool],
    turn: _Turn,
) -> AsyncIterator[AgentEvent]:
    runnable: list[ToolCall] = []
    declined: dict[str, ToolOutcome] = {}
    for call in calls:
        gate = gates[call.id]
        if gate.error is not None:
            declined[call.id] = registry.not_executed(
                call, "failed", f"Could not check this action, so it was not run: {gate.error}"
            )
            continue
        if gate.request is not None and not decided[gate.request.id]:
            declined[call.id] = registry.not_executed(
                call, "declined", "The user declined this action. Do not retry it unasked."
            )
            continue
        runnable.append(gate.run)
    for call in runnable:
        yield ToolStarted(call, registry.activity(call))
    ran = {o.call.id: o for o in await registry.execute_all(runnable)}
    outcomes = [ran.get(c.id) or declined[c.id] for c in calls]
    for outcome in outcomes:
        yield ToolFinished(outcome, registry.activity(outcome.call))
    turn.tool_outcomes.extend(outcomes)
    turn.messages.append(Message.tool(*(o.result for o in outcomes)))


def _max_steps_report(turn: _Turn) -> str:
    tried = Counter(f"{o.call.name} ({o.status})" for o in turn.tool_outcomes)
    summary = ", ".join(f"{name} x{n}" for name, n in tried.items()) or "no tools"
    request = next((m.text for m in reversed(turn.messages) if m.role == "user"), "")
    return (
        f"Stopped after {turn.steps} steps without a final answer. "
        f"Tried: {summary}. Still open: {request[:200]}"
    )
