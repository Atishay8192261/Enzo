"""Tables. Migrations are generated from these and reviewed by hand; a test proves they match."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pgvector.sqlalchemy import VECTOR
from sqlalchemy import (
    BigInteger,
    Boolean,
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
        CheckConstraint(
            "origin IN ('user', 'schedule', 'brief', 'handoff', 'watch')", name="origin"
        ),
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
    #: Private details the card says the action includes ("This includes a health detail: …").
    privacy: Mapped[list[str] | None] = mapped_column(JSONB)
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


class DiscretionRuleRow(Base):
    """A discretion rule the user stated (spec §12.5): what Enzo never shares with others.

    Saved only from the user's own words (``quote``), never inferred. ``conversation_id`` set:
    the rule holds in that chat only; NULL: in every chat. Removing a rule deletes it.
    """

    __tablename__ = "discretion_rules"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    #: The rule in plain words: "Don't mention my health to anyone".
    text: Mapped[str] = mapped_column(Text)
    #: The kinds of private detail it covers (``enzo.privacy.RULE_KINDS``), and its own words.
    kinds: Mapped[list[str]] = mapped_column(JSONB)
    words: Mapped[list[str]] = mapped_column(JSONB)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    #: The user's words that stated it, from their message (the evidence, §6.5).
    quote: Mapped[str] = mapped_column(Text)
    #: Where it was said: the chat and the run (the same id as its log lines).
    said_in: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL")
    )
    turn_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


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
            "repeat IN ('once', 'hourly', 'daily', 'weekdays', 'weekly', 'monthly', 'yearly')",
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


class HookRow(Base):
    """A watch: Enzo checks something cheaply, with no model call, and wakes up only when it
    changes (spec §9.4, ADR 0031). It starts off; the user turns it on after a dry run.

    Like a scheduled task it has an owner (the user), a stop condition (``ends_at`` or
    ``max_wakes``) and a delivery destination (the chat it was set in); a change runs in its
    own chat (``chat_id``).
    """

    __tablename__ = "hooks"
    __table_args__ = (
        CheckConstraint("source IN ('page', 'email', 'calendar', 'pull_request')", name="source"),
        CheckConstraint("rule IN ('changes', 'new', 'above', 'below', 'appears')", name="rule"),
        CheckConstraint("status IN ('off', 'on', 'stopped', 'done', 'cancelled')", name="status"),
        CheckConstraint("interval_s >= 60", name="interval"),
        CheckConstraint("ends_at IS NOT NULL OR max_wakes IS NOT NULL", name="stop"),
        Index(None, "status", "next_poll_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    #: What it watches, in the user's words: "the price of the blue kettle".
    what: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    #: Where and how to read it (a page and a selector, an email search, a pull request).
    target: Mapped[dict[str, Any]] = mapped_column(JSONB)
    #: Who answers its checks: a site ("site:shop.example") or a service ("gmail"). A rate
    #: limit stops every watch of that scope (§8.3).
    scope: Mapped[str] = mapped_column(Text, index=True)
    rule: Mapped[str] = mapped_column(Text)
    threshold: Mapped[Decimal | None] = mapped_column(Numeric)
    #: For "appears": the words to look for.
    match: Mapped[str | None] = mapped_column(Text)
    #: What Enzo does when it changes.
    instruction: Mapped[str] = mapped_column(Text)
    interval_s: Mapped[int] = mapped_column(Integer)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    max_wakes: Mapped[int | None] = mapped_column(Integer)
    wakes: Mapped[int] = mapped_column(Integer, server_default="0")
    polls: Mapped[int] = mapped_column(Integer, server_default="0")
    #: Checks that failed in a row: it backs off, then stops after a few.
    failures: Mapped[int] = mapped_column(Integer, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="off")
    #: Why it stopped: rate_limited, auth, failing, ended, max_wakes.
    stop_reason: Mapped[str | None] = mapped_column(Text)
    #: The last reading: what is compared (a digest for long text) and what the user sees.
    value: Mapped[str | None] = mapped_column(Text)
    shown: Mapped[str | None] = mapped_column(Text)
    number: Mapped[Decimal | None] = mapped_column(Numeric)
    matched: Mapped[bool | None]
    value_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: The dry run the user saw before turning it on; turning it on starts from it.
    dry_value: Mapped[str | None] = mapped_column(Text)
    dry_shown: Mapped[str | None] = mapped_column(Text)
    dry_number: Mapped[Decimal | None] = mapped_column(Numeric)
    dry_matched: Mapped[bool | None]
    dry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: A check in progress since then: it is never claimed twice.
    polling_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    timezone: Mapped[str] = mapped_column(Text)
    chat_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL")
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL")
    )
    turn_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class HookEventRow(Base):
    """A change a watch saw (one wake, recorded with the reading that caused it, so it runs once
    even across restarts), or the watch stopping; with how its report went."""

    __tablename__ = "hook_events"
    __table_args__ = (
        CheckConstraint("kind IN ('wake', 'stopped')", name="kind"),
        CheckConstraint("state IN ('pending', 'running', 'done')", name="state"),
        CheckConstraint(
            "outcome IS NULL OR outcome IN ('reported', 'needs_approval', 'failed', 'stopped')",
            name="outcome",
        ),
        Index(None, "state"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    hook_id: Mapped[int] = mapped_column(ForeignKey("hooks.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(Text)
    #: The readings before and after the change, as the user sees them.
    before: Mapped[str | None] = mapped_column(Text)
    after: Mapped[str | None] = mapped_column(Text)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(Text, server_default="pending")
    outcome: Mapped[str | None] = mapped_column(Text)
    #: The report (the first line of the wake's answer), or why the watch stopped.
    text: Mapped[str | None] = mapped_column(Text)
    #: The chat it was set in, where the report shows, after this many of its messages.
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: It was a while after the change: Enzo was not running when it was seen.
    late: Mapped[bool] = mapped_column(server_default="false")


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


class PersonRow(Base):
    """Someone in the user's life, as a record (spec §6.4, ADR 0036): who they are is their name
    and the other names they go by; everything Enzo knows about them is a ``person_facts`` row,
    each with its evidence."""

    __tablename__ = "people"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    #: When the name was last set (written, or corrected): People.md edits compare against it.
    named_at: Mapped[datetime] = _created_at()
    #: Where Enzo first heard of them: the chat and the run ("People.md": added there by hand).
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL")
    )
    turn_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class PersonAliasRow(Base):
    """Another name someone goes by ("Mom", "Pri"): Enzo knows them when it is said."""

    __tablename__ = "person_aliases"
    __table_args__ = (Index(None, "person_id", text("lower(alias)"), unique=True),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id", ondelete="CASCADE"))
    alias: Mapped[str] = mapped_column(Text)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL")
    )
    turn_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class PersonFactRow(Base):
    """One thing Enzo knows about a person, with its evidence (spec §6.5): who said it, when, and
    in which chat. A correction is a new row naming the one it ``replaces``; the old one is kept,
    replaced, and never used again (never two truths). A person has one relation to the user and
    one birthday at a time; a ``tie`` says how they relate to someone else ("Priya is Sam's
    sister": Priya's fact, ``other_id`` Sam, text "sister")."""

    __tablename__ = "person_facts"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('relation', 'birthday', 'likes', 'food', 'contact', 'other', 'tie')",
            name="kind",
        ),
        CheckConstraint("said_by IN ('user', 'inferred')", name="said_by"),
        CheckConstraint("status IN ('active', 'replaced')", name="status"),
        CheckConstraint("(kind = 'tie') = (other_id IS NOT NULL)", name="tie"),
        CheckConstraint(
            "kind <> 'birthday' OR (month BETWEEN 1 AND 12 AND day BETWEEN 1 AND 31)",
            name="birthday",
        ),
        # One current relation to the user and one birthday each; one current tie to each person.
        Index(
            "ix_person_facts_one_current",
            "person_id",
            "kind",
            unique=True,
            postgresql_where=text("status = 'active' AND kind IN ('relation', 'birthday')"),
        ),
        Index(
            "ix_person_facts_one_tie",
            "person_id",
            "other_id",
            unique=True,
            postgresql_where=text("status = 'active' AND kind = 'tie'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    person_id: Mapped[int] = mapped_column(
        ForeignKey("people.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(Text)
    #: The fact in plain words; a birthday's date in words; a tie's relation ("sister").
    text: Mapped[str] = mapped_column(Text)
    #: A tie: the other person.
    other_id: Mapped[int | None] = mapped_column(
        ForeignKey("people.id", ondelete="CASCADE"), index=True
    )
    #: A birthday: its month and day, and the year when known.
    month: Mapped[int | None] = mapped_column(Integer)
    day: Mapped[int | None] = mapped_column(Integer)
    year: Mapped[int | None] = mapped_column(Integer)
    #: "user": they said it. "inferred": Enzo concluded it (labelled as such, §6.5).
    said_by: Mapped[str] = mapped_column(Text)
    #: When it happened, for an event, when known (§6.5).
    happened_on: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(Text, server_default="active")
    replaces: Mapped[int | None] = mapped_column(
        ForeignKey("person_facts.id", ondelete="CASCADE"), index=True
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL")
    )
    turn_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class PersonLinkRow(Base):
    """A goal, reminder or task about someone ("Plan Mom's party"); ``days_before`` marks a
    yearly birthday reminder (it moves when the birthday is corrected)."""

    __tablename__ = "person_links"
    __table_args__ = (
        CheckConstraint("(goal_id IS NULL) <> (job_id IS NULL)", name="one"),
        CheckConstraint("days_before IS NULL OR days_before BETWEEN 0 AND 60", name="days"),
        UniqueConstraint("person_id", "goal_id", name="uq_person_links_goal"),
        UniqueConstraint("person_id", "job_id", name="uq_person_links_job"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    person_id: Mapped[int] = mapped_column(
        ForeignKey("people.id", ondelete="CASCADE"), index=True
    )
    goal_id: Mapped[int | None] = mapped_column(
        ForeignKey("goals.id", ondelete="CASCADE"), index=True
    )
    job_id: Mapped[int | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), index=True
    )
    days_before: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = _created_at()


class TrackedItemRow(Base):
    """Something the user is waiting on or keeping track of (spec §6.4): a thing lent or
    borrowed, money owed either way, a refund or reply they wait for. Its status changes only on
    evidence, kept in ``tracked_events``."""

    __tablename__ = "tracked_items"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('lent', 'borrowed', 'owed_to_me', 'i_owe', 'waiting_for', 'other')",
            name="kind",
        ),
        CheckConstraint("status IN ('open', 'done', 'dropped')", name="status"),
        Index(None, "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    #: Who it is with, when it is someone Enzo knows.
    person_id: Mapped[int | None] = mapped_column(
        ForeignKey("people.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(Text, server_default="open")
    #: The user's local date it is due back or expected by, if any.
    due_on: Mapped[date | None] = mapped_column(Date)
    #: Where its papers live in the user's Enzo folder ("receipts/acme.pdf"), if anywhere.
    path: Mapped[str | None] = mapped_column(Text)
    #: What remains for it to be settled, in words.
    remains: Mapped[str | None] = mapped_column(Text)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL")
    )
    turn_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TrackedEventRow(Base):
    """One change of a tracked item and what it rests on (spec §6.4, §6.5)."""

    __tablename__ = "tracked_events"
    __table_args__ = (
        CheckConstraint("change IN ('created', 'updated', 'done', 'dropped')", name="change"),
        CheckConstraint(
            "basis IS NULL OR basis IN ('user_confirmed', 'verified', 'timeline_ended')",
            name="basis",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    item_id: Mapped[int] = mapped_column(
        ForeignKey("tracked_items.id", ondelete="CASCADE"), index=True
    )
    change: Mapped[str] = mapped_column(Text)
    #: An update: what changed, in words ("due 2026-12-05").
    detail: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[str | None] = mapped_column(Text)
    basis: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(Text)
    happened_on: Mapped[date | None] = mapped_column(Date)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL")
    )
    turn_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class ChunkRow(Base):
    """A piece of what Enzo knows, kept for search by meaning (spec §6.3, ADR 0030).

    Memories, goals, past chats (a turn at a time) and the long texts kept from them are cut into
    chunks; each chunk keeps where it came from, so a hit is checked against its source before
    the brain sees it. ``embedding`` is NULL until the indexer embeds it, and ``model`` says which
    model made it: when the model changes, chunks are embedded again.
    """

    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint("key", "part"),
        CheckConstraint(
            "source IN ('memory', 'goal', 'turn', 'stored', 'person', 'tracked')", name="source"
        ),
        Index(None, "search", postgresql_using="gin"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source: Mapped[str] = mapped_column(Text)
    #: Which source: ``memory:12``, ``goal:3``, ``person:2``, ``tracked:4``, ``turn:<chat>:<seq>``,
    #: ``stored:<chat>:<id>``.
    key: Mapped[str] = mapped_column(Text)
    #: Its place within the source, from 0.
    part: Mapped[int] = mapped_column(Integer)
    memory_id: Mapped[int | None] = mapped_column(
        ForeignKey("memories.id", ondelete="CASCADE"), index=True
    )
    goal_id: Mapped[int | None] = mapped_column(
        ForeignKey("goals.id", ondelete="CASCADE"), index=True
    )
    person_id: Mapped[int | None] = mapped_column(
        ForeignKey("people.id", ondelete="CASCADE"), index=True
    )
    tracked_id: Mapped[int | None] = mapped_column(
        ForeignKey("tracked_items.id", ondelete="CASCADE"), index=True
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    #: A turn: the messages it covers (their ``seq`` in the chat).
    seq_from: Mapped[int | None] = mapped_column(Integer)
    seq_to: Mapped[int | None] = mapped_column(Integer)
    #: A stored text's id (``s_…``) and where in it this chunk starts, in characters.
    stored_id: Mapped[str | None] = mapped_column(Text)
    start: Mapped[int] = mapped_column(Integer, server_default="0")
    text: Mapped[str] = mapped_column(Text)
    #: The text's SHA-256: an unchanged chunk keeps its vector when its source is read again.
    digest: Mapped[str] = mapped_column(Text)
    #: Outside content (a page, a file): shown fenced, never recalled on its own.
    untrusted: Mapped[bool] = mapped_column(Boolean, server_default="false")
    #: When what it says was said or written.
    happened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    model: Mapped[str | None] = mapped_column(Text)
    embedding: Mapped[Any] = mapped_column(VECTOR(), nullable=True)
    created_at: Mapped[datetime] = _created_at()
    search: Mapped[Any] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english', text)", persisted=True)
    )


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
