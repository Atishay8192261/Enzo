"""What the user scheduled (spec §9.2, §9.3), kept in the database so it survives restarts:
reminders (words to tell them) and tasks (something Enzo does on each run, then reports).

A job is due when its ``next_run_at`` has passed. Claiming it moves it on in one transaction, so
it runs once, even with two gateways (they skip each other's locked rows). One due while Enzo was
not running (the computer slept or was off) runs once, marked late; missed repeats are skipped.
A reminder's delivery is recorded as it is claimed. A task is marked running when claimed and its
run is recorded when it ends; the next run never starts on top of one still going.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from enzo.context import current_conversation, turn_var
from enzo.memory.guard import secret_reason
from enzo.schedule.recurrence import Repeat, describe, next_after, zone
from enzo.store.schema import JobRow, JobRunRow, MessageRow

log = logging.getLogger("enzo.schedule")

#: Delivered later than this after it was due: it says it is late.
LATE_AFTER = timedelta(minutes=2)
#: A task whose runs failed this many times in a row is stopped, not left to fail on.
MAX_FAILURES = 3

type Kind = Literal["reminder", "task"]
type Outcome = Literal["reported", "quiet", "finished", "needs_approval", "failed"]


class ScheduleRefusedError(Exception):
    """A plain-language reason a reminder or task was not set or changed, for the brain."""


@dataclass(frozen=True, slots=True)
class Job:
    id: int
    kind: Kind
    #: A reminder's words, or what a task does on each run.
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
    #: A task: when it is done, checked on each run.
    stop_when: str | None = None
    #: A task's own chat, where its runs happen.
    chat_id: uuid.UUID | None = None
    created_at: datetime | None = None

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
    reminder: Job
    text: str
    due_at: datetime
    delivered_at: datetime
    late: bool
    conversation_id: uuid.UUID | None
    #: Where in its chat it shows: after this many messages.
    after: int


@dataclass(frozen=True, slots=True)
class Notice:
    """A reminder or a task report, as a chat's timeline shows it."""

    kind: Kind
    job_id: int
    #: A task: what it does (the report's subject).
    task: str | None
    #: A reminder's words, or the run's report.
    text: str
    due_at: datetime
    delivered_at: datetime
    late: bool
    after: int
    outcome: str
    #: A task's own chat, where the whole run can be read.
    chat_id: uuid.UUID | None
    timezone: str


@dataclass(frozen=True, slots=True)
class TaskStart:
    """A task that just came due: its run starts now."""

    job: Job
    due_at: datetime
    late: bool
    #: Which run this is, counting from 1.
    run: int


@dataclass(frozen=True, slots=True)
class TaskEnd:
    """A task run, as recorded."""

    run_id: int
    job: Job
    outcome: Outcome
    #: The task stopped after this run: its end came, its stop was met, or it kept failing.
    stopped: bool
    #: Where in the chat it was set in the report shows: after this many messages.
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
    ) -> Job:
        """Set a reminder; ``at`` is aware (the tool puts a local time in the user's zone)."""
        text = _words(text, "A reminder needs words: what should Enzo remind you of?")
        local, first = _first(at, timezone, repeat, every, ends_on, now or datetime.now(UTC))
        made = await self._add(
            kind="reminder",
            text=text,
            timezone=timezone,
            first_at=local,
            repeat=repeat,
            every=every,
            ends_on=ends_on,
            max_runs=max_runs,
            next_run_at=first,
        )
        log.info("reminder.set", extra={"reminder": made.id, "repeat": repeat, "every": every})
        return made

    async def schedule_task(  # noqa: PLR0913 - one task's settings, all keyword-only
        self,
        instruction: str,
        *,
        at: datetime,
        timezone: str,
        repeat: Repeat = "once",
        every: int = 1,
        ends_on: date | None = None,
        max_runs: int | None = None,
        stop_when: str | None = None,
        now: datetime | None = None,
    ) -> Job:
        """Set a task: ``instruction`` runs as an agent turn at each time (spec §9.3).

        A repeating task must end, after a number of runs or on a last day: each run spends the
        user's model quota, so it never runs on with no end the user agreed to.
        """
        text = _words(instruction, "A task needs words: what should Enzo do on each run?")
        if repeat != "once" and max_runs is None and ends_on is None:
            msg = (
                "A repeating task needs an end: a number of runs or a last date. Ask the user "
                "how long it should run."
            )
            raise ScheduleRefusedError(msg)
        stop = _words(stop_when, "") if stop_when and stop_when.strip() else None
        local, first = _first(at, timezone, repeat, every, ends_on, now or datetime.now(UTC))
        made = await self._add(
            kind="task",
            text=text,
            timezone=timezone,
            first_at=local,
            repeat=repeat,
            every=every,
            ends_on=ends_on,
            max_runs=max_runs,
            next_run_at=first,
            stop_when=stop,
        )
        log.info(
            "task.set",
            extra={"task": made.id, "repeat": repeat, "every": every, "max_runs": max_runs},
        )
        return made

    async def _add(self, **fields: object) -> Job:
        async with self._sessions.begin() as session:
            row = JobRow(
                conversation_id=current_conversation(),
                turn_id=turn_var.get(),
                status="active",
                **fields,
            )
            session.add(row)
            await session.flush()
            return _job(row)

    async def active(self) -> list[Job]:
        """Reminders and tasks still to come, soonest first."""
        async with self._sessions() as session:
            rows = await session.scalars(
                select(JobRow)
                .where(JobRow.status == "active")
                .order_by(JobRow.next_run_at, JobRow.id)
            )
            return [_job(r) for r in rows]

    async def get(self, job_id: int) -> Job | None:
        async with self._sessions() as session:
            row = await session.get(JobRow, job_id)
            return _job(row) if row is not None else None

    async def cancel(self, job_id: int) -> Job:
        """Stop a reminder or task; a task run already going finishes, and none starts again."""
        async with self._sessions.begin() as session:
            row = await session.get(JobRow, job_id, with_for_update=True)
            if row is None or row.status != "active":
                msg = f"There is no reminder or task {job_id} still to come."
                raise ScheduleRefusedError(msg)
            row.status = "cancelled"
            row.next_run_at = None
            gone = _job(row)
        log.info(f"{gone.kind}.cancelled", extra={gone.kind: job_id})
        return gone

    async def set_chat(self, job_id: int, chat_id: uuid.UUID) -> None:
        """A task's runs happen in this chat from now on."""
        async with self._sessions.begin() as session:
            row = await session.get(JobRow, job_id, with_for_update=True)
            if row is not None:
                row.chat_id = chat_id

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
                .where(
                    JobRow.kind == "reminder",
                    JobRow.status == "active",
                    JobRow.next_run_at <= now,
                )
                .order_by(JobRow.next_run_at, JobRow.id)
                .with_for_update(skip_locked=True)
            )
            for row in list(rows):
                due = row.next_run_at
                assert due is not None  # noqa: S101 - the query only finds rows with a time
                after = await _messages_in(session, row.conversation_id)
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
                        reminder=_job(row),
                        text=row.text,
                        due_at=due,
                        delivered_at=now,
                        late=run.late,
                        conversation_id=row.conversation_id,
                        after=after,
                    )
                )
        return out

    async def claim_tasks(self, now: datetime | None = None) -> list[TaskStart]:
        """Start every task that is due and not still running its last run."""
        now = now or datetime.now(UTC)
        out: list[TaskStart] = []
        async with self._sessions.begin() as session:
            rows = await session.scalars(
                select(JobRow)
                .where(
                    JobRow.kind == "task",
                    JobRow.status == "active",
                    JobRow.next_run_at <= now,
                    JobRow.running_since.is_(None),
                )
                .order_by(JobRow.next_run_at, JobRow.id)
                .with_for_update(skip_locked=True)
            )
            for row in list(rows):
                due = row.next_run_at
                assert due is not None  # noqa: S101 - the query only finds rows with a time
                row.running_since = now
                _advance(row, now)
                out.append(TaskStart(_job(row), due, now - due > LATE_AFTER, row.runs))
        return out

    async def finish_run(
        self,
        job_id: int,
        *,
        due_at: datetime,
        late: bool,
        outcome: Outcome,
        summary: str,
        now: datetime | None = None,
    ) -> TaskEnd:
        """Record how a task run went; stop the task when its stop was met or it keeps failing."""
        now = now or datetime.now(UTC)
        async with self._sessions.begin() as session:
            row = await session.get(JobRow, job_id, with_for_update=True)
            if row is None:
                msg = f"no task {job_id}"
                raise KeyError(msg)
            row.running_since = None
            row.failures = row.failures + 1 if outcome == "failed" else 0
            active = row.status == "active"
            if active and (outcome == "finished" or row.failures >= MAX_FAILURES):
                row.status, row.next_run_at = "done", None
            run = JobRunRow(
                job_id=row.id,
                conversation_id=row.conversation_id,
                text=summary,
                due_at=due_at,
                delivered_at=now,
                late=late,
                after=await _messages_in(session, row.conversation_id),
                outcome=outcome,
            )
            session.add(run)
            await session.flush()
            end = TaskEnd(run.id, _job(row), outcome, row.status != "active", run.after)
        log.info(
            "task.run",
            extra={"task": job_id, "run": end.run_id, "outcome": outcome, "stopped": end.stopped},
        )
        return end

    async def recover(self, now: datetime | None = None) -> list[TaskEnd]:
        """After a restart: a run that was going when Enzo stopped is recorded as failed."""
        async with self._sessions() as session:
            ids = list(
                await session.scalars(select(JobRow.id).where(JobRow.running_since.is_not(None)))
            )
            starts = {
                i: await session.scalar(select(JobRow.running_since).where(JobRow.id == i))
                for i in ids
            }
        ended = []
        for job_id, since in starts.items():
            ended.append(
                await self.finish_run(
                    job_id,
                    due_at=since or now or datetime.now(UTC),
                    late=False,
                    outcome="failed",
                    summary="Enzo stopped while this run was going; the next run will try again.",
                    now=now,
                )
            )
        return ended

    async def delivered(self, conversation_id: uuid.UUID) -> list[Notice]:
        """Reminders and task reports that arrived in a chat, in order: shown in its timeline."""
        async with self._sessions() as session:
            rows = await session.execute(
                select(JobRunRow, JobRow)
                .join(JobRow, JobRow.id == JobRunRow.job_id)
                .where(JobRunRow.conversation_id == conversation_id)
                .order_by(JobRunRow.delivered_at, JobRunRow.id)
            )
            return [
                Notice(
                    kind=job.kind,  # type: ignore[arg-type]  # the table's check keeps it
                    job_id=job.id,
                    task=job.text if job.kind == "task" else None,
                    text=run.text,
                    due_at=run.due_at,
                    delivered_at=run.delivered_at,
                    late=run.late,
                    after=run.after,
                    outcome=run.outcome,
                    chat_id=job.chat_id,
                    timezone=job.timezone,
                )
                for run, job in rows
            ]


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


def _words(text: str | None, empty: str) -> str:
    """One line of plain words, never holding a secret (spec §11.2)."""
    words = " ".join((text or "").split())
    if not words:
        raise ScheduleRefusedError(empty)
    reason = secret_reason(words)
    if reason:
        msg = f"Not saved: {reason}, and Enzo never keeps those."
        raise ScheduleRefusedError(msg)
    return words


def _first(
    at: datetime,
    timezone: str,
    repeat: Repeat,
    every: int,
    ends_on: date | None,
    now: datetime,
) -> tuple[datetime, datetime]:
    """The schedule's anchor in the user's zone, and the first time it comes."""
    local = at.astimezone(zone(timezone))
    if repeat == "weekdays" and local.weekday() >= 5:  # a Saturday or Sunday
        local += timedelta(days=7 - local.weekday())  # the Monday after, same time
    first = next_after(local, repeat, every, now - timedelta(minutes=1))
    if first is None:
        msg = (
            f"That time has passed: it is now {now.astimezone(zone(timezone)):%a %-d %b %H:%M}"
            f" in {timezone}. Pick a time later than that."
        )
        raise ScheduleRefusedError(msg)
    if ends_on is not None and first.date() > ends_on:
        msg = f"It would never run: the first time is after {ends_on:%-d %b %Y}."
        raise ScheduleRefusedError(msg)
    return local, first


async def _messages_in(session: AsyncSession, conversation_id: uuid.UUID | None) -> int:
    if conversation_id is None:
        return 0
    found = await session.scalar(
        select(func.count())
        .select_from(MessageRow)
        .where(MessageRow.conversation_id == conversation_id)
    )
    return found or 0


def _job(row: JobRow) -> Job:
    return Job(
        id=row.id,
        kind=row.kind,  # type: ignore[arg-type]  # the table's check keeps it a Kind
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
        stop_when=row.stop_when,
        chat_id=row.chat_id,
        created_at=row.created_at,
    )
