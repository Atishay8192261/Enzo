"""Tables. Migrations are generated from these and reviewed by hand; a test proves they match."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Stable constraint names, so migrations can always find and drop what they created.
NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Conversation(Base):
    """Main chat, a side chat, or an external chat Enzo takes part in (spec §10.2)."""

    __tablename__ = "conversations"
    __table_args__ = (CheckConstraint("kind IN ('main', 'side', 'external')", name="kind"),)

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    kind: Mapped[str] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    #: The brain the user selected for this conversation (spec §1.3.6); None = routing decides.
    brain: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    #: Moved to the trash by the user (#38): left out of the list, still openable by id.
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MessageRow(Base):
    """The transcript (spec §6.1 working memory, §11.2): canonical messages in order."""

    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("conversation_id", "seq"),
        CheckConstraint("origin IN ('user', 'schedule')", name="origin"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE")
    )
    seq: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(Text)
    body: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created_at()
    #: "schedule": a scheduled task's run started this turn, not the user typing it.
    origin: Mapped[str] = mapped_column(Text, server_default="user")


class BrainCallRow(Base):
    """One brain call (spec §1.3.5, §13): what ran, what it cost, how it ended."""

    __tablename__ = "brain_calls"
    __table_args__ = (Index(None, "conversation_id", "started_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL")
    )
    brain: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    task: Mapped[str] = mapped_column(Text)
    attempt: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    latency_ms: Mapped[float]
    outcome: Mapped[str] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(Integer)
    output_tokens: Mapped[int] = mapped_column(Integer)
    cached_input_tokens: Mapped[int] = mapped_column(Integer)
    cache_write_input_tokens: Mapped[int] = mapped_column(Integer)
    #: NULL means unknown (no published price), never "free" (spec §1.3.5).
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 8))
    error: Mapped[str | None] = mapped_column(Text)
    fallback_from: Mapped[str | None] = mapped_column(Text)
    #: "usage" (per-token, on a key) or "plan" (the user's subscription: no per-token cost).
    billing: Mapped[str] = mapped_column(Text, server_default="usage")
    #: The run (one send or resume) that made this call: the same id as its log lines (#48).
    turn_id: Mapped[str | None] = mapped_column(Text, index=True)


class ToolCallRow(Base):
    """One tool call and its result (spec §13): tool, arguments, what came back, how long."""

    __tablename__ = "tool_calls"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    call_id: Mapped[str] = mapped_column(Text)
    tool: Mapped[str] = mapped_column(Text)
    arguments_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    is_error: Mapped[bool]
    result: Mapped[str] = mapped_column(Text)
    latency_ms: Mapped[float]
    #: When the call started (rows are saved at the end of a turn, #43).
    created_at: Mapped[datetime] = _created_at()
    #: The run that made this call: the same id as its log lines (#48).
    turn_id: Mapped[str | None] = mapped_column(Text, index=True)


class ApprovalRow(Base):
    """The approval record: the only thing that counts as consent (spec §12.2)."""

    __tablename__ = "approvals"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True
    )
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    tool: Mapped[str] = mapped_column(Text)
    arguments: Mapped[dict[str, Any]] = mapped_column(JSONB)
    #: The tool call this approval is for, so an app reopened later shows the open card (#39).
    call_id: Mapped[str | None] = mapped_column(Text)
    #: What the card says: the action in plain words, and what it changes (e.g. a diff).
    summary: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[str | None] = mapped_column(Text)
    #: NULL while waiting; set once, by the user.
    approved: Mapped[bool | None]
    requested_at: Mapped[datetime] = _created_at()
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MemoryRow(Base):
    """One durable fact about the user, with where it came from (spec §6.2, §6.5).

    A correction is a new row that names the one it ``replaces``; the old row is kept, marked
    replaced, and never recalled again. Forgetting deletes the row and the versions before it.
    """

    __tablename__ = "memories"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('about', 'preference', 'person', 'decision', 'fact')", name="kind"
        ),
        CheckConstraint("said_by IN ('user', 'inferred')", name="said_by"),
        CheckConstraint("status IN ('active', 'replaced')", name="status"),
        Index(None, "search", postgresql_using="gin"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    #: Who or what it is about, when not the user: a person's name, a project.
    about: Mapped[str | None] = mapped_column(Text)
    #: "user": they said it. "inferred": Enzo concluded it (labelled as such, §6.5).
    said_by: Mapped[str] = mapped_column(Text)
    #: When it happened, if it is an event and the date is known (§6.5).
    happened_on: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(Text, server_default="active")
    replaces: Mapped[int | None] = mapped_column(
        ForeignKey("memories.id", ondelete="CASCADE"), index=True
    )
    #: Where it was learned: the chat and the run (the same id as its log lines and ledger rows).
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL")
    )
    turn_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    search: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', coalesce(about, '') || ' ' || text)", persisted=True),
    )


class JobRow(Base):
    """Something Enzo does at a time, for the user: for now a reminder (spec §9.2, §9.3).

    It belongs to the user (Enzo has one), is delivered to the chat it was set in, and stops at
    its stop condition: a one-time reminder after it fires, a repeating one after ``max_runs``
    deliveries or past ``ends_on`` (the user's local date), or when the user cancels it.
    """

    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint("kind IN ('reminder', 'task')", name="kind"),
        CheckConstraint(
            "repeat IN ('once', 'hourly', 'daily', 'weekdays', 'weekly', 'monthly')",
            name="repeat",
        ),
        CheckConstraint("status IN ('active', 'done', 'cancelled')", name="status"),
        CheckConstraint("every >= 1", name="every"),
        Index(None, "status", "next_run_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(Text)
    #: A reminder: what to remind the user of. A task: what Enzo does on each run.
    text: Mapped[str] = mapped_column(Text)
    #: A task: when it is done (Enzo checks on each run and stops once it is met).
    stop_when: Mapped[str | None] = mapped_column(Text)
    #: A task's own chat, where its runs happen (the chat it was set in gets the reports).
    chat_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL")
    )
    #: A task run in progress since then: the next is never started on top of it.
    running_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Runs that failed in a row: a task stops after a few, rather than fail on and on.
    failures: Mapped[int] = mapped_column(Integer, server_default="0")
    #: The chat it was set in, where it is delivered (and the run, as on log lines).
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL")
    )
    turn_id: Mapped[str | None] = mapped_column(Text)
    #: The user's zone: repeats keep their wall-clock time across daylight-saving changes.
    timezone: Mapped[str] = mapped_column(Text)
    #: The first time it fires; repeats are counted from it.
    first_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    repeat: Mapped[str] = mapped_column(Text, server_default="once")
    every: Mapped[int] = mapped_column(Integer, server_default="1")
    ends_on: Mapped[date | None] = mapped_column(Date)
    max_runs: Mapped[int | None] = mapped_column(Integer)
    runs: Mapped[int] = mapped_column(Integer, server_default="0")
    #: When it fires next; None once it is done or cancelled.
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text, server_default="active")
    created_at: Mapped[datetime] = _created_at()


class JobRunRow(Base):
    """One delivery: when it was due, when it arrived, and where in the chat it showed."""

    __tablename__ = "job_runs"
    __table_args__ = (
        CheckConstraint(
            "outcome IN ('delivered', 'reported', 'quiet', 'finished', 'needs_approval', 'failed')",
            name="outcome",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    #: The reminder as delivered, kept even if the reminder is later changed.
    text: Mapped[str] = mapped_column(Text)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    #: Delivered well after it was due: Enzo was not running (the computer slept or was off).
    late: Mapped[bool]
    #: How many messages the chat had then: clients show the reminder after that many.
    after: Mapped[int] = mapped_column(Integer)
    #: delivered (a reminder); reported, quiet (nothing new yet), finished (its stop was met),
    #: needs_approval (it waits for the user) or failed (a task run).
    outcome: Mapped[str] = mapped_column(Text, server_default="delivered")
