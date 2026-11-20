"""Tables. Migrations are generated from these and reviewed by hand; a test proves they match."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
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
from sqlalchemy.dialects.postgresql import JSONB
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
    __table_args__ = (UniqueConstraint("conversation_id", "seq"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE")
    )
    seq: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(Text)
    body: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created_at()


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
