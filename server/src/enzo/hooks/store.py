"""Watches as kept in the database, so they survive restarts (spec §9.2, §9.4, ADR 0031).

- **Off until the user says so.** A watch is saved off. A dry run records what it reads now;
  turning it on needs that dry run (recent, and the one the user saw: its token) and starts from
  it, so the first check after that compares with exactly what the user approved.
- **One wake per change, across restarts.** A check that sees a change saves the new reading and
  the wake in one transaction, with the row locked. The next check compares with the saved
  reading, so a restart never sees the same change twice, and a wake runs once: one interrupted
  by a restart is recorded as failed, not run again.
- **Stops:** at its end date or after its last wake; after ``MAX_FAILURES`` failed checks in a
  row (backing off in between); at once on a rate limit (every watch of that site or service,
  §8.3) or a refused sign-in. A stop is reported in the chat it was set in.
"""

import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from enzo.context import current_conversation, turn_var
from enzo.hooks.rules import Rule, Seen, describe, is_change
from enzo.hooks.sources import TARGET, PollError, Target, scope_of, where
from enzo.memory.guard import secret_reason
from enzo.schedule.recurrence import zone
from enzo.store.schema import HookEventRow, HookRow, MessageRow
from enzo.tools.registry import plain_words

log = logging.getLogger("enzo.hooks")

MIN_INTERVAL_S = 60
#: Checks that fail this many times in a row stop the watch, with a report.
MAX_FAILURES = 5
#: A failing check waits longer each time (interval x 2, x 4, …), at most this long.
MAX_BACKOFF_S = 6 * 3600
#: Turning a watch on needs a dry run at most this old: the user saw what it reads now.
DRY_RUN_FRESH = timedelta(minutes=30)
#: A wake that runs this long after its change says it is late (Enzo was not running).
LATE_AFTER = timedelta(minutes=2)

type Status = Literal["off", "on", "stopped", "done", "cancelled"]
type StopReason = Literal["rate_limited", "auth", "failing", "ended", "max_wakes"]
type WakeOutcome = Literal["reported", "needs_approval", "failed", "stopped"]


class HookRefusedError(Exception):
    """A plain-language reason a watch was not set or changed, for the brain or the app."""


@dataclass(frozen=True, slots=True)
class Hook:
    id: int
    what: str
    target: Target
    scope: str
    rule: Rule
    threshold: Decimal | None
    match: str | None
    instruction: str
    interval_s: int
    ends_at: datetime | None
    max_wakes: int | None
    wakes: int
    polls: int
    failures: int
    last_error: str | None
    status: Status
    stop_reason: str | None
    shown: str | None
    value_at: datetime | None
    timezone: str
    chat_id: uuid.UUID | None
    conversation_id: uuid.UUID | None
    created_at: datetime | None
    next_poll_at: datetime | None

    @property
    def source(self) -> str:
        return self.target.kind

    @property
    def change_words(self) -> str:
        return describe(self.rule, self.threshold, self.match)

    @property
    def where(self) -> str:
        return where(self.target)

    @property
    def every(self) -> str:
        return every_words(self.interval_s)

    @property
    def ends(self) -> str:
        """Its stop condition in words: "until Thu 10 Dec, at most 1 wake"."""
        parts = []
        if self.ends_at is not None:
            parts.append(f"until {self.ends_at.astimezone(zone(self.timezone)):%a %-d %b %Y}")
        if self.max_wakes is not None:
            parts.append(f"at most {self.max_wakes} wake{'s' if self.max_wakes != 1 else ''}")
        return ", ".join(parts)

    def local(self, at: datetime | None) -> str | None:
        return f"{at.astimezone(zone(self.timezone)):%a %-d %b %H:%M}" if at else None


@dataclass(frozen=True, slots=True)
class DryRun:
    hook: Hook
    seen: Seen
    at: datetime
    #: Names this exact dry run: turning the watch on must name the one the user saw.
    token: str


@dataclass(frozen=True, slots=True)
class Wake:
    """A change to act on: its watch, the readings before and after, and when it was seen."""

    event_id: int
    hook: Hook
    before: str | None
    after: str
    detected_at: datetime
    late: bool


@dataclass(frozen=True, slots=True)
class Stopped:
    """A watch that stopped on its own, as reported in the chat it was set in."""

    event_id: int
    hook: Hook
    reason: StopReason
    words: str


@dataclass(frozen=True, slots=True)
class Checked:
    """What one check led to: a wake to run, watches that stopped, or neither (silent)."""

    hook: Hook | None
    wake: Wake | None = None
    stopped: tuple[Stopped, ...] = ()


@dataclass(frozen=True, slots=True)
class Reported:
    """A wake's report or a stop, as a chat's timeline shows it."""

    hook_id: int
    what: str
    text: str
    detected_at: datetime
    delivered_at: datetime
    late: bool
    position: int
    outcome: str
    chat_id: uuid.UUID | None
    timezone: str


def every_words(seconds: int) -> str:
    minutes = seconds // 60
    if minutes % (24 * 60) == 0:
        days = minutes // (24 * 60)
        return "every day" if days == 1 else f"every {days} days"
    if minutes % 60 == 0:
        hours = minutes // 60
        return "every hour" if hours == 1 else f"every {hours} hours"
    return "every minute" if minutes == 1 else f"every {minutes} minutes"


def backoff_s(interval_s: int, failures: int) -> int:
    """How long a failing watch waits before its next check: doubling, capped."""
    wait: int = min(interval_s * 2**failures, max(MAX_BACKOFF_S, interval_s))
    return wait


def _token(hook_id: int, key: str) -> str:
    """Names what a dry run read. The same reading gives the same token, so a yes still holds
    when Enzo checks again just before acting; a changed reading means asking again (§12.3)."""
    return hashlib.sha256(f"{hook_id}|{key}".encode()).hexdigest()[:16]


def stop_words(hook: Hook, reason: StopReason, error: str | None = None) -> str:
    what = plain_words(hook.what, 60)
    again = " Ask Enzo to turn it on again when you want it back."
    if reason == "rate_limited":
        why = plain_words(error or "it was told to slow down", 160)
        return f"Enzo stopped watching {what}: {why}. Nothing was retried.{again}"
    if reason == "auth":
        why = plain_words(error or "the sign-in was refused", 160)
        return f"Enzo stopped watching {what}: {why}.{again}"
    if reason == "failing":
        why = plain_words(error or "", 160)
        return (
            f"Enzo stopped watching {what}: the last {MAX_FAILURES} checks failed ({why}).{again}"
        )
    if reason == "ended":
        seen = "" if hook.wakes else " It saw no change."
        return f"The watch on {what} ended: its last day passed.{seen}"
    return f"The watch on {what} ended after its last wake."


class Hooks:
    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def create(  # noqa: PLR0913 - one watch's settings, all keyword-only
        self,
        *,
        what: str,
        target: Target,
        rule: Rule,
        instruction: str,
        interval_s: int,
        timezone: str,
        threshold: Decimal | None = None,
        match: str | None = None,
        ends_at: datetime | None = None,
        max_wakes: int | None = None,
        now: datetime | None = None,
    ) -> Hook:
        """Save a watch, off (spec §9.4: hooks start disabled)."""
        now = now or datetime.now(UTC)
        if interval_s < MIN_INTERVAL_S:
            msg = (
                f"A watch checks at most once a minute: use {MIN_INTERVAL_S // 60} minute or more."
            )
            raise HookRefusedError(msg)
        if ends_at is None and max_wakes is None:
            msg = "A watch needs an end: a last day or a number of wakes. Ask the user."
            raise HookRefusedError(msg)
        if ends_at is not None and ends_at <= now:
            msg = "Its last day has already passed: pick a later one."
            raise HookRefusedError(msg)
        for words in (what, instruction, match or "", _searched(target)):
            reason = secret_reason(words)
            if reason:
                msg = f"Not saved: {reason}, and Enzo never keeps those."
                raise HookRefusedError(msg)
        async with self._sessions.begin() as session:
            row = HookRow(
                what=" ".join(what.split()),
                source=target.kind,
                target=target.model_dump(mode="json"),
                scope=scope_of(target),
                rule=rule,
                threshold=threshold,
                match=match,
                instruction=" ".join(instruction.split()),
                interval_s=interval_s,
                ends_at=ends_at,
                max_wakes=max_wakes,
                timezone=timezone,
                status="off",
                conversation_id=current_conversation(),
                turn_id=turn_var.get(),
            )
            session.add(row)
            await session.flush()
            made = _hook(row)
        log.info(
            "hook.set",
            extra={
                "hook": made.id,
                "source": made.source,
                "rule": rule,
                "interval_s": interval_s,
                "max_wakes": max_wakes,
            },
        )
        return made

    async def get(self, hook_id: int) -> Hook | None:
        async with self._sessions() as session:
            row = await session.get(HookRow, hook_id)
            return _hook(row) if row is not None else None

    async def listed(self) -> list[Hook]:
        """Watches the user still has: on, off or stopped (not ended or cancelled), newest first."""
        async with self._sessions() as session:
            rows = await session.scalars(
                select(HookRow)
                .where(HookRow.status.in_(("on", "off", "stopped")))
                .order_by(HookRow.id.desc())
            )
            return [_hook(r) for r in rows]

    async def next_due(self) -> datetime | None:
        """The next check or end of a watch that is on; None: none is on. (A change waiting to
        be acted on is started by the tick that saw it, or the one after the last wake ended.)"""
        async with self._sessions() as session:
            row = (
                await session.execute(
                    select(func.min(HookRow.next_poll_at), func.min(HookRow.ends_at)).where(
                        HookRow.status == "on"
                    )
                )
            ).one()
        times = [t for t in row if t is not None]
        return min(times) if times else None

    async def dry_run(self, hook_id: int, reading: Seen, now: datetime | None = None) -> DryRun:
        """Record what the watch reads now, to show the user before they turn it on."""
        now = now or datetime.now(UTC)
        async with self._sessions.begin() as session:
            row = await session.get(HookRow, hook_id, with_for_update=True)
            if row is None or row.status not in ("off", "stopped", "on"):
                msg = f"There is no watch {hook_id} to check."
                raise HookRefusedError(msg)
            row.dry_value, row.dry_shown = reading.key, reading.shown
            row.dry_number, row.dry_matched, row.dry_at = reading.number, reading.matched, now
            hook = _hook(row)
        log.info("hook.dry_run", extra={"hook": hook_id})
        return DryRun(hook, reading, now, _token(hook_id, reading.key))

    async def enable(self, hook_id: int, token: str | None, now: datetime | None = None) -> Hook:
        """Turn a watch on, starting from the dry run the user saw (``token``)."""
        now = now or datetime.now(UTC)
        async with self._sessions.begin() as session:
            row = await session.get(HookRow, hook_id, with_for_update=True)
            if row is None or row.status not in ("off", "stopped"):
                state = "on already" if row is not None and row.status == "on" else "not there"
                msg = f"Watch {hook_id} is {state}: there is nothing to turn on."
                raise HookRefusedError(msg)
            if row.dry_at is None or row.dry_value is None or not token:
                msg = "Check it first: the user must see what it reads now before it goes on."
                raise HookRefusedError(msg)
            if token != _token(hook_id, row.dry_value):
                msg = "That is not the latest check of this watch: check it again first."
                raise HookRefusedError(msg)
            if now - row.dry_at > DRY_RUN_FRESH:
                msg = "Its last check is more than 30 minutes old: check it again first."
                raise HookRefusedError(msg)
            if row.ends_at is not None and row.ends_at <= now:
                msg = "Its last day has passed: set up a new watch instead."
                raise HookRefusedError(msg)
            row.status, row.stop_reason, row.stopped_at = "on", None, None
            row.value, row.shown, row.number = row.dry_value, row.dry_shown, row.dry_number
            row.matched, row.value_at = row.dry_matched, row.dry_at
            row.failures, row.last_error, row.polling_since = 0, None, None
            row.enabled_at = now
            row.next_poll_at = now + timedelta(seconds=row.interval_s)
            hook = _hook(row)
        log.info("hook.enabled", extra={"hook": hook_id, "interval_s": hook.interval_s})
        return hook

    async def cancel(self, hook_id: int, now: datetime | None = None) -> Hook:
        """Stop and remove a watch; a wake already running finishes, none starts again."""
        async with self._sessions.begin() as session:
            row = await session.get(HookRow, hook_id, with_for_update=True)
            if row is None or row.status not in ("on", "off", "stopped"):
                msg = f"There is no watch {hook_id} to cancel."
                raise HookRefusedError(msg)
            row.status, row.next_poll_at = "cancelled", None
            row.stopped_at = now or datetime.now(UTC)
            gone = _hook(row)
        log.info("hook.cancelled", extra={"hook": hook_id})
        return gone

    async def set_chat(self, hook_id: int, chat_id: uuid.UUID) -> None:
        async with self._sessions.begin() as session:
            row = await session.get(HookRow, hook_id, with_for_update=True)
            if row is not None:
                row.chat_id = chat_id

    async def end_due(self, now: datetime | None = None) -> list[Stopped]:
        """Watches whose last day passed: on ones end with a report, off ones quietly."""
        now = now or datetime.now(UTC)
        out = []
        async with self._sessions.begin() as session:
            rows = await session.scalars(
                select(HookRow)
                .where(HookRow.status.in_(("on", "off", "stopped")), HookRow.ends_at <= now)
                .with_for_update(skip_locked=True)
            )
            for row in list(rows):
                was_on = row.status == "on"
                row.status, row.stop_reason, row.next_poll_at = "done", "ended", None
                row.stopped_at = now
                if was_on:
                    out.append(await _stopped(session, row, "ended", None, now))
        for s in out:
            log.info("hook.stopped", extra={"hook": s.hook.id, "reason": "ended"})
        return out

    async def claim_polls(self, now: datetime | None = None) -> list[Hook]:
        """Watches due a check, each marked as being checked (never claimed twice)."""
        now = now or datetime.now(UTC)
        async with self._sessions.begin() as session:
            rows = await session.scalars(
                select(HookRow)
                .where(
                    HookRow.status == "on",
                    HookRow.next_poll_at <= now,
                    HookRow.polling_since.is_(None),
                )
                .order_by(HookRow.next_poll_at, HookRow.id)
                .with_for_update(skip_locked=True)
            )
            claimed = []
            for row in list(rows):
                row.polling_since = now
                claimed.append(_hook(row))
        return claimed

    async def record(
        self, hook_id: int, outcome: Seen | PollError, now: datetime | None = None
    ) -> Checked:
        """Save one check: silent when nothing changed by the rule, a wake when it did."""
        now = now or datetime.now(UTC)
        async with self._sessions.begin() as session:
            row = await session.get(HookRow, hook_id, with_for_update=True)
            if row is None:
                return Checked(None)
            row.polling_since = None
            if row.status != "on":  # cancelled or stopped while it was checking
                return Checked(_hook(row))
            row.polls += 1
            if isinstance(outcome, PollError):
                return await _failed(session, row, outcome, now)
            row.failures, row.last_error = 0, None
            before = (
                Seen(row.value, row.shown or "", row.number, row.matched)
                if row.value is not None
                else None
            )
            changed = is_change(row.rule, row.threshold, before, outcome)  # type: ignore[arg-type]
            row.value, row.shown, row.number = outcome.key, outcome.shown, outcome.number
            row.matched, row.value_at = outcome.matched, now
            row.next_poll_at = now + timedelta(seconds=row.interval_s)
            if not changed:
                return Checked(_hook(row))
            row.wakes += 1
            if row.max_wakes is not None and row.wakes >= row.max_wakes:
                row.status, row.stop_reason = "done", "max_wakes"
                row.next_poll_at, row.stopped_at = None, now
            event = HookEventRow(
                hook_id=row.id,
                kind="wake",
                before=before.shown if before else None,
                after=outcome.shown,
                detected_at=now,
                state="pending",
                conversation_id=row.conversation_id,
            )
            session.add(event)
            await session.flush()
            hook = _hook(row)
            wake = Wake(event.id, hook, event.before, outcome.shown, now, late=False)
        log.info(
            "hook.changed", extra={"hook": hook_id, "event": wake.event_id, "wakes": hook.wakes}
        )
        return Checked(hook, wake=wake)

    async def claim_wakes(self, now: datetime | None = None) -> list[Wake]:
        """Changes waiting to run, one at a time per watch, each marked as running."""
        now = now or datetime.now(UTC)
        other = aliased(HookEventRow)
        out = []
        async with self._sessions.begin() as session:
            rows = await session.execute(
                select(HookEventRow, HookRow)
                .join(HookRow, HookRow.id == HookEventRow.hook_id)
                .where(
                    HookEventRow.state == "pending",
                    ~exists().where(
                        other.hook_id == HookEventRow.hook_id, other.state == "running"
                    ),
                )
                .order_by(HookEventRow.detected_at, HookEventRow.id)
                .with_for_update(of=HookEventRow, skip_locked=True)
            )
            started: set[int] = set()
            for event, row in list(rows):
                if row.id in started:
                    continue  # the next change of this watch runs after this one
                if row.status == "cancelled":  # cancelled before its wake could run
                    event.state, event.outcome, event.delivered_at = "done", "stopped", now
                    event.text = "The watch was cancelled before this change could be acted on."
                    event.position = await _messages_in(session, event.conversation_id)
                    continue
                started.add(row.id)
                event.state, event.started_at = "running", now
                out.append(
                    Wake(
                        event.id,
                        _hook(row),
                        event.before,
                        event.after or "",
                        event.detected_at,
                        late=now - event.detected_at > LATE_AFTER,
                    )
                )
        return out

    async def finish_wake(
        self, event_id: int, outcome: WakeOutcome, text: str, now: datetime | None = None
    ) -> tuple[Hook, int]:
        """Record how a wake went; returns its watch and where in its chat the report shows."""
        now = now or datetime.now(UTC)
        async with self._sessions.begin() as session:
            event = await session.get(HookEventRow, event_id, with_for_update=True)
            if event is None:
                msg = f"no watch event {event_id}"
                raise KeyError(msg)
            event.state, event.outcome, event.text, event.delivered_at = "done", outcome, text, now
            event.late = event.started_at is not None and (
                event.started_at - event.detected_at > LATE_AFTER
            )
            event.position = await _messages_in(session, event.conversation_id)
            row = await session.get(HookRow, event.hook_id)
            assert row is not None  # noqa: S101 - the event's foreign key keeps it
            hook, position = _hook(row), event.position
        log.info("hook.wake", extra={"hook": hook.id, "event": event_id, "outcome": outcome})
        return hook, position

    async def recover(self, now: datetime | None = None) -> list[int]:
        """After a restart: a wake that was running is recorded as failed (it never runs twice),
        and a check that was going is simply done again. Returns the failed wakes' ids."""
        now = now or datetime.now(UTC)
        async with self._sessions.begin() as session:
            events = list(
                await session.scalars(
                    select(HookEventRow)
                    .where(HookEventRow.state == "running")
                    .with_for_update(skip_locked=True)
                )
            )
            for event in events:
                event.state, event.outcome, event.delivered_at = "done", "failed", now
                event.text = (
                    "Enzo stopped while it was acting on this change; it does not run again."
                )
                event.position = await _messages_in(session, event.conversation_id)
            await session.execute(
                update(HookRow).where(HookRow.polling_since.is_not(None)).values(polling_since=None)
            )
        return [e.id for e in events]

    async def reported(self, conversation_id: uuid.UUID) -> list[Reported]:
        """Wake reports and stops that arrived in a chat, in order: shown in its timeline."""
        async with self._sessions() as session:
            rows = await session.execute(
                select(HookEventRow, HookRow)
                .join(HookRow, HookRow.id == HookEventRow.hook_id)
                .where(
                    HookEventRow.conversation_id == conversation_id,
                    HookEventRow.state == "done",
                )
                .order_by(HookEventRow.delivered_at, HookEventRow.id)
            )
            return [
                Reported(
                    hook_id=row.id,
                    what=row.what,
                    text=event.text or "",
                    detected_at=event.detected_at,
                    delivered_at=event.delivered_at or event.detected_at,
                    late=event.late,
                    position=event.position or 0,
                    outcome=event.outcome or "reported",
                    chat_id=row.chat_id,
                    timezone=row.timezone,
                )
                for event, row in rows
            ]


async def _failed(session: AsyncSession, row: HookRow, error: PollError, now: datetime) -> Checked:
    """A check that failed: back off; stop after a few in a row, or at once on a rate limit or a
    refused sign-in (every watch of the same site or service, §8.3)."""
    row.failures += 1
    row.last_error = plain_words(str(error), 300)
    if error.stop is not None:
        rows = await session.scalars(
            select(HookRow)
            .where(HookRow.scope == row.scope, HookRow.status == "on", HookRow.id != row.id)
            .with_for_update(skip_locked=True)
        )
        stopped = [await _stop(session, r, error.stop, str(error), now) for r in [row, *rows]]
        return Checked(_hook(row), stopped=tuple(stopped))
    if row.failures >= MAX_FAILURES:
        stop = await _stop(session, row, "failing", row.last_error, now)
        return Checked(_hook(row), stopped=(stop,))
    row.next_poll_at = now + timedelta(seconds=backoff_s(row.interval_s, row.failures))
    return Checked(_hook(row))


async def _stop(
    session: AsyncSession, row: HookRow, reason: StopReason, error: str | None, now: datetime
) -> Stopped:
    row.status, row.stop_reason, row.next_poll_at, row.stopped_at = "stopped", reason, None, now
    row.polling_since = None
    stopped = await _stopped(session, row, reason, error, now)
    log.warning(
        "hook.stopped",
        extra={"hook": row.id, "reason": reason, "polls": row.polls, "wakes": row.wakes},
    )
    return stopped


async def _stopped(
    session: AsyncSession, row: HookRow, reason: StopReason, error: str | None, now: datetime
) -> Stopped:
    """Record a stop where the watch was set, as its timeline shows it."""
    hook = _hook(row)
    words = stop_words(hook, reason, error)
    event = HookEventRow(
        hook_id=row.id,
        kind="stopped",
        detected_at=now,
        state="done",
        outcome="stopped",
        text=words,
        conversation_id=row.conversation_id,
        position=await _messages_in(session, row.conversation_id),
        delivered_at=now,
    )
    session.add(event)
    await session.flush()
    return Stopped(event.id, hook, reason, words)


async def _messages_in(session: AsyncSession, conversation_id: uuid.UUID | None) -> int:
    if conversation_id is None:
        return 0
    found = await session.scalar(
        select(func.count())
        .select_from(MessageRow)
        .where(MessageRow.conversation_id == conversation_id)
    )
    return found or 0


def _searched(target: Target) -> str:
    """The words a target holds that came from the user (checked for secrets)."""
    data: dict[str, Any] = target.model_dump()
    return " ".join(str(v) for k, v in data.items() if k in ("query", "text", "selector"))


def _hook(row: HookRow) -> Hook:
    return Hook(
        id=row.id,
        what=row.what,
        target=TARGET.validate_python(row.target),
        scope=row.scope,
        rule=row.rule,  # type: ignore[arg-type]  # the table's check keeps it a Rule
        threshold=row.threshold,
        match=row.match,
        instruction=row.instruction,
        interval_s=row.interval_s,
        ends_at=row.ends_at,
        max_wakes=row.max_wakes,
        wakes=row.wakes,
        polls=row.polls,
        failures=row.failures,
        last_error=row.last_error,
        status=row.status,  # type: ignore[arg-type]  # the table's check keeps it a Status
        stop_reason=row.stop_reason,
        shown=row.shown,
        value_at=row.value_at,
        timezone=row.timezone,
        chat_id=row.chat_id,
        conversation_id=row.conversation_id,
        created_at=row.created_at,
        next_poll_at=row.next_poll_at,
    )
