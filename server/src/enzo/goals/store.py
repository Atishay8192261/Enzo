"""Goals (spec §6.4): what the user is working toward, as records, not free text.

Each goal has a number, a title, a status (open, done or dropped), a due date if any, the place in
the user's Enzo folder where its work lives, and its steps: what remains. Every change is kept
with its evidence (spec §6.5): what shows it, how that is known, where it can be found again, and
when it happened. The rules hold here, for every caller, not only in the prompt:

- **Only on evidence.** A step is done, or a goal closed, only when the user confirmed it, a check
  verified it (with a source to find it again), or, to close a goal, its due date has passed.
- **A plan is not a result.** Evidence that reads as planned or proposed ("will", "going to") is
  refused: a proposed action is never recorded as done.
- **One step is not the goal.** Recording steps never closes a goal. Closing one as done while
  steps remain needs the user's own word that the whole goal is done.
- **Closing stops the check-ins.** Reminders and tasks linked to a goal are cancelled with it, in
  the same transaction.
- **Never secrets** (spec §11.2): titles, steps and evidence pass the memory guard.
"""

import asyncio
import logging
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from enzo.context import current_conversation, timezone_var, turn_var
from enzo.goals.goals_md import (
    GOALS_FILE,
    render,
    set_aside,
    untouched,
    write_read_only,
)
from enzo.memory.guard import secret_reason
from enzo.paths import shown
from enzo.schedule.recurrence import local_zone, zone
from enzo.schedule.store import Job, Schedule
from enzo.store.schema import Conversation, GoalEventRow, GoalRow, GoalStepRow, JobRow

log = logging.getLogger("enzo.goals")

type Status = Literal["open", "done", "dropped"]
type Basis = Literal["user_confirmed", "verified", "timeline_ended"]

_PLANNED = re.compile(
    r"\b(will|going to|plan(?:s|ning)? to|intends? to|about to|would like to|wants? to|"
    r"proposed?|suggested?|should)\b",
    re.IGNORECASE,
)
MAX_STEPS = 30


class GoalRefusedError(Exception):
    """A plain-language reason a goal was not set or changed, for the brain to relay."""


@dataclass(frozen=True, slots=True)
class Evidence:
    """One change of a goal and what it rests on."""

    change: str
    #: The step it is about, by its number.
    step: int | None
    evidence: str | None
    basis: str | None
    source: str | None
    happened_on: date | None
    recorded_at: datetime
    #: The chat it was recorded in, when that chat still exists.
    chat: str | None


@dataclass(frozen=True, slots=True)
class Step:
    number: int
    text: str
    done_at: datetime | None
    #: Why it counts as done.
    evidence: Evidence | None = None

    @property
    def done(self) -> bool:
        return self.done_at is not None


@dataclass(frozen=True, slots=True)
class Goal:
    id: int
    title: str
    status: Status
    due_on: date | None
    #: Where its work lives in the user's Enzo folder ("passport", "trip.md").
    path: str | None
    created_at: datetime
    closed_at: datetime | None
    conversation_id: uuid.UUID | None
    chat: str | None
    steps: tuple[Step, ...]
    history: tuple[Evidence, ...]
    #: Reminders and tasks still to come that check in on it.
    check_ins: tuple[Job, ...] = ()

    @property
    def remaining(self) -> list[Step]:
        return [s for s in self.steps if not s.done]

    @property
    def closing(self) -> Evidence | None:
        """Why it was closed, when it is."""
        return next((e for e in reversed(self.history) if e.change in ("done", "dropped")), None)


@dataclass(frozen=True, slots=True)
class Closed:
    goal: Goal
    #: The check-ins that stopped with it.
    stopped: tuple[Job, ...]


def user_today() -> date:
    """Today in the user's zone (from their app, else this computer's)."""
    tz = timezone_var.get()
    return datetime.now(zone(tz) if tz else local_zone()).date()


class Goals:
    def __init__(
        self,
        engine: AsyncEngine,
        workspace: Path | None = None,
        schedule: Schedule | None = None,
    ) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)
        self._workspace = workspace
        self._schedule = schedule or Schedule(engine)
        self._lock = asyncio.Lock()
        self._seen: tuple[int, int, int] | None = None

    @property
    def file(self) -> Path | None:
        return self._workspace / GOALS_FILE if self._workspace else None

    async def create(
        self,
        title: str,
        *,
        steps: Sequence[str] = (),
        due_on: date | None = None,
        path: str | None = None,
    ) -> Goal:
        title = _words(title, "A goal needs a title: what is the user working toward?")
        texts = [_words(s, "A step needs words.") for s in steps]
        folder = _folder(path)
        if len(texts) > MAX_STEPS:
            msg = f"At most {MAX_STEPS} steps: keep the ones that matter."
            raise GoalRefusedError(msg)
        async with self._lock, self._sessions.begin() as session:
            same = await session.scalar(
                select(GoalRow.id).where(
                    GoalRow.status == "open", func.lower(GoalRow.title) == title.lower()
                )
            )
            if same is not None:
                msg = f"There is already an open goal #{same} with that title: update it instead."
                raise GoalRefusedError(msg)
            row = GoalRow(
                title=title,
                due_on=due_on,
                path=folder,
                conversation_id=current_conversation(),
                turn_id=turn_var.get(),
            )
            session.add(row)
            await session.flush()
            for n, text in enumerate(texts, start=1):
                session.add(GoalStepRow(goal_id=row.id, position=n, text=text))
            session.add(_event(row.id, "created"))
            made = row.id
        log.info("goal.created", extra={"goal": made, "steps": len(texts), "due": bool(due_on)})
        await self._write_file()
        return await self._one(made)

    async def progress(
        self,
        goal_id: int,
        *,
        done: Sequence[int] = (),
        evidence: str | None = None,
        basis: Basis | None = None,
        source: str | None = None,
        happened_on: date | None = None,
        add: Sequence[str] = (),
    ) -> Goal:
        """Record steps done (on evidence) and steps that turned out to remain. Never closes."""
        new = [_words(s, "A step needs words.") for s in add]
        if not done and not new:
            msg = "Nothing to record: give the steps done, or the steps to add."
            raise GoalRefusedError(msg)
        proof = _evidence(evidence, basis, source, happened_on, done=True) if done else None
        if basis == "timeline_ended" and done:
            msg = "A step is done when the user confirms it or a check shows it, not by a date."
            raise GoalRefusedError(msg)
        async with self._lock, self._sessions.begin() as session:
            row = await _open(session, goal_id)
            steps = {
                s.position: s
                for s in await session.scalars(
                    select(GoalStepRow).where(GoalStepRow.goal_id == goal_id).with_for_update()
                )
            }
            for n in dict.fromkeys(done):
                step = steps.get(n)
                if step is None:
                    msg = f"Goal #{goal_id} has no step {n}: its steps are 1 to {len(steps)}."
                    raise GoalRefusedError(msg)
                if step.done_at is not None:
                    msg = f"Step {n} of goal #{goal_id} is already done."
                    raise GoalRefusedError(msg)
                step.done_at = datetime.now(UTC)
                assert proof is not None  # noqa: S101 - done steps always come with evidence
                session.add(_event(row.id, "step_done", step_id=step.id, **proof))
            if len(steps) + len(new) > MAX_STEPS:
                msg = f"At most {MAX_STEPS} steps: keep the ones that matter."
                raise GoalRefusedError(msg)
            for n, text in enumerate(new, start=len(steps) + 1):
                session.add(GoalStepRow(goal_id=row.id, position=n, text=text))
            if new:
                session.add(_event(row.id, "steps_added"))
        log.info("goal.progress", extra={"goal": goal_id, "done": len(done), "added": len(new)})
        await self._write_file()
        return await self._one(goal_id)

    async def close(
        self,
        goal_id: int,
        *,
        outcome: Literal["done", "dropped"],
        evidence: str,
        basis: Basis,
        source: str | None = None,
        happened_on: date | None = None,
        today: date | None = None,
    ) -> Closed:
        """Close a goal on evidence, and stop every check-in linked to it."""
        proof = _evidence(evidence, basis, source, happened_on, done=outcome == "done")
        async with self._lock, self._sessions.begin() as session:
            row = await _open(session, goal_id)
            if basis == "timeline_ended":
                now = today or user_today()
                if row.due_on is None or row.due_on >= now:
                    when = f"it is due {row.due_on:%-d %b %Y}" if row.due_on else "it has no date"
                    msg = f"Its time has not run out: {when}. Close it on other evidence."
                    raise GoalRefusedError(msg)
            left = await session.scalar(
                select(func.count())
                .select_from(GoalStepRow)
                .where(GoalStepRow.goal_id == goal_id, GoalStepRow.done_at.is_(None))
            )
            if outcome == "done" and left and basis != "user_confirmed":
                msg = (
                    f"{left} step(s) of goal #{goal_id} remain. Steps done do not finish a goal: "
                    "record each step as it is done, or ask the user whether the whole goal is "
                    "done (their word closes it)."
                )
                raise GoalRefusedError(msg)
            row.status, row.closed_at = outcome, datetime.now(UTC)
            session.add(_event(row.id, outcome, **proof))
            linked = list(
                await session.scalars(
                    select(JobRow)
                    .where(JobRow.goal_id == goal_id, JobRow.status == "active")
                    .with_for_update()
                )
            )
            for job in linked:
                job.status, job.next_run_at = "cancelled", None
            stopped = [j.id for j in linked]
        for job in linked:
            log.info(f"{job.kind}.cancelled", extra={job.kind: job.id, "goal": goal_id})
        log.info(
            "goal.closed",
            extra={"goal": goal_id, "outcome": outcome, "basis": basis, "stopped": stopped},
        )
        await self._write_file()
        closed = await self._one(goal_id)
        return Closed(closed, tuple(await self._jobs(stopped)))

    async def get(self, goal_id: int) -> Goal | None:
        found = await self._load(GoalRow.id == goal_id)
        return found[0] if found else None

    async def open(self) -> list[Goal]:
        """Open goals: due soonest first (those with no date last), then the newest."""
        found = await self._load(GoalRow.status == "open")
        return sorted(found, key=lambda g: (g.due_on or date.max, -g.id))

    async def closed(self) -> list[Goal]:
        """Done and dropped goals, the most recently closed first."""
        found = await self._load(GoalRow.status != "open")
        return sorted(found, key=lambda g: (g.closed_at or g.created_at, g.id), reverse=True)

    async def check_file(self) -> str | None:
        """Goals.md is read-only: a hand edit is kept aside, the file written again, and a note
        returned for the user (never silently lost). Cheap when nobody touched it."""
        path = self.file
        if path is None:
            return None
        async with self._lock:
            stat = await asyncio.to_thread(_stat, path)
            if stat is not None and stat == self._seen:
                return None
            if stat is None:
                if await self._load():  # deleted by hand: written again
                    await self._write_unlocked()
                return None
            body = await asyncio.to_thread(path.read_bytes)
            if untouched(body):
                self._seen = stat
                return None
            kept = await asyncio.to_thread(set_aside, path)
            log.info("goals.file_edited")
            await self._write_unlocked()
            return (
                "Goals.md was edited by hand, but it is a read-only copy: Enzo keeps goals only "
                f"through the chat and /goals. The user's edited copy was kept as “{kept}”. Ask "
                "them what they wanted to change, and change it with them."
            )

    async def _write_file(self) -> None:
        async with self._lock:
            await self._write_unlocked()

    async def _write_unlocked(self) -> None:
        path = self.file
        if path is None:
            return
        body = render(await self.open(), await self.closed(), shown_folder=self.shown_folder)
        self._seen = await asyncio.to_thread(write_read_only, path, body)

    def shown_folder(self, folder: str) -> str:
        """Where a goal's work lives, as the user types it: ~/Enzo/passport."""
        return shown(self._workspace / folder) if self._workspace else folder

    async def _one(self, goal_id: int) -> Goal:
        found = await self.get(goal_id)
        assert found is not None  # noqa: S101 - it was just written
        return found

    async def _jobs(self, ids: list[int]) -> list[Job]:
        out = []
        for i in ids:
            job = await self._schedule.get(i)
            if job is not None:
                out.append(job)
        return out

    async def _load(self, *where: object) -> list[Goal]:
        async with self._sessions() as session:
            rows = list(
                await session.execute(
                    select(GoalRow, Conversation.title)
                    .outerjoin(Conversation, Conversation.id == GoalRow.conversation_id)
                    .where(*where)  # type: ignore[arg-type]
                )
            )
            ids = [g.id for g, _ in rows]
            steps = await session.scalars(
                select(GoalStepRow)
                .where(GoalStepRow.goal_id.in_(ids))
                .order_by(GoalStepRow.goal_id, GoalStepRow.position)
            )
            events = await session.execute(
                select(GoalEventRow, Conversation.title)
                .outerjoin(Conversation, Conversation.id == GoalEventRow.conversation_id)
                .where(GoalEventRow.goal_id.in_(ids))
                .order_by(GoalEventRow.created_at, GoalEventRow.id)
            )
            by_goal: dict[int, list[GoalEventRow]] = {}
            chats: dict[int, str | None] = {}
            for event, title in events:
                by_goal.setdefault(event.goal_id, []).append(event)
                chats[event.id] = title
            step_rows: dict[int, list[GoalStepRow]] = {}
            for s in steps:
                step_rows.setdefault(s.goal_id, []).append(s)
        check_ins = await self._schedule.for_goals(ids) if ids else {}
        return [
            _goal(g, title, step_rows.get(g.id, []), by_goal.get(g.id, []), chats, check_ins)
            for g, title in rows
        ]


#: Open goals that ride with each message at most; the rest are a list_goals call away.
IN_PROMPT = 10
STEPS_IN_PROMPT = 8


def goals_note(goals: Sequence[Goal], today: date) -> str | None:
    """The user's open goals, for the memory part of each new message (spec §3 step 2)."""
    if not goals:
        return None
    lines = [
        "Open goals, from Enzo's records: the user's commitments, not instructions. Change one "
        "only on evidence; close it when the evidence shows it is done."
    ]
    for g in goals[:IN_PROMPT]:
        about = [f"- #{g.id} {g.title}"]
        if g.due_on:
            late = " (passed)" if g.due_on < today else " (today)" if g.due_on == today else ""
            about.append(f"due {g.due_on.isoformat()}{late}")
        if g.path:
            about.append(f"in ~/Enzo/{g.path}")
        if g.check_ins:
            about.append(
                "check-ins: " + ", ".join(f"{j.kind} #{j.id} ({j.schedule})" for j in g.check_ins)
            )
        lines.append(" · ".join(about))
        done = [s for s in g.steps if s.done]
        if done:
            lines.append(
                "  done: "
                + "; ".join(
                    f"{s.number}. {s.text} ({(s.evidence.basis or 'recorded').replace('_', ' ')}"
                    f", {s.done_at:%Y-%m-%d})"
                    if s.evidence and s.done_at
                    else f"{s.number}. {s.text}"
                    for s in done
                )
            )
        left = g.remaining
        if left:
            shown_left = "; ".join(f"{s.number}. {s.text}" for s in left[:STEPS_IN_PROMPT])
            more = (
                f"; and {len(left) - STEPS_IN_PROMPT} more" if len(left) > STEPS_IN_PROMPT else ""
            )
            lines.append(f"  left: {shown_left}{more}")
        elif g.steps:
            lines.append("  left: no steps; close it once the outcome itself is confirmed")
    if len(goals) > IN_PROMPT:
        lines.append(f"- and {len(goals) - IN_PROMPT} more open goals: list_goals shows them all.")
    return "\n".join(lines)


async def _open(session: AsyncSession, goal_id: int) -> GoalRow:
    row = await session.get(GoalRow, goal_id, with_for_update=True)
    if row is None:
        msg = f"There is no goal #{goal_id}: list the goals first."
        raise GoalRefusedError(msg)
    if row.status != "open":
        msg = f"Goal #{goal_id} is already {row.status}."
        raise GoalRefusedError(msg)
    return row


def _event(goal_id: int, change: str, **fields: object) -> GoalEventRow:
    return GoalEventRow(
        goal_id=goal_id,
        change=change,
        conversation_id=current_conversation(),
        turn_id=turn_var.get(),
        **fields,
    )


def _evidence(
    evidence: str | None,
    basis: Basis | None,
    source: str | None,
    happened_on: date | None,
    *,
    done: bool,
) -> dict[str, object]:
    """The evidence for a change, checked: present, sourced when verified, and not a plan."""
    words = " ".join((evidence or "").split())
    if not words or basis is None:
        msg = (
            "A change needs its evidence: what shows it, and how you know (the user confirmed "
            "it, or you verified it). If you only think it is so, ask the user first."
        )
        raise GoalRefusedError(msg)
    where = " ".join((source or "").split()) or None
    if basis == "verified" and where is None:
        msg = "Verified evidence needs its source: the page, file or result you checked."
        raise GoalRefusedError(msg)
    if done and _PLANNED.search(words):
        msg = (
            "Not recorded: the evidence reads as something planned or proposed, not done. "
            "Record it once it has happened, in the past tense ('The user said they mailed it')."
        )
        raise GoalRefusedError(msg)
    for text in (words, where or ""):
        reason = secret_reason(text)
        if reason:
            msg = f"Not saved: {reason}, and Enzo never keeps those."
            raise GoalRefusedError(msg)
    return {"evidence": words, "basis": basis, "source": where, "happened_on": happened_on}


def _words(text: str, empty: str) -> str:
    words = " ".join(text.split())
    if not words:
        raise GoalRefusedError(empty)
    reason = secret_reason(words)
    if reason:
        msg = f"Not saved: {reason}, and Enzo never keeps those."
        raise GoalRefusedError(msg)
    return words


def _folder(path: str | None) -> str | None:
    """Where its work lives, relative to the user's Enzo folder; never outside it."""
    if path is None or not path.strip():
        return None
    raw = path.strip().removeprefix("~/Enzo/").removeprefix("~/Enzo")
    folder = PurePosixPath(raw)
    if folder.is_absolute() or ".." in folder.parts or (folder.parts[:1] == (".enzo",)):
        msg = "The folder must be inside the user's Enzo folder, like 'passport' or 'trip.md'."
        raise GoalRefusedError(msg)
    return folder.as_posix() if folder.parts else None


def _goal(
    row: GoalRow,
    chat: str | None,
    steps: list[GoalStepRow],
    events: list[GoalEventRow],
    chats: dict[int, str | None],
    check_ins: dict[int, list[Job]],
) -> Goal:
    position = {s.id: s.position for s in steps}
    history = tuple(
        Evidence(
            change=e.change,
            step=position.get(e.step_id) if e.step_id else None,
            evidence=e.evidence,
            basis=e.basis,
            source=e.source,
            happened_on=e.happened_on,
            recorded_at=e.created_at,
            chat=chats.get(e.id),
        )
        for e in events
    )
    proof = {e.step: e for e in history if e.change == "step_done"}
    return Goal(
        id=row.id,
        title=row.title,
        status=row.status,  # type: ignore[arg-type]  # the table's check keeps it a Status
        due_on=row.due_on,
        path=row.path,
        created_at=row.created_at,
        closed_at=row.closed_at,
        conversation_id=row.conversation_id,
        chat=chat,
        steps=tuple(
            Step(s.position, s.text, s.done_at, proof.get(s.position) if s.done_at else None)
            for s in steps
        ),
        history=history,
        check_ins=tuple(check_ins.get(row.id, [])),
    )


def _stat(path: Path) -> tuple[int, int, int] | None:
    try:
        found = path.stat()
    except FileNotFoundError:
        return None
    return (found.st_mtime_ns, found.st_size, found.st_ino)
