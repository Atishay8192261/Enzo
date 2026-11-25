"""Delivers reminders when they come due, while the gateway runs (spec §9.2, §9.3).

It looks at the wall clock at least every ``tick_s`` seconds, not only after a long sleep: a
computer that slept wakes with old timers, and a reminder due during the sleep must come within
moments of waking, marked late. Setting a reminder wakes it at once.
"""

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime

from enzo.schedule.bus import Bus, Event
from enzo.schedule.notify import Notify, silent
from enzo.schedule.recurrence import zone
from enzo.schedule.store import Delivery, Schedule, TaskStart

log = logging.getLogger("enzo.schedule")

TICK_S = 30.0


class Runner:
    def __init__(
        self,
        schedule: Schedule,
        bus: Bus,
        *,
        notify: Notify = silent,
        tick_s: float = TICK_S,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        on_idle: Callable[[], None] | None = None,
    ) -> None:
        self._schedule = schedule
        self.bus = bus
        self._notify = notify
        self._tick_s = tick_s
        self._clock = clock
        #: Called after a tick when nothing is left to deliver (an idle gateway may stop).
        self._on_idle = on_idle
        #: Starts a due task's run (it runs on its own; this never waits for it). Set by connect.
        self._on_task: Callable[[TaskStart], None] | None = None
        #: A task run is going: not idle, even with nothing left to schedule.
        self._busy: Callable[[], bool] = lambda: False
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._loop())

    def connect(self, on_task: Callable[[TaskStart], None], busy: Callable[[], bool]) -> None:
        """Tasks run through the agent, which is built after the runner: hand it over here."""
        self._on_task, self._busy = on_task, busy

    def poke(self) -> None:
        """Something changed (a reminder was set): look again now."""
        self._wake.set()

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def tick(self) -> list[Delivery]:
        """Deliver everything due now."""
        deliveries = await self._schedule.claim_due(self._clock())
        for d in deliveries:
            log.info(
                "reminder.delivered",
                extra={"reminder": d.reminder.id, "run": d.run_id, "late": d.late},
            )
            self.bus.publish(Event("reminder", _event(d)))
            await self._notify("Enzo reminder", _words(d))
        if self._on_task is not None:
            for start in await self._schedule.claim_tasks(self._clock()):
                log.info(
                    "task.start",
                    extra={"task": start.job.id, "run": start.run, "late": start.late},
                )
                self._on_task(start)
        return deliveries

    async def _loop(self) -> None:
        try:
            for ended in await self._schedule.recover(self._clock()):
                log.warning(
                    "task.interrupted", extra={"task": ended.job.id, "run_id": ended.run_id}
                )
        except Exception:
            log.exception("task.recover_failed")
        while True:
            try:
                await self.tick()
                upcoming = await self._schedule.next_due()
                if upcoming is None and not self._busy() and self._on_idle is not None:
                    self._on_idle()
            except Exception:  # keep delivering: one bad tick must not end reminders
                log.exception("reminder.tick_failed")
                upcoming = None
            wait = self._tick_s
            if upcoming is not None:
                wait = min(wait, max(0.2, (upcoming - self._clock()).total_seconds()))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), wait)
            self._wake.clear()


def _words(d: Delivery) -> str:
    if not d.late:
        return d.text
    due = d.due_at.astimezone(zone(d.reminder.timezone))
    return f"{d.text} (late: due {due:%a %H:%M}, Enzo wasn't running)"


def _event(d: Delivery) -> dict[str, object]:
    return {
        "reminder": d.reminder.id,
        "run": d.run_id,
        "text": d.text,
        "words": _words(d),
        "late": d.late,
        "due_at": d.due_at.isoformat(),
        "conversation_id": str(d.conversation_id) if d.conversation_id else None,
        "next": d.reminder.next_words,
    }
