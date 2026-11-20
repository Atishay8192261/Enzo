"""The store: transcripts, call ledgers and approval records in Postgres (spec §11.2)."""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from enzo.agent.approvals import ApprovalRequest
from enzo.brain.models import Message
from enzo.brain.router import CallRecord
from enzo.store.schema import ApprovalRow, BrainCallRow, Conversation, MessageRow, ToolCallRow
from enzo.tools.registry import ToolOutcome

type ConversationKind = Literal["main", "side", "external"]


class ApprovalError(Exception):
    """An approval decision that is unknown or would overwrite an earlier decision."""


@dataclass(frozen=True, slots=True)
class ConversationInfo:
    id: uuid.UUID
    kind: ConversationKind
    title: str | None
    brain: str | None
    created_at: datetime


class Store:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)

    @classmethod
    def from_url(cls, url: str) -> "Store":
        return cls(create_async_engine(url, pool_pre_ping=True))

    async def close(self) -> None:
        await self.engine.dispose()

    async def create_conversation(
        self, kind: ConversationKind = "main", title: str | None = None
    ) -> uuid.UUID:
        async with self._sessions.begin() as session:
            row = Conversation(kind=kind, title=title)
            session.add(row)
            await session.flush()
            return row.id

    async def conversation(self, conversation_id: uuid.UUID) -> ConversationInfo | None:
        async with self._sessions() as session:
            row = await session.get(Conversation, conversation_id)
        if row is None:
            return None
        kind: ConversationKind = row.kind  # type: ignore[assignment]  # enforced by a CHECK
        return ConversationInfo(row.id, kind, row.title, row.brain, row.created_at)

    async def set_brain(self, conversation_id: uuid.UUID, brain: str | None) -> None:
        """Store the user's brain choice for this conversation (spec §1.3.6)."""
        async with self._sessions.begin() as session:
            result = await session.execute(
                update(Conversation).where(Conversation.id == conversation_id).values(brain=brain)
            )
            if result.rowcount != 1:  # type: ignore[attr-defined]
                msg = f"no conversation {conversation_id}"
                raise KeyError(msg)

    async def history(self, conversation_id: uuid.UUID) -> list[Message]:
        async with self._sessions() as session:
            rows = await session.scalars(
                select(MessageRow.body)
                .where(MessageRow.conversation_id == conversation_id)
                .order_by(MessageRow.seq)
            )
            return [Message.model_validate(body) for body in rows]

    async def save_turn(
        self,
        conversation_id: uuid.UUID,
        *,
        messages: Sequence[Message],
        brain_calls: Sequence[CallRecord] = (),
        tool_outcomes: Sequence[ToolOutcome] = (),
        pending: Sequence[ApprovalRequest] = (),
    ) -> None:
        """Persist one turn atomically: all of it or none of it."""
        async with self._sessions.begin() as session:
            # Lock the conversation so two turns cannot interleave their message numbers.
            locked = await session.scalar(
                select(Conversation.id).where(Conversation.id == conversation_id).with_for_update()
            )
            if locked is None:
                msg = f"no conversation {conversation_id}"
                raise KeyError(msg)
            last = await session.scalar(
                select(func.coalesce(func.max(MessageRow.seq), -1)).where(
                    MessageRow.conversation_id == conversation_id
                )
            )
            start = (last if last is not None else -1) + 1
            session.add_all(
                MessageRow(
                    conversation_id=conversation_id,
                    seq=start + i,
                    role=m.role,
                    body=m.model_dump(mode="json"),
                )
                for i, m in enumerate(messages)
            )
            session.add_all(_brain_call_row(conversation_id, c) for c in brain_calls)
            session.add_all(
                ToolCallRow(
                    conversation_id=conversation_id,
                    call_id=o.call.id,
                    tool=o.call.name,
                    arguments_json=o.call.arguments_json,
                    status=o.status,
                    is_error=o.result.is_error,
                    result=o.result.content,
                    latency_ms=o.latency_ms,
                )
                for o in tool_outcomes
            )
            if pending:
                await session.execute(
                    insert(ApprovalRow)
                    .values(
                        [
                            {
                                "conversation_id": conversation_id,
                                "id": p.id,
                                "tool": p.tool,
                                "arguments": p.arguments,
                                "call_id": p.call.id,
                                "summary": p.summary,
                                "detail": p.detail,
                            }
                            for p in pending
                        ]
                    )
                    .on_conflict_do_nothing()
                )

    async def decide(self, conversation_id: uuid.UUID, approval_id: str, *, approved: bool) -> None:
        """Record the user's decision. A decision is final: it is never overwritten."""
        async with self._sessions.begin() as session:
            row = await session.get(
                ApprovalRow, (conversation_id, approval_id), with_for_update=True
            )
            if row is None:
                msg = f"no approval request {approval_id} in this conversation"
                raise ApprovalError(msg)
            if row.approved is not None:
                msg = f"approval {approval_id} was already decided"
                raise ApprovalError(msg)
            row.approved = approved
            row.decided_at = datetime.now(UTC)

    async def decisions(self, conversation_id: uuid.UUID) -> dict[str, bool]:
        """Only recorded decisions count as consent (spec §12.2)."""
        async with self._sessions() as session:
            rows = await session.execute(
                select(ApprovalRow.id, ApprovalRow.approved).where(
                    ApprovalRow.conversation_id == conversation_id,
                    ApprovalRow.approved.is_not(None),
                )
            )
            return {row.id: bool(row.approved) for row in rows}

    async def pending_approvals(self, conversation_id: uuid.UUID) -> list[ApprovalRow]:
        """Approvals still waiting for the user, oldest first."""
        async with self._sessions() as session:
            rows = await session.scalars(
                select(ApprovalRow)
                .where(
                    ApprovalRow.conversation_id == conversation_id,
                    ApprovalRow.approved.is_(None),
                )
                .order_by(ApprovalRow.requested_at)
            )
            return list(rows)

    async def drop_pending(self, conversation_id: uuid.UUID) -> int:
        """Forget approvals nobody answered: the user moved on, so they are no longer asked.
        Decided ones stay: they are the record of consent (§12.2)."""
        async with self._sessions.begin() as session:
            result = await session.execute(
                delete(ApprovalRow).where(
                    ApprovalRow.conversation_id == conversation_id,
                    ApprovalRow.approved.is_(None),
                )
            )
            return int(getattr(result, "rowcount", 0) or 0)

    async def brain_calls(self, conversation_id: uuid.UUID) -> list[BrainCallRow]:
        async with self._sessions() as session:
            rows = await session.scalars(
                select(BrainCallRow)
                .where(BrainCallRow.conversation_id == conversation_id)
                .order_by(BrainCallRow.started_at, BrainCallRow.id)
            )
            return list(rows)

    async def saves(self, conversation_id: uuid.UUID) -> list[tuple[datetime, bool]]:
        """When each part of the transcript was saved, in order, and whether it holds a user
        message (a new turn) or continues one (a resume after an approval).

        A turn's messages are written in one transaction, so they share its timestamp.
        """
        async with self._sessions() as session:
            rows = await session.execute(
                select(MessageRow.created_at, MessageRow.role)
                .where(MessageRow.conversation_id == conversation_id)
                .order_by(MessageRow.seq)
            )
            saves: list[tuple[datetime, bool]] = []
            for at, role in rows:
                if saves and saves[-1][0] == at:
                    saves[-1] = (at, saves[-1][1] or role == "user")
                else:
                    saves.append((at, role == "user"))
            return saves

    async def tool_calls(self, conversation_id: uuid.UUID) -> list[ToolCallRow]:
        async with self._sessions() as session:
            rows = await session.scalars(
                select(ToolCallRow)
                .where(ToolCallRow.conversation_id == conversation_id)
                .order_by(ToolCallRow.id)
            )
            return list(rows)


def _brain_call_row(conversation_id: uuid.UUID, c: CallRecord) -> BrainCallRow:
    return BrainCallRow(
        conversation_id=conversation_id,
        brain=c.brain,
        provider=c.provider,
        model=c.model,
        task=c.task,
        attempt=c.attempt,
        started_at=c.started_at,
        latency_ms=c.latency_ms,
        outcome=c.outcome,
        input_tokens=c.input_tokens,
        output_tokens=c.output_tokens,
        cached_input_tokens=c.cached_input_tokens,
        cache_write_input_tokens=c.cache_write_input_tokens,
        # Decimal from the float's shortest repr keeps the exact digits we computed.
        cost_usd=None if c.cost_usd is None else Decimal(repr(c.cost_usd)),
        error=c.error,
        fallback_from=c.fallback_from,
        billing=c.billing,
    )
