"""Reminders the user set (spec §9.2, §9.3): kept in the database, so they survive restarts.

A reminder is due when its ``next_run_at`` has passed. Claiming it records the delivery and
moves it on in one transaction, so it is delivered once, even by two gateways at once (they
skip each other's locked rows). One that was due while Enzo was not running (the computer slept
or was off) is delivered once, marked late, and its missed repeats are skipped.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from enzo.context import current_conversation, turn_var
from enzo.schedule.recurrence import Repeat, describe, next_after, zone
from enzo.store.schema import JobRow, JobRunRow, MessageRow

log = logging.getLogger("enzo.schedule")

#: Delivered later than this after it was due: it says it is late.
LATE_AFTER = timedelta(minutes=2)


class ReminderRefusedError(Exception):
    """A plain-language reason a reminder was not set or changed, for the brain to relay."""


@dataclass(frozen=True, slots=True)
class Reminder:
    id: int
    text: str
    timezone: str
    first_at: datetime
    repeat: Repeat
    every: int
    ends_on: date | None
    max_runs: int | None
    runs: int
    next_run_at: datetime | None
    status: str
    conversation_id: uuid.UUID | None

    @property
    def schedule(self) -> str:
        """In words, in the user's zone: "every weekday at 09:00"."""
        first = self.first_at.astimezone(zone(self.timezone))
        words = describe(first, self.repeat, self.every)
        if self.ends_on:
            words += f", until {self.ends_on:%-d %b %Y}"
        if self.max_runs and self.repeat != "once":
            words += f", {self.max_runs} times"
        return words

    @property
    def next_words(self) -> str | None:
        """The next time in the user's zone: "Fri 21 Nov 09:00"."""
        if self.next_run_at is None:
            return None
        return f"{self.next_run_at.astimezone(zone(self.timezone)):%a %-d %b %H:%M}"


@dataclass(frozen=True, slots=True)
class Delivery:
    """A reminder that just came due, as recorded."""

    run_id: int
    reminder: Reminder
    text: str
    due_at: datetime
    delivered_at: datetime
    late: bool
    conversation_id: uuid.UUID | None
    #: Where in its chat it shows: after this many messages.
    after: int


class Schedule:
    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def remind(  # noqa: PLR0913 - one reminder's settings, all keyword-only
        self,
        text: str,
        *,
        at: datetime,
        timezone: str,
        repeat: Repeat = "once",
        every: int = 1,
        ends_on: date | None = None,
        max_runs: int | None = None,
        now: datetime | None = None,
    ) -> Reminder:
        """Set a reminder; ``at`` is aware (the tool puts a local time in the user's zone)."""
        now = now or datetime.now(UTC)
        text = " ".join(text.split())
        if not text:
            msg = "A reminder needs words: what should Enzo remind you of?"
            raise ReminderRefusedError(msg)
        local = at.astimezone(zone(timezone))
        if repeat == "weekdays" and local.weekday() >= 5:  # a Saturday or Sunday
            local += timedelta(days=7 - local.weekday())  # the Monday after, same time
        first = next_after(local, repeat, every, now - timedelta(minutes=1))
        if first is None:
            msg = (
                f"That time has passed: it is now {now.astimezone(zone(timezone)):%a %-d %b %H:%M}"
                f" in {timezone}. Pick a time later than that."
            )
            raise ReminderRefusedError(msg)
        if ends_on is not None and first.date() > ends_on:
            msg = f"It would never fire: the first time is after {ends_on:%-d %b %Y}."
            raise ReminderRefusedError(msg)
        async with self._sessions.begin() as session:
            row = JobRow(
                kind="reminder",
                text=text,
                conversation_id=current_conversation(),
                turn_id=turn_var.get(),
                timezone=timezone,
                first_at=local,
                repeat=repeat,
                every=every,
                ends_on=ends_on,
                max_runs=max_runs,
                next_run_at=first,
                status="active",
            )
            session.add(row)
            await session.flush()
            made = _reminder(row)
        log.info("reminder.set", extra={"reminder": made.id, "repeat": repeat, "every": every})
        return made

    async def active(self) -> list[Reminder]:
        """Reminders still to come, soonest first."""
        async with self._sessions() as session:
            rows = await session.scalars(
                select(JobRow)
                .where(JobRow.status == "active")
                .order_by(JobRow.next_run_at, JobRow.id)
            )
            return [_reminder(r) for r in rows]

    async def cancel(self, reminder_id: int) -> Reminder:
        async with self._sessions.begin() as session:
            row = await session.get(JobRow, reminder_id, with_for_update=True)
            if row is None or row.status != "active":
                msg = f"There is no reminder {reminder_id} still to come."
                raise ReminderRefusedError(msg)
            row.status = "cancelled"
            row.next_run_at = None
            gone = _reminder(row)
        log.info("reminder.cancelled", extra={"reminder": reminder_id})
        return gone

    async def next_due(self) -> datetime | None:
        async with self._sessions() as session:
            found: datetime | None = await session.scalar(
                select(func.min(JobRow.next_run_at)).where(JobRow.status == "active")
            )
            return found

    async def claim_due(self, now: datetime | None = None) -> list[Delivery]:
        """Record every reminder that is due and move each on to its next time (or finish it)."""
        now = now or datetime.now(UTC)
        out: list[Delivery] = []
        async with self._sessions.begin() as session:
            rows = await session.scalars(
                select(JobRow)
                .where(JobRow.status == "active", JobRow.next_run_at <= now)
                .order_by(JobRow.next_run_at, JobRow.id)
                .with_for_update(skip_locked=True)
            )
            for row in list(rows):
                due = row.next_run_at
                assert due is not None  # noqa: S101 - the query only finds rows with a time
                after = 0
                if row.conversation_id is not None:
                    after = (
                        await session.scalar(
                            select(func.count())
                            .select_from(MessageRow)
                            .where(MessageRow.conversation_id == row.conversation_id)
                        )
                        or 0
                    )
                run = JobRunRow(
                    job_id=row.id,
                    conversation_id=row.conversation_id,
                    text=row.text,
                    due_at=due,
                    delivered_at=now,
                    late=now - due > LATE_AFTER,
                    after=after,
                )
                session.add(run)
                _advance(row, now)
                await session.flush()
                out.append(
                    Delivery(
                        run_id=run.id,
                        reminder=_reminder(row),
                        text=row.text,
                        due_at=due,
                        delivered_at=now,
                        late=run.late,
                        conversation_id=row.conversation_id,
                        after=after,
                    )
                )
        return out

    async def delivered(self, conversation_id: uuid.UUID) -> list[JobRunRow]:
        """The reminders that arrived in a chat, in order: shown in its timeline."""
        async with self._sessions() as session:
            rows = await session.scalars(
                select(JobRunRow)
                .where(JobRunRow.conversation_id == conversation_id)
                .order_by(JobRunRow.delivered_at, JobRunRow.id)
            )
            return list(rows)


def _advance(row: JobRow, now: datetime) -> None:
    """One more delivery done: the next time, or the end when the stop condition is met."""
    row.runs += 1
    first = row.first_at.astimezone(zone(row.timezone))
    due = row.next_run_at or now
    nxt = next_after(first, row.repeat, row.every, max(now, due))  # type: ignore[arg-type]
    finished = (
        nxt is None
        or (row.max_runs is not None and row.runs >= row.max_runs)
        or (row.ends_on is not None and nxt.astimezone(zone(row.timezone)).date() > row.ends_on)
    )
    if finished:
        row.status, row.next_run_at = "done", None
    else:
        row.next_run_at = nxt


def _reminder(row: JobRow) -> Reminder:
    return Reminder(
        id=row.id,
        text=row.text,
        timezone=row.timezone,
        first_at=row.first_at,
        repeat=row.repeat,  # type: ignore[arg-type]  # the table's check keeps it a Repeat
        every=row.every,
        ends_on=row.ends_on,
        max_runs=row.max_runs,
        runs=row.runs,
        next_run_at=row.next_run_at,
        status=row.status,
        conversation_id=row.conversation_id,
    )
