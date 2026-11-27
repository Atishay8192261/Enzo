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
        CheckConstraint("origin IN ('user', 'schedule', 'brief', 'handoff')", name="origin"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE")
    )
    seq: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(Text)
    body: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created_at()
    #: Who wrote it, when not the user: "schedule" (a scheduled task's run), "brief" (the work
    #: a helper was given, in its own chat), "handoff" (helpers' reports, ADR 0028).
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


class CompactionRow(Base):
    """A summary of a chat's older turns (spec §5.2): what the brain reads instead of them.

    The transcript is never changed; the newest row says which turns the prompt replaces with its
    summary, and which kept messages predate it (their provider replay data is left out).
    """

    __tablename__ = "compactions"
    __table_args__ = (
        CheckConstraint("turns >= 1 AND made_turn >= turns AND made_offset >= 0", name="span"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    #: Turns [0, turns) are summarized.
    turns: Mapped[int] = mapped_column(Integer)
    #: Made in turn ``made_turn``, after its first ``made_offset`` messages.
    made_turn: Mapped[int] = mapped_column(Integer)
    made_offset: Mapped[int] = mapped_column(Integer)
    #: The structured summary (``enzo.compaction.Summary``).
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB)
    #: The brain that wrote it, and the prompt's size before and after, in tokens.
    brain: Mapped[str] = mapped_column(Text)
    tokens_before: Mapped[int] = mapped_column(Integer)
    tokens_after: Mapped[int] = mapped_column(Integer)
    #: The run that made it (the same id as its log lines and ledger rows).
    turn_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


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
    #: The goal it checks in on: closing the goal cancels it (spec §6.4 "stops reminding").
    goal_id: Mapped[int | None] = mapped_column(
        ForeignKey("goals.id", ondelete="SET NULL"), index=True
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


class GoalRow(Base):
    """Something the user is working toward, as a record, not free text (spec §6.4).

    Its status changes only on evidence, kept in ``goal_events``: open until the user confirms it
    is done (or given up), a check verifies the outcome, or its time is up.
    """

    __tablename__ = "goals"
    __table_args__ = (
        CheckConstraint("status IN ('open', 'done', 'dropped')", name="status"),
        Index(None, "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="open")
    #: The user's local date it should be done by, if they gave one.
    due_on: Mapped[date | None] = mapped_column(Date)
    #: Where its work lives in the user's Enzo folder ("passport/", "trip.md"), if anywhere.
    path: Mapped[str | None] = mapped_column(Text)
    #: Where it was set: the chat and the run (the same id as its log lines).
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL")
    )
    turn_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GoalStepRow(Base):
    """What remains of a goal, one step at a time, in order; done once its evidence is in."""

    __tablename__ = "goal_steps"
    __table_args__ = (UniqueConstraint("goal_id", "position"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    goal_id: Mapped[int] = mapped_column(ForeignKey("goals.id", ondelete="CASCADE"))
    #: Its number within the goal, from 1.
    position: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GoalEventRow(Base):
    """One change of a goal and the evidence for it (spec §6.4, §6.5): what shows it, how that
    is known (the user confirmed it, a check verified it, or its time ran out), where the
    evidence can be found again, and when it happened."""

    __tablename__ = "goal_events"
    __table_args__ = (
        CheckConstraint(
            "change IN ('created', 'steps_added', 'step_done', 'done', 'dropped')", name="change"
        ),
        CheckConstraint(
            "basis IS NULL OR basis IN ('user_confirmed', 'verified', 'timeline_ended')",
            name="basis",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    goal_id: Mapped[int] = mapped_column(ForeignKey("goals.id", ondelete="CASCADE"), index=True)
    change: Mapped[str] = mapped_column(Text)
    step_id: Mapped[int | None] = mapped_column(ForeignKey("goal_steps.id", ondelete="CASCADE"))
    evidence: Mapped[str | None] = mapped_column(Text)
    basis: Mapped[str | None] = mapped_column(Text)
    #: Where the evidence can be found again: a page, a file, a pull request, a chat.
    source: Mapped[str | None] = mapped_column(Text)
    #: When it happened, when known; else it happened by the time it was recorded.
    happened_on: Mapped[date | None] = mapped_column(Date)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL")
    )
    turn_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class HelperRow(Base):
    """A helper: a child agent doing work it was handed, in its own chat (spec §9.5, ADR 0028).

    Its report goes to the chat that asked (``parent_id``); its notices show in the user's own
    chat (``root_id``). A helper of a helper (depth 2) cannot hand work on.
    """

    __tablename__ = "helpers"
    __table_args__ = (
        CheckConstraint("depth IN (1, 2)", name="depth"),
        CheckConstraint(
            "status IN ('working', 'waiting', 'done', 'failed', 'stopped', 'interrupted')",
            name="status",
        ),
        CheckConstraint("restarts >= 0", name="restarts"),
        Index(None, "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    #: The chat that handed it the work: its report goes there.
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"), index=True
    )
    #: The user's own chat at the top: where its notices show.
    root_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"), index=True
    )
    #: Its own chat, where it works.
    chat_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"), unique=True
    )
    #: The helper that handed it the work (depth 2), if any.
    coordinator_id: Mapped[int | None] = mapped_column(
        ForeignKey("helpers.id", ondelete="SET NULL"), index=True
    )
    depth: Mapped[int] = mapped_column(Integer)
    #: A short name the user sees: "Compare 5 laptops".
    title: Mapped[str] = mapped_column(Text)
    #: The message it was given: the task, the user's limits and what it needs to know.
    brief: Mapped[str] = mapped_column(Text)
    #: The user's own words that started the work, passed down to every helper below.
    request: Mapped[str | None] = mapped_column(Text)
    #: working, waiting (for the user's approval), or how it ended.
    status: Mapped[str] = mapped_column(Text, server_default="working")
    #: Its final answer, or why it did not finish: what the chat that asked reviews.
    report: Mapped[str | None] = mapped_column(Text)
    #: When the chat that asked took its report in (its brain saw it in a turn that ended).
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Times it picked up again after Enzo stopped while it worked.
    restarts: Mapped[int] = mapped_column(Integer, server_default="0")
    #: Who stopped it: "user", "parent" (the chat that asked) or "coordinator" (with its own).
    stopped_by: Mapped[str | None] = mapped_column(Text)
    #: The run that handed it the work (the same id as its log lines).
    turn_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class HelperNoticeRow(Base):
    """A line about a helper in the user's chat: it waits for approval, finished, was stopped…"""

    __tablename__ = "helper_notices"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('started', 'waiting', 'done', 'failed', 'stopped', 'interrupted', 'resumed')",
            name="kind",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    helper_id: Mapped[int] = mapped_column(ForeignKey("helpers.id", ondelete="CASCADE"), index=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(Text)
    #: In plain words, without tool names: "Helper #3 “Compare 5 laptops” finished."
    text: Mapped[str] = mapped_column(Text)
    #: Shown after this many of the chat's messages.
    after: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = _created_at()
