"""What replies used, for the chat (#49): tokens, cost or plan, models, time.

From the brain-call ledger, so a reopened chat shows the same facts as a live one. A turn is
everything between one user message and the next: an approval and its resume stay one turn.
"""

from collections.abc import Iterable, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel, Field


class _Call(Protocol):
    """A brain call as the router records it, or as the ledger stores it."""

    @property
    def model(self) -> str: ...
    @property
    def started_at(self) -> datetime: ...
    @property
    def latency_ms(self) -> float: ...
    @property
    def outcome(self) -> str: ...
    @property
    def input_tokens(self) -> int: ...
    @property
    def output_tokens(self) -> int: ...
    @property
    def cached_input_tokens(self) -> int: ...
    @property
    def cost_usd(self) -> float | Decimal | None: ...
    @property
    def billing(self) -> str: ...


class UsageOut(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    #: Input tokens the provider read from its cache (cheaper), part of ``input_tokens``.
    cached_input_tokens: int = 0
    #: Brain calls made, including failed ones and fallbacks.
    calls: int = 0
    #: Per-token cost; None when any call has no published price (never shown as 0, §1.3.5).
    cost_usd: float | None = 0.0
    #: Some call was paid by the user's ChatGPT plan: there is no per-token price for it.
    plan: bool = False
    #: The models that answered, in the order they first did.
    models: list[str] = Field(default_factory=list)
    #: Time spent waiting for brains (tools and approvals not included).
    model_ms: float = 0.0
    #: When the first call started: about when the message was sent.
    started_at: datetime | None = None


class ConversationUsageOut(BaseModel):
    total: UsageOut
    #: One per user message, in order.
    turns: list[UsageOut]


def usage_of(calls: Iterable[_Call]) -> UsageOut:
    calls = list(calls)
    usage = UsageOut(calls=len(calls))
    costs: list[float | None] = []
    for c in calls:
        usage.input_tokens += c.input_tokens
        usage.output_tokens += c.output_tokens
        usage.cached_input_tokens += c.cached_input_tokens
        usage.model_ms += c.latency_ms
        if c.billing == "plan":
            usage.plan = True
        else:
            costs.append(None if c.cost_usd is None else float(c.cost_usd))
        if c.outcome == "ok" and c.model not in usage.models:
            usage.models.append(c.model)
    usage.cost_usd = None if any(c is None for c in costs) else sum(c or 0.0 for c in costs)
    usage.started_at = min((c.started_at for c in calls), default=None)
    return usage


def per_turn(saves: Sequence[tuple[datetime, bool]], calls: Sequence[_Call]) -> list[list[_Call]]:
    """Each user message's calls.

    ``saves``: when each saved part of the transcript was written, in order, and whether it
    starts with a user message (a new turn) or continues one (a resume after an approval).
    A call belongs to the first save written after it started.
    """
    turns: list[list[_Call]] = []
    owner: list[int] = []  # save index -> turn index (-1: before any user message)
    for _, starts_turn in saves:
        if starts_turn:
            turns.append([])
        owner.append(len(turns) - 1)
    for call in sorted(calls, key=lambda c: c.started_at):
        save = next((i for i, (at, _) in enumerate(saves) if at >= call.started_at), None)
        turn = owner[save] if save is not None else len(turns) - 1  # still running: the last turn
        if turn >= 0:
            turns[turn].append(call)
    return turns
