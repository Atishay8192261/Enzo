"""Reminders (spec §9.2, §9.3): set in plain words, delivered once at their time in the user's
own clock, late after a sleep, stopped by their stop condition or by cancelling, and still there
after a restart."""

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from enzo.agent.service import ConversationService
from enzo.brain import Message, ToolCall
from enzo.brain.fake import ScriptedBrain
from enzo.brain.router import Router
from enzo.context import timezone_var
from enzo.schedule import Background, Bus, Runner, Schedule, reminder_tools
from enzo.schedule.recurrence import describe, next_after, occurrence
from enzo.schedule.store import Delivery, ReminderRefusedError
from enzo.store.repository import Store
from enzo.tools.registry import ToolRegistry
from tests.gateway.test_api import client, config, new_conversation

LA = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 11, 20, 17, 0, tzinfo=UTC)  # Fri 20 Nov, 09:00 in Los Angeles


def la(year: int, month: int, day: int, hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=LA)


# The user's clock: repeats keep their wall time.


def test_a_daily_reminder_keeps_its_time_across_daylight_saving() -> None:
    first = la(2026, 10, 30, 9, 0)  # clocks go back on Sun 1 Nov
    days = [occurrence(first, "daily", 1, n) for n in range(4)]
    assert [(d.day, d.hour, d.utcoffset()) for d in days] == [
        (30, 9, timedelta(hours=-7)),
        (31, 9, timedelta(hours=-7)),
        (1, 9, timedelta(hours=-8)),
        (2, 9, timedelta(hours=-8)),
    ]
    hourly = [occurrence(la(2026, 11, 1, 0, 30), "hourly", 1, n) for n in range(3)]
    assert [h.astimezone(UTC).hour for h in hourly] == [7, 8, 9]  # real hours, not wall hours


def test_months_weekdays_and_a_skipped_hour() -> None:
    month = la(2026, 1, 31, 8, 0)
    assert [occurrence(month, "monthly", 1, n).day for n in range(4)] == [31, 28, 31, 30]
    friday = la(2026, 11, 20, 9, 0)
    assert [occurrence(friday, "weekdays", 1, n).strftime("%a") for n in range(4)] == [
        "Fri",
        "Mon",
        "Tue",
        "Wed",
    ]
    assert occurrence(la(2026, 3, 7, 2, 30), "daily", 1, 1).hour == 3  # 02:30 does not exist
    years_later = next_after(friday, "daily", 1, datetime(2036, 1, 1, tzinfo=UTC))
    assert years_later is not None
    assert (years_later.year, years_later.hour) == (2036, 9)
    assert describe(friday, "weekly", 2) == "every 2 weeks on Friday at 09:00"
    assert describe(friday, "weekdays", 1) == "every weekday at 09:00"


# The store: once, on time, until its stop condition.


@pytest.fixture
def schedule(store: Store) -> Schedule:
    return Schedule(store.engine)


async def remind(schedule: Schedule, text: str, at: datetime, **more: object) -> int:
    made = await schedule.remind(text, at=at, timezone="America/Los_Angeles", now=NOW, **more)  # type: ignore[arg-type]
    return made.id


async def test_a_reminder_is_delivered_once_at_its_time_and_then_done(schedule: Schedule) -> None:
    """Spec §9.3 Verify: it runs once, reports once and stops at its stop condition."""
    rid = await remind(schedule, "Call mom", la(2026, 11, 21, 9, 0))
    (active,) = await schedule.active()
    assert (active.next_words, active.schedule) == (
        "Sat 21 Nov 09:00",
        "once, Sat 21 Nov 2026 at 09:00",
    )
    assert await schedule.claim_due(la(2026, 11, 21, 8, 59)) == []  # not yet
    (delivered,) = await schedule.claim_due(
        la(
            2026,
            11,
            21,
            9,
            0,
        )
    )
    assert (delivered.reminder.id, delivered.text, delivered.late) == (rid, "Call mom", False)
    assert await schedule.claim_due(la(2026, 11, 21, 9, 1)) == []  # never twice
    assert await schedule.active() == []
    assert await schedule.next_due() is None


async def test_a_repeat_stops_after_its_count_or_its_last_day(schedule: Schedule) -> None:
    await remind(schedule, "Stretch", la(2026, 11, 21, 9, 0), repeat="daily", max_runs=2)
    await remind(
        schedule, "Water plants", la(2026, 11, 21, 8, 0), repeat="daily", ends_on=date(2026, 11, 22)
    )
    got: list[str] = []
    for day in (21, 22, 23, 24):
        got += [f"{d.text} {day}" for d in await schedule.claim_due(la(2026, 11, day, 10, 0))]
    assert got == ["Water plants 21", "Stretch 21", "Water plants 22", "Stretch 22"]
    assert await schedule.active() == []


async def test_a_repeat_delivered_on_the_dot_comes_next_time_not_again(
    schedule: Schedule,
) -> None:
    await remind(schedule, "Stand up", la(2026, 11, 21, 9, 0), repeat="daily")
    assert len(await schedule.claim_due(la(2026, 11, 21, 9, 0))) == 1
    assert await schedule.claim_due(la(2026, 11, 21, 9, 0)) == []
    (active,) = await schedule.active()
    assert active.next_words == "Sun 22 Nov 09:00"


async def test_a_cancelled_reminder_never_comes(schedule: Schedule) -> None:
    """Spec §9.3 Verify: cancel a job and confirm it does not run again."""
    rid = await remind(schedule, "Gym", la(2026, 11, 21, 7, 0), repeat="weekdays")
    await schedule.cancel(rid)
    assert await schedule.claim_due(la(2026, 12, 31, 12, 0)) == []
    with pytest.raises(ReminderRefusedError, match=f"no reminder {rid} still to come"):
        await schedule.cancel(rid)


async def test_one_missed_while_enzo_was_off_comes_once_late_and_moves_on(
    schedule: Schedule,
) -> None:
    await remind(schedule, "Standup", la(2026, 11, 23, 9, 0), repeat="weekdays")  # Monday
    # The computer slept from Monday morning to Wednesday 10:00.
    (late,) = await schedule.claim_due(la(2026, 11, 25, 10, 0))
    assert late.late
    assert late.due_at == la(2026, 11, 23, 9, 0)  # once, for the first missed time
    (active,) = await schedule.active()
    assert active.next_words == "Thu 26 Nov 09:00"  # the missed ones in between are skipped
    (on_time,) = await schedule.claim_due(la(2026, 11, 26, 9, 0, 30))
    assert not on_time.late


async def test_times_in_the_past_and_weekends(schedule: Schedule) -> None:
    with pytest.raises(
        ReminderRefusedError, match="That time has passed: it is now Fri 20 Nov 09:00"
    ):
        await remind(schedule, "Too late", la(2026, 11, 20, 8, 0))
    # A repeat set for a time already gone today starts with the next one.
    await remind(schedule, "Vitamins", la(2026, 11, 20, 8, 0), repeat="daily")
    # Weekdays set on a Saturday start on Monday.
    await remind(schedule, "Work", la(2026, 11, 21, 9, 0), repeat="weekdays")
    with pytest.raises(ReminderRefusedError, match="would never fire"):
        await remind(
            schedule, "Never", la(2026, 11, 25, 9, 0), repeat="daily", ends_on=date(2026, 11, 24)
        )
    assert [(r.text, r.next_words) for r in await schedule.active()] == [
        ("Vitamins", "Sat 21 Nov 08:00"),
        ("Work", "Mon 23 Nov 09:00"),
    ]


async def test_reminders_survive_a_restart(store: Store, schedule: Schedule) -> None:
    """Spec §9.2 Verify: restart, and the scheduled jobs are still there."""
    await remind(schedule, "Pay rent", la(2026, 12, 1, 9, 0), repeat="monthly")
    restarted = Schedule(store.engine)  # a new gateway on the same database
    (kept,) = await restarted.active()
    assert (kept.text, kept.next_words, kept.schedule) == (
        "Pay rent",
        "Tue 1 Dec 09:00",
        "every month on day 1 at 09:00",
    )


async def test_gateways_racing_never_deliver_the_same_reminder_twice(
    schedule: Schedule, store: Store
) -> None:
    await remind(schedule, "Once only", la(2026, 11, 21, 9, 0))
    when = la(2026, 11, 21, 9, 0)
    racing = [Schedule(store.engine) for _ in range(8)]  # each claims on its own connection
    claimed = await asyncio.gather(*(s.claim_due(when) for s in racing))
    assert sum(len(c) for c in claimed) == 1


# The runner: delivers, tells open apps and the desktop, and survives a bad moment.


async def test_the_runner_delivers_tells_apps_and_notifies(schedule: Schedule) -> None:
    clock = [la(2026, 11, 21, 9, 5)]  # five minutes late
    shown: list[tuple[str, str]] = []

    async def notify(title: str, body: str) -> bool:
        shown.append((title, body))
        return True

    bus = Bus()
    runner = Runner(schedule, bus, notify=notify, clock=lambda: clock[0])
    await remind(schedule, "Call mom", la(2026, 11, 21, 9, 0))
    listening = bus.subscribe()
    waiting = asyncio.ensure_future(anext(listening))
    await asyncio.sleep(0)
    (delivered,) = await runner.tick()
    event = await asyncio.wait_for(waiting, 1)
    assert event.kind == "reminder"
    assert event.data["text"] == "Call mom"
    assert event.data["words"] == "Call mom (late: due Sat 09:00, Enzo wasn't running)"
    assert shown == [("Enzo reminder", "Call mom (late: due Sat 09:00, Enzo wasn't running)")]
    assert delivered.late
    await listening.aclose()  # type: ignore[attr-defined]


async def test_the_runner_wakes_when_a_reminder_is_set_and_keeps_going_after_errors(
    schedule: Schedule,
) -> None:
    now = [datetime.now(UTC)]
    delivered: list[str] = []
    idle: list[bool] = []

    async def notify(_title: str, body: str) -> bool:
        delivered.append(body)
        return True

    runner = Runner(
        schedule,
        Bus(),
        notify=notify,
        tick_s=60,
        clock=lambda: now[0],
        on_idle=lambda: idle.append(True),
    )
    broken = [True]
    real = schedule.claim_due

    async def flaky(when: datetime | None = None) -> list[Delivery]:
        if broken.pop() if broken else False:
            raise RuntimeError("database hiccup")
        return await real(when)

    schedule.claim_due = flaky  # type: ignore[method-assign,assignment]
    runner.start()
    try:
        await asyncio.sleep(0.1)  # the first tick failed; the loop is still alive
        await schedule.remind("Tea", at=now[0] + timedelta(seconds=1), timezone="UTC", now=now[0])
        now[0] += timedelta(seconds=2)
        runner.poke()  # without this it would sleep its full 60 s
        for _ in range(50):
            if delivered:
                break
            await asyncio.sleep(0.05)
    finally:
        await runner.stop()
    assert delivered == ["Tea"]
    assert idle  # nothing left to deliver: an idle gateway may stop


# The tools and the gateway.


async def test_a_reminder_due_soon_comes_on_time_without_a_poke(schedule: Schedule) -> None:
    """The runner sleeps until the next one is due, not a whole tick."""
    delivered: list[str] = []

    async def notify(_title: str, body: str) -> bool:
        delivered.append(body)
        return True

    runner = Runner(schedule, Bus(), notify=notify, tick_s=60)
    now = datetime.now(UTC)
    await schedule.remind("Soon", at=now + timedelta(milliseconds=400), timezone="UTC", now=now)
    runner.start()
    try:
        for _ in range(60):
            if delivered:
                break
            await asyncio.sleep(0.05)
    finally:
        await runner.stop()
    assert delivered == ["Soon"]


async def test_the_tools_set_list_and_cancel_in_the_users_time_zone(schedule: Schedule) -> None:
    poked: list[bool] = []
    registry = ToolRegistry(reminder_tools(schedule, on_set=lambda: poked.append(True)))
    timezone_var.set("Asia/Kolkata")
    try:
        args = {"text": "Call mom", "at": "2030-01-02T09:00"}
        set_ = await registry.execute(
            ToolCall(id="c1", name="remind", arguments_json=json.dumps(args))
        )
    finally:
        timezone_var.set(None)
    assert set_.status == "ok", set_.result.content
    out = json.loads(set_.result.content)
    assert (out["next"], out["timezone"]) == ("Wed 2 Jan 09:00", "Asia/Kolkata")
    assert poked == [True]  # the runner looks at once
    assert registry.activity(ToolCall(id="c1", name="remind", arguments_json=json.dumps(args))) == (
        "Setting a reminder: Call mom"
    )
    listed = await registry.execute(ToolCall(id="c2", name="list_reminders", arguments_json="{}"))
    assert [r["text"] for r in json.loads(listed.result.content)["reminders"]] == ["Call mom"]
    past = {"text": "x", "at": "2001-01-01T09:00"}
    refused = await registry.execute(
        ToolCall(id="c3", name="remind", arguments_json=json.dumps(past))
    )
    assert refused.status == "failed"
    assert "That time has passed" in refused.result.content
    gone = await registry.execute(
        ToolCall(id="c4", name="cancel_reminder", arguments_json=json.dumps({"id": out["id"]}))
    )
    assert gone.status == "ok"
    assert await schedule.active() == []


async def test_set_in_a_chat_and_delivered_into_it(store: Store, schedule: Schedule) -> None:
    at = (
        (datetime.now(UTC) + timedelta(days=1))
        .astimezone(LA)
        .replace(second=0, microsecond=0, tzinfo=None)
    )
    call = ToolCall(
        id="c1",
        name="remind",
        arguments_json=json.dumps({"text": "Call mom", "at": at.isoformat()}),
    )
    brain = ScriptedBrain([Message.assistant(tool_calls=(call,)), Message.assistant("Set.")])
    router = Router(config(), build=lambda _c: brain, available=lambda _c: True)
    bus = Bus()
    runner = Runner(schedule, bus)
    svc = ConversationService(store, router, ToolRegistry(reminder_tools(schedule)))
    async with client(svc, background=Background(schedule, bus, runner)) as c:
        cid = await new_conversation(c)
        turn = {"message": "remind me tomorrow to call mom", "timezone": "America/Los_Angeles"}
        await c.post(f"/conversations/{cid}/turns", json=turn)
        (listed,) = (await c.get("/reminders")).json()["reminders"]
        assert (listed["text"], listed["conversation_id"], listed["timezone"]) == (
            "Call mom",
            cid,
            "America/Los_Angeles",
        )
        assert await runner.tick() == []
        due = datetime.fromisoformat(listed["next_at"])
        (delivered,) = await schedule.claim_due(due)
        assert delivered.after == 4  # after the user's message, the call, its result, the reply
        (shown,) = (await c.get(f"/conversations/{cid}/reminders")).json()
        assert (shown["text"], shown["after"], shown["late"]) == ("Call mom", 4, False)
        assert (await c.get("/reminders")).json()["reminders"] == []
        assert (await c.delete(f"/reminders/{listed['id']}")).status_code == 404
