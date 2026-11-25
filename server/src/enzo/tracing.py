"""`enzo trace`: one task step by step, from the ledgers and the log (spec §13.3, §15.4).

A task is one message and everything Enzo did for it: each run (the send, and each resume after an
approval) with its model calls (provider, model, tokens, time to first word, latency, cost) and
its tool calls (arguments, result size, latency, approval). The ledgers hold the calls; the log
adds what they do not store: the route, the stop reason and the time to first token.

A trace shows tool names and arguments on purpose: it is how the user checks what Enzo did
(§15.4), not a chat reply. Secrets and key shapes are redacted, and control characters dropped,
since arguments come from the brain and results from outside.
"""

import json
import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Protocol

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from enzo.brain.models import Message, TextPart
from enzo.redact import redact
from enzo.store.schema import ApprovalRow, BrainCallRow, Conversation, MessageRow, ToolCallRow

TURN_ID = re.compile(r"^t_[0-9a-f]{12}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
#: How much of an argument or result a trace shows unless asked for all of it.
ARGS_CHARS = 300
RESULT_CHARS = 160

type Approval = Literal["not needed", "approved", "declined", "waiting"]
_DECIDED: dict[bool | None, Approval] = {True: "approved", False: "declined", None: "waiting"}


class TraceNotFoundError(LookupError):
    """Nothing matches: the message says what to try instead."""


class BrainStep(BaseModel):
    run: str | None
    started_at: datetime
    brain: str
    provider: str
    model: str
    attempt: int
    outcome: str
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    latency_ms: float
    #: From the log (or measured live by an eval); None when unknown or the reply had no text.
    first_token_ms: float | None = None
    #: None: unknown (no published price), never 0 (§1.3.5).
    cost_usd: float | None
    billing: str
    fallback_from: str | None = None
    error: str | None = None


class ToolStep(BaseModel):
    run: str | None
    started_at: datetime
    call_id: str
    tool: str
    #: The arguments as the brain sent them (JSON), secrets redacted.
    arguments: str
    status: str
    result_chars: int
    #: The start of the result (all of it with ``full``), on one line.
    result: str
    latency_ms: float
    approval: Approval
    #: What the approval card said, when the call needed one.
    approval_summary: str | None = None


class Waiting(BaseModel):
    """An approval asked for in this task that has not led to a call (yet)."""

    tool: str
    arguments: str
    approved: bool | None
    summary: str | None


class Trace(BaseModel):
    conversation_id: str | None
    title: str | None
    #: The user's words that started the task (a scheduled run's instruction).
    message: str | None
    #: The runs of this task, in order: the send, then each resume.
    runs: list[str]
    route: str | None = None
    stop: str | None = None
    brain_calls: list[BrainStep]
    tool_calls: list[ToolStep]
    waiting: list[Waiting] = []


class _Call(Protocol):
    """A brain call as the router records it (an eval) or as the ledger stores it."""

    @property
    def started_at(self) -> datetime: ...
    @property
    def brain(self) -> str: ...
    @property
    def provider(self) -> str: ...
    @property
    def model(self) -> str: ...
    @property
    def attempt(self) -> int: ...
    @property
    def outcome(self) -> str: ...
    @property
    def input_tokens(self) -> int: ...
    @property
    def output_tokens(self) -> int: ...
    @property
    def cached_input_tokens(self) -> int: ...
    @property
    def latency_ms(self) -> float: ...
    @property
    def cost_usd(self) -> float | Decimal | None: ...
    @property
    def billing(self) -> str: ...
    @property
    def fallback_from(self) -> str | None: ...
    @property
    def error(self) -> str | None: ...


def brain_step(
    call: _Call,
    run: str | None,
    *,
    first_token_ms: float | None = None,
    secrets: Iterable[str] = (),
) -> BrainStep:
    return BrainStep(
        run=run,
        started_at=call.started_at,
        brain=call.brain,
        provider=call.provider,
        model=call.model,
        attempt=call.attempt,
        outcome=call.outcome,
        input_tokens=call.input_tokens,
        output_tokens=call.output_tokens,
        cached_input_tokens=call.cached_input_tokens,
        latency_ms=call.latency_ms,
        first_token_ms=first_token_ms,
        cost_usd=None if call.cost_usd is None else float(call.cost_usd),
        billing=call.billing,
        fallback_from=call.fallback_from,
        error=printable(redact(call.error, secrets)) if call.error else None,
    )


def tool_step(
    row: ToolCallRow,
    approval: ApprovalRow | None,
    *,
    full: bool = False,
    secrets: Iterable[str] = (),
) -> ToolStep:
    secrets = tuple(secrets)
    decided: Approval = "not needed"
    if approval is not None:
        decided = _DECIDED[approval.approved]
    return ToolStep(
        run=row.turn_id,
        started_at=row.created_at,
        call_id=row.call_id,
        tool=row.tool,
        arguments=arguments_text(row.arguments_json, full=full, secrets=secrets),
        status=row.status,
        result_chars=len(row.result),
        result=_cut(printable(redact(row.result, secrets)), None if full else RESULT_CHARS),
        latency_ms=row.latency_ms,
        approval=decided,
        approval_summary=printable(approval.summary) if approval and approval.summary else None,
    )


def arguments_text(raw: str, *, full: bool = False, secrets: Iterable[str] = ()) -> str:
    """Arguments as one line of JSON: printable, secrets redacted, cut unless ``full``."""
    try:
        text = json.dumps(json.loads(raw or "{}"), ensure_ascii=False)
    except ValueError:
        text = raw
    return _cut(printable(redact(text, secrets)), None if full else ARGS_CHARS)


def printable(text: str) -> str:
    """One line, without control characters: what came from outside never drives a terminal."""
    return " ".join(_CONTROL.sub(" ", text).split())


def _cut(text: str, limit: int | None) -> str:
    return text if limit is None or len(text) <= limit else f"{text[: limit - 1]}…"


# ---------- loading a trace from the ledgers ----------


@dataclass(frozen=True, slots=True)
class _Saved:
    """One save of the transcript: messages written together, in one transaction."""

    at: datetime
    starts_turn: bool


async def load(
    engine: AsyncEngine,
    turn: str = "last",
    *,
    log_lines: Iterable[str] = (),
    full: bool = False,
    secrets: Iterable[str] = (),
) -> Trace:
    """The task that run ``turn`` belongs to (``last``: the newest run with a recorded call)."""
    secrets = tuple(secrets)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        anchor = await _anchor(session, turn)
        conversation, run = anchor
        if conversation is None:  # the chat was deleted: only this run's own calls are left
            brains = list(
                await session.scalars(
                    select(BrainCallRow)
                    .where(BrainCallRow.turn_id == run)
                    .order_by(BrainCallRow.started_at, BrainCallRow.id)
                )
            )
            tools = list(
                await session.scalars(
                    select(ToolCallRow).where(ToolCallRow.turn_id == run).order_by(ToolCallRow.id)
                )
            )
            return _assemble(
                conversation=None,
                title=None,
                words=None,
                brains=brains,
                tools=tools,
                approvals={},
                waiting=[],
                log_lines=log_lines,
                full=full,
                secrets=secrets,
            )
        chat = await session.get(Conversation, conversation)
        messages = list(
            await session.execute(
                select(MessageRow.created_at, MessageRow.role, MessageRow.body)
                .where(MessageRow.conversation_id == conversation)
                .order_by(MessageRow.seq)
            )
        )
        all_brains = list(
            await session.scalars(
                select(BrainCallRow)
                .where(BrainCallRow.conversation_id == conversation)
                .order_by(BrainCallRow.started_at, BrainCallRow.id)
            )
        )
        all_tools = list(
            await session.scalars(
                select(ToolCallRow)
                .where(ToolCallRow.conversation_id == conversation)
                .order_by(ToolCallRow.id)
            )
        )
        approvals = list(
            await session.scalars(
                select(ApprovalRow)
                .where(ApprovalRow.conversation_id == conversation)
                .order_by(ApprovalRow.requested_at)
            )
        )
    saves = _saves([(at, role) for at, role, _ in messages])
    owner = _owners(saves)

    def turn_of(started: datetime) -> int:
        return _turn_of(saves, owner, started)

    anchor_rows = [r.started_at for r in all_brains if r.turn_id == run] + [
        r.created_at for r in all_tools if r.turn_id == run
    ]
    task = turn_of(min(anchor_rows))
    brains = [r for r in all_brains if turn_of(r.started_at) == task]
    tools = [r for r in all_tools if turn_of(r.created_at) == task]
    asked = _user_turns(messages)
    words, call_ids = asked[task] if 0 <= task < len(asked) else (None, set())
    by_call = {a.call_id: a for a in approvals if a.call_id}
    ran = {t.call_id for t in tools}
    waiting = [a for a in approvals if a.call_id in call_ids and a.call_id not in ran]
    return _assemble(
        conversation=str(conversation),
        title=chat.title if chat else None,
        words=words,
        brains=brains,
        tools=tools,
        approvals=by_call,
        waiting=waiting,
        log_lines=log_lines,
        full=full,
        secrets=secrets,
    )


async def _anchor(session: AsyncSession, turn: str) -> tuple[uuid.UUID | None, str]:
    if turn == "last":
        brain = (
            await session.execute(
                select(BrainCallRow.conversation_id, BrainCallRow.turn_id, BrainCallRow.started_at)
                .where(BrainCallRow.turn_id.is_not(None))
                .order_by(BrainCallRow.started_at.desc(), BrainCallRow.id.desc())
                .limit(1)
            )
        ).first()
        tool = (
            await session.execute(
                select(ToolCallRow.conversation_id, ToolCallRow.turn_id, ToolCallRow.created_at)
                .where(ToolCallRow.turn_id.is_not(None))
                .order_by(ToolCallRow.created_at.desc(), ToolCallRow.id.desc())
                .limit(1)
            )
        ).first()
        newest = max((r for r in (brain, tool) if r is not None), key=lambda r: r[2], default=None)
        if newest is None:
            msg = "Nothing to trace yet: send Enzo a message first."
            raise TraceNotFoundError(msg)
        return newest[0], str(newest[1])
    if not TURN_ID.match(turn):
        msg = f"{turn!r} is not a turn id: they look like t_9f2c41aa01bc (see `enzo logs`)"
        raise TraceNotFoundError(msg)
    for column, chat in (
        (BrainCallRow.turn_id, BrainCallRow.conversation_id),
        (ToolCallRow.turn_id, ToolCallRow.conversation_id),
    ):
        found = (await session.execute(select(chat).where(column == turn).limit(1))).first()
        if found is not None:
            return found[0], turn
    msg = f"No model or tool call has turn id {turn}: it may have failed before any call."
    raise TraceNotFoundError(msg)


def _saves(messages: Sequence[tuple[datetime, str]]) -> list[_Saved]:
    """A turn's messages share their transaction's time (``enzo.gateway.usage.per_turn``)."""
    saves: list[_Saved] = []
    for at, role in messages:
        if saves and saves[-1].at == at:
            saves[-1] = _Saved(at, saves[-1].starts_turn or role == "user")
        else:
            saves.append(_Saved(at, role == "user"))
    return saves


def _owners(saves: Sequence[_Saved]) -> list[int]:
    """Save index → the user turn it belongs to (-1: before any user message)."""
    owner: list[int] = []
    turns = 0
    for save in saves:
        if save.starts_turn:
            turns += 1
        owner.append(turns - 1)
    return owner


def _turn_of(saves: Sequence[_Saved], owner: Sequence[int], started: datetime) -> int:
    """A call belongs to the first save written after it started; still running: the last."""
    found = next((i for i, s in enumerate(saves) if s.at >= started), None)
    return owner[found] if found is not None else (owner[-1] if owner else 0)


def _user_turns(messages: Sequence[Any]) -> list[tuple[str | None, set[str]]]:
    """Per user turn: the user's own words (first text part) and the tool calls it made."""
    turns: list[tuple[str | None, set[str]]] = []
    for _, role, body in messages:
        message = Message.model_validate(body)
        if role == "user":
            first = message.parts[0] if message.parts else None
            turns.append((first.text if isinstance(first, TextPart) else None, set()))
        elif turns:
            turns[-1][1].update(c.id for c in message.tool_calls)
    return turns


def _assemble(  # noqa: PLR0913 - the parts of one trace
    *,
    conversation: str | None,
    title: str | None,
    words: str | None,
    brains: Sequence[BrainCallRow],
    tools: Sequence[ToolCallRow],
    approvals: Mapping[str, ApprovalRow],
    waiting: Sequence[ApprovalRow],
    log_lines: Iterable[str],
    full: bool,
    secrets: Sequence[str],
) -> Trace:
    starts: dict[str, datetime] = {}
    for at, run in [(b.started_at, b.turn_id) for b in brains] + [
        (t.created_at, t.turn_id) for t in tools
    ]:
        if run is not None and (run not in starts or at < starts[run]):
            starts[run] = at
    runs = sorted(starts, key=lambda r: starts[r])
    logged = _from_log(log_lines, set(runs))
    ttft = logged.first_tokens
    trace = Trace(
        conversation_id=conversation,
        title=printable(title) if title else None,
        message=printable(redact(words, secrets)) if words else None,
        runs=runs,
        route=logged.route,
        stop=logged.stops.get(runs[-1]) if runs else None,
        brain_calls=[
            brain_step(
                b,
                b.turn_id,
                first_token_ms=_next(ttft, (b.turn_id or "", b.brain, b.attempt)),
                secrets=secrets,
            )
            for b in brains
        ],
        tool_calls=[
            tool_step(t, approvals.get(t.call_id), full=full, secrets=secrets) for t in tools
        ],
        waiting=[
            Waiting(
                tool=a.tool,
                arguments=arguments_text(json.dumps(a.arguments), full=full, secrets=secrets),
                approved=a.approved,
                summary=printable(a.summary) if a.summary else None,
            )
            for a in waiting
        ],
    )
    return trace


def _next(
    values: dict[tuple[str, str, int], list[float | None]], key: tuple[str, str, int]
) -> float | None:
    """The next logged value for this call: calls and their log lines come in the same order."""
    queue = values.get(key)
    return queue.pop(0) if queue else None


@dataclass
class _Logged:
    route: str | None
    stops: dict[str, str]
    #: (run, brain, attempt) → time to first token of each such call, in order.
    first_tokens: dict[tuple[str, str, int], list[float | None]]


def _from_log(lines: Iterable[str], runs: set[str]) -> _Logged:
    logged = _Logged(None, {}, {})
    for raw in lines:
        if not any(r in raw for r in runs):  # cheap filter before parsing
            continue
        try:
            line = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(line, dict) or line.get("turn") not in runs:
            continue
        run = str(line["turn"])
        event = line.get("event")
        if event == "route" and logged.route is None:
            logged.route = printable(f"tier {line.get('tier')}: {line.get('reason')}")
        elif event == "turn.end":
            logged.stops[run] = str(line.get("stop"))
        elif event == "brain.call":
            key = (run, str(line.get("brain")), int(line.get("attempt") or 1))
            value = line.get("ttft_ms")
            logged.first_tokens.setdefault(key, []).append(
                float(value) if isinstance(value, int | float) else None
            )
    return logged


# ---------- showing it ----------

_STOPS = {
    "final_answer": "answered",
    "approval_pending": "waiting for approval",
    "max_steps": "stopped at the step limit",
    "refusal": "the model declined",
    "truncated": "cut off (output limit)",
    "brain_failure": "no answer from the model",
    "over_budget": "too long for the budget",
    "cancelled": "stopped by you",
}


def render(trace: Trace) -> str:
    """The trace for a terminal: a header, the model calls, the tool calls, then totals."""
    out: list[str] = []
    chat = f"chat {trace.conversation_id[:8]}" if trace.conversation_id else "a deleted chat"
    title = f" · “{trace.title}”" if trace.title else ""
    out.append(f"Trace of {trace.runs[-1] if trace.runs else '?'}{title} · {chat}")
    if trace.message:
        out.append(f"Asked: “{_cut(trace.message, 100)}”")
    runs = ", ".join(trace.runs) or "none"
    parts = [f"Runs: {runs}"]
    if trace.stop:
        parts.append(f"ended: {_STOPS.get(trace.stop, trace.stop)}")
    out.append(" · ".join(parts))
    if trace.route:
        out.append(f"Route: {trace.route}")
    out.append("")
    out.append(f"Model calls ({len(trace.brain_calls)})")
    for b in trace.brain_calls:
        fallback = f" · fallback from {b.fallback_from}" if b.fallback_from else ""
        out.append(
            f"  {_clock(b.started_at)}  {b.brain} · {b.provider}/{b.model} · {b.outcome}{fallback}"
        )
        cached = f" (cached {b.cached_input_tokens:,})" if b.cached_input_tokens else ""
        out.append(
            f"            in {b.input_tokens:,}{cached} · out {b.output_tokens:,} · "
            f"first token {_seconds(b.first_token_ms)} · took {_seconds(b.latency_ms)} · "
            f"{_cost(b.cost_usd, b.billing)}"
        )
        if b.error:
            out.append(f"            error: {_cut(b.error, 200)}")
    out.append("")
    out.append(f"Tool calls ({len(trace.tool_calls)})")
    for t in trace.tool_calls:
        approval = ""
        if t.approval != "not needed":
            said = f" ({t.approval_summary})" if t.approval_summary else ""
            approval = f" · {t.approval} by you{said}" if t.approval != "waiting" else " · waiting"
        out.append(
            f"  {_clock(t.started_at)}  {t.tool} · {t.status} · {_seconds(t.latency_ms)} · "
            f"result {t.result_chars:,} chars{approval}"
        )
        out.append(f"            arguments {t.arguments}")
        if t.result:
            out.append(f"            result    {t.result}")
    for w in trace.waiting:
        state = {None: "waiting for your approval", True: "approved, not run yet", False: ""}
        if w.approved is False:
            continue  # a declined call shows above, with its "declined" result
        said = f" ({w.summary})" if w.summary else ""
        out.append(f"  {state[w.approved]}: {w.tool}{said}")
        out.append(f"            arguments {w.arguments}")
    out.append("")
    out.append(_totals(trace))
    return "\n".join(out)


def _totals(trace: Trace) -> str:
    calls = trace.brain_calls
    tokens_in = sum(b.input_tokens for b in calls)
    tokens_out = sum(b.output_tokens for b in calls)
    model_ms = sum(b.latency_ms for b in calls)
    priced = [b.cost_usd for b in calls if b.billing != "plan"]
    plan = any(b.billing == "plan" for b in calls)
    if any(c is None for c in priced):
        cost = "unknown (a model has no published price)"
    else:
        cost = f"${sum(c or 0.0 for c in priced):.4f}" if priced else "none"
        if plan:
            cost = "your ChatGPT plan" if not priced else f"{cost} + your ChatGPT plan"
    return (
        f"Total: {len(calls)} model calls · {tokens_in:,} in / {tokens_out:,} out · "
        f"{_seconds(model_ms)} in models · cost: {cost} · {len(trace.tool_calls)} tool calls"
    )


def _clock(at: datetime) -> str:
    return at.astimezone().strftime("%H:%M:%S")


def _seconds(ms: float | None) -> str:
    if ms is None:
        return "—"
    return f"{ms:.0f} ms" if ms < 1000 else f"{ms / 1000:.2f} s"


def _cost(cost: float | None, billing: str) -> str:
    if billing == "plan":
        return "your ChatGPT plan"
    return "cost unknown" if cost is None else f"${cost:.4f}"
