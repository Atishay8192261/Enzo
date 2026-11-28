"""Helpers as records (spec §9.5, §9.2): who asked, where each works, how it is doing, its report.

Kept in the database, so a restart knows which helpers were working and picks them up again.
A helper closes once (done, failed, stopped, interrupted): closing is a guarded update, so a stop
and a finish that race each other never both count.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from enzo.store.schema import HelperNoticeRow, HelperRow, MessageRow

type Status = Literal["working", "waiting", "done", "failed", "stopped", "interrupted"]
type Closed = Literal["done", "failed", "stopped", "interrupted"]
type NoticeKind = Literal[
    "started", "waiting", "done", "failed", "stopped", "interrupted", "resumed"
]

#: Still at work: working, or waiting for the user's approval of one of its steps.
OPEN: tuple[Status, ...] = ("working", "waiting")


@dataclass(frozen=True, slots=True)
class Helper:
    id: int
    parent_id: uuid.UUID | None
    root_id: uuid.UUID | None
    chat_id: uuid.UUID | None
    coordinator_id: int | None
    depth: int
    title: str
    brief: str
    request: str | None
    status: Status
    report: str | None
    delivered_at: datetime | None
    restarts: int
    stopped_by: str | None
    created_at: datetime
    finished_at: datetime | None

    @property
    def open(self) -> bool:
        return self.status in OPEN


@dataclass(frozen=True, slots=True)
class Notice:
    """A line about a helper in the user's chat, where it arrived."""

    helper_id: int
    kind: NoticeKind
    text: str
    after: int
    created_at: datetime
    chat_id: uuid.UUID | None


class HelperStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def add(  # noqa: PLR0913 - one helper's parts, keyword-only
        self,
        *,
        parent_id: uuid.UUID,
        root_id: uuid.UUID,
        chat_id: uuid.UUID,
        coordinator_id: int | None,
        depth: int,
        title: str,
        brief: str,
        request: str | None,
        turn_id: str | None,
    ) -> Helper:
        async with self._sessions.begin() as session:
            row = HelperRow(
                parent_id=parent_id,
                root_id=root_id,
                chat_id=chat_id,
                coordinator_id=coordinator_id,
                depth=depth,
                title=title,
                brief=brief,
                request=request,
                turn_id=turn_id,
                status="working",
            )
            session.add(row)
            await session.flush()
            await session.refresh(row)
            return _helper(row)

    async def get(self, helper_id: int) -> Helper | None:
        async with self._sessions() as session:
            row = await session.get(HelperRow, helper_id)
            return _helper(row) if row is not None else None

    async def by_chat(self, chat_id: uuid.UUID) -> Helper | None:
        """The helper that works in this chat, if it is a helper's chat."""
        async with self._sessions() as session:
            row = await session.scalar(select(HelperRow).where(HelperRow.chat_id == chat_id))
            return _helper(row) if row is not None else None

    async def open(self) -> list[Helper]:
        """Every helper still at work, oldest first."""
        return await self._where(HelperRow.status.in_(OPEN))

    async def working(self) -> int:
        """Helpers working right now (not those waiting for the user)."""
        async with self._sessions() as session:
            found = await session.scalar(
                select(func.count()).select_from(HelperRow).where(HelperRow.status == "working")
            )
            return found or 0

    async def open_children(self, helper_id: int) -> list[Helper]:
        """The helpers this one handed work to that are still at work."""
        return await self._where(HelperRow.coordinator_id == helper_id, HelperRow.status.in_(OPEN))

    async def open_for(self, parent_id: uuid.UUID) -> list[Helper]:
        """Helpers this chat handed work to that are still at work."""
        return await self._where(HelperRow.parent_id == parent_id, HelperRow.status.in_(OPEN))

    async def undelivered(self, parent_id: uuid.UUID) -> list[Helper]:
        """Closed helpers of this chat whose report its brain has not taken in yet."""
        return await self._where(
            HelperRow.parent_id == parent_id,
            HelperRow.status.not_in(OPEN),
            HelperRow.delivered_at.is_(None),
        )

    async def recent(self, limit: int = 30) -> list[Helper]:
        """For the list: helpers at work first (oldest first), then the latest closed ones."""
        at_work = await self.open()
        async with self._sessions() as session:
            rows = await session.scalars(
                select(HelperRow)
                .where(HelperRow.status.not_in(OPEN))
                .order_by(HelperRow.finished_at.desc().nulls_last(), HelperRow.id.desc())
                .limit(max(0, limit - len(at_work)))
            )
            return at_work + [_helper(r) for r in rows]

    async def set_status(self, helper_id: int, status: Literal["working", "waiting"]) -> bool:
        """Between working and waiting for the user; False when it is closed (or unchanged)."""
        async with self._sessions.begin() as session:
            result = await session.execute(
                update(HelperRow)
                .where(
                    HelperRow.id == helper_id,
                    HelperRow.status.in_(OPEN),
                    HelperRow.status != status,
                )
                .values(status=status)
            )
            return bool(getattr(result, "rowcount", 0))

    async def close(
        self,
        helper_id: int,
        status: Closed,
        *,
        report: str | None,
        stopped_by: str | None = None,
        now: datetime | None = None,
    ) -> Helper | None:
        """Close it once: None when it was closed already (a stop and a finish raced)."""
        async with self._sessions.begin() as session:
            row = await session.scalar(
                update(HelperRow)
                .where(HelperRow.id == helper_id, HelperRow.status.in_(OPEN))
                .values(
                    status=status,
                    report=report,
                    stopped_by=stopped_by,
                    finished_at=now or datetime.now(UTC),
                )
                .returning(HelperRow)
            )
            return _helper(row) if row is not None else None

    async def delivered(self, helper_ids: list[int], now: datetime | None = None) -> None:
        if not helper_ids:
            return
        async with self._sessions.begin() as session:
            await session.execute(
                update(HelperRow)
                .where(HelperRow.id.in_(helper_ids), HelperRow.delivered_at.is_(None))
                .values(delivered_at=now or datetime.now(UTC))
            )

    async def restarted(self, helper_id: int) -> None:
        async with self._sessions.begin() as session:
            await session.execute(
                update(HelperRow)
                .where(HelperRow.id == helper_id)
                .values(restarts=HelperRow.restarts + 1)
            )

    async def notice(self, helper: Helper, kind: NoticeKind, text: str) -> Notice | None:
        """Put a line about ``helper`` in the user's chat, after its messages so far."""
        chat = helper.root_id
        if chat is None:  # the user's chat was deleted: nowhere to show it
            return None
        async with self._sessions.begin() as session:
            after = await session.scalar(
                select(func.count())
                .select_from(MessageRow)
                .where(MessageRow.conversation_id == chat)
            )
            row = HelperNoticeRow(
                helper_id=helper.id, conversation_id=chat, kind=kind, text=text, after=after or 0
            )
            session.add(row)
            await session.flush()
            await session.refresh(row)
            return Notice(helper.id, kind, text, row.after, row.created_at, helper.chat_id)

    async def notices(self, conversation_id: uuid.UUID) -> list[Notice]:
        """The lines about helpers in a chat, in order."""
        async with self._sessions() as session:
            rows = await session.execute(
                select(HelperNoticeRow, HelperRow.chat_id)
                .join(HelperRow, HelperRow.id == HelperNoticeRow.helper_id)
                .where(HelperNoticeRow.conversation_id == conversation_id)
                .order_by(HelperNoticeRow.id)
            )
            return [
                Notice(
                    n.helper_id,
                    n.kind,  # type: ignore[arg-type]  # the table's check keeps it a NoticeKind
                    n.text,
                    n.after,
                    n.created_at,
                    chat,
                )
                for n, chat in rows
            ]

    async def _where(self, *conditions: object) -> list[Helper]:
        async with self._sessions() as session:
            rows = await session.scalars(
                select(HelperRow).where(*conditions).order_by(HelperRow.id)  # type: ignore[arg-type]
            )
            return [_helper(r) for r in rows]


def _helper(row: HelperRow) -> Helper:
    return Helper(
        id=row.id,
        parent_id=row.parent_id,
        root_id=row.root_id,
        chat_id=row.chat_id,
        coordinator_id=row.coordinator_id,
        depth=row.depth,
        title=row.title,
        brief=row.brief,
        request=row.request,
        status=row.status,  # type: ignore[arg-type]  # the table's check keeps it a Status
        report=row.report,
        delivered_at=row.delivered_at,
        restarts=row.restarts,
        stopped_by=row.stopped_by,
        created_at=row.created_at,
        finished_at=row.finished_at,
    )
