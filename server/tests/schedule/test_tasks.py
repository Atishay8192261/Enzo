"""Scheduled tasks (spec §9.3): approved once with their exact plan, run as full Enzo turns in
their own chat, reported where they were set, quiet until a watch's end, and stopped by their
stop condition, by repeated failure or by cancelling."""

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from enzo.agent.service import ConversationService
from enzo.brain import Message, RateLimitError, ToolCall
from enzo.brain.fake import ScriptedBrain
from enzo.brain.router import Router
from enzo.context import job_var
from enzo.schedule import Background, Bus, FinishedTasks, Runner, Schedule, schedule_tools
from enzo.schedule.store import Outcome, ScheduleRefusedError, TaskStart
from enzo.schedule.tasks import TaskRunner, first_line, judge, run_note
from enzo.store.repository import Store
from enzo.tools.registry import ToolRegistry
from tests.agent import tools as t
from tests.gateway.test_api import client, config, events, new_conversation

LA = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 11, 20, 17, 0, tzinfo=UTC)  # Fri 20 Nov, 09:00 in Los Angeles


def la(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=LA)


@pytest.fixture
def schedule(store: Store) -> Schedule:
    return Schedule(store.engine)


async def task(schedule: Schedule, text: str = "Check the weather", **more: object) -> int:
    made = await schedule.schedule_task(
        text,
        at=la(21, 8),
        timezone="America/Los_Angeles",
        now=NOW,
        **more,  # type: ignore[arg-type]
    )
    return made.id


# The store: each run once, never on top of the last, and an end it cannot run past.


async def test_a_repeating_task_must_end(schedule: Schedule) -> None:
    with pytest.raises(ScheduleRefusedError, match="A repeating task needs an end"):
        await task(schedule, repeat="daily")
    with pytest.raises(ScheduleRefusedError, match="never keeps those"):
        await task(schedule, "Log in with password: hunter2")
    with pytest.raises(ScheduleRefusedError, match="never keeps those"):  # reminders too (§11.2)
        await schedule.remind(
            "My PIN is 4321", at=la(21, 8), timezone="America/Los_Angeles", now=NOW
        )
    await task(schedule, repeat="daily", max_runs=3)
    await task(schedule, repeat="weekly", ends_on=la(30, 0).date())


async def test_a_run_never_starts_on_top_of_the_last(schedule: Schedule) -> None:
    tid = await task(schedule, repeat="hourly", max_runs=5)
    (first,) = await schedule.claim_tasks(la(21, 8))
    assert (first.job.id, first.run, first.late) == (tid, 1, False)
    assert await schedule.claim_tasks(la(21, 9, 30)) == []  # run 1 is still going
    await schedule.finish_run(tid, due_at=first.due_at, late=False, outcome="reported", summary="x")
    (second,) = await schedule.claim_tasks(la(21, 9, 30))
    assert second.run == 2
    assert second.due_at == la(21, 9)  # the slot it missed while busy, now


async def test_three_failures_in_a_row_stop_a_task(schedule: Schedule) -> None:
    tid = await task(schedule, repeat="hourly", max_runs=10)
    ends = []
    for hour in (8, 9, 10):
        (start,) = await schedule.claim_tasks(la(21, hour))
        ends.append(
            await schedule.finish_run(
                tid, due_at=start.due_at, late=False, outcome="failed", summary="no account"
            )
        )
    assert [e.stopped for e in ends] == [False, False, True]
    assert await schedule.active() == []


async def test_a_success_resets_the_failure_count(schedule: Schedule) -> None:
    tid = await task(schedule, repeat="hourly", max_runs=10)
    runs: list[tuple[int, Outcome]] = [
        (8, "failed"),
        (9, "failed"),
        (10, "reported"),
        (11, "failed"),
    ]
    for hour, outcome in runs:
        (start,) = await schedule.claim_tasks(la(21, hour))
        end = await schedule.finish_run(
            tid, due_at=start.due_at, late=False, outcome=outcome, summary="x"
        )
    assert not end.stopped


async def test_a_run_cut_short_by_a_restart_is_recorded_and_the_next_runs(
    schedule: Schedule, store: Store
) -> None:
    tid = await task(schedule, repeat="hourly", max_runs=5)
    await schedule.claim_tasks(la(21, 8))  # running when the gateway died
    restarted = Schedule(store.engine)
    (ended,) = await restarted.recover(la(21, 8, 30))
    assert (ended.job.id, ended.outcome) == (tid, "failed")
    (again,) = await restarted.claim_tasks(la(21, 9))
    assert again.run == 2


# Judging a run, and what the brain is told.


def test_what_the_brain_is_told_about_a_run() -> None:
    job = _job(stop_when="a table for two is free on Friday", max_runs=12, repeat="hourly")
    note = run_note(job, TaskStart(job, la(21, 8), late=True, run=3))
    assert note.startswith("Scheduled task #7, run 3 of at most 12, set by the user on Fri 20 Nov.")
    assert "The task is done when: a table for two is free on Friday" in note
    assert "call finish_task" in note
    assert "This run is late: it was due Sat 08:00" in note


def test_how_a_run_is_judged() -> None:
    plain, watch = _job(), _job(stop_when="it rains")
    assert judge(plain, _done("final_answer", "## Sunny, 21°C\nmore"), None) == (
        "reported",
        "Sunny, 21°C",
    )
    assert judge(watch, _done("final_answer", "Not yet: dry all week."), None) == (
        "quiet",
        "Not yet: dry all week.",
    )
    assert judge(watch, _done("final_answer", "Rain at 3pm."), "rain forecast")[0] == "finished"
    assert judge(plain, _done("approval_pending", ""), None)[0] == "needs_approval"
    assert judge(plain, _done("brain_failure", "No model account"), None) == (
        "failed",
        "No model account",
    )
    assert judge(plain, None, None)[0] == "failed"
    assert first_line("\n\n- **Rain** at 3pm\n") == "Rain at 3pm"
    assert first_line("1. `Bus 12` leaves at 5") == "Bus 12 leaves at 5"
    assert first_line("*Note:* it is 5 * 3") == "*Note:* it is 5 * 3"  # single stars stay


# End to end: approved in a chat, run in its own chat, reported back.


def runner_for(store: Store, brain: ScriptedBrain, schedule: Schedule) -> tuple:  # type: ignore[type-arg]
    shown: list[str] = []

    async def notify(_title: str, body: str) -> bool:
        shown.append(body)
        return True

    finished = FinishedTasks()
    router = Router(config(), build=lambda _c: brain, available=lambda _c: True)
    registry = ToolRegistry([*schedule_tools(schedule, finished), *t.registry()._tools.values()])
    svc = ConversationService(store, router, registry)
    bus = Bus()
    runner = Runner(schedule, bus)
    tasks = TaskRunner(schedule, svc, bus, finished, notify=notify, busy_retry_s=0.01)
    runner.connect(tasks.start, lambda: tasks.running > 0)
    return svc, runner, tasks, bus, shown


def plan(**args: object) -> ToolCall:
    at = (datetime.now(UTC) + timedelta(days=1)).astimezone(LA).replace(second=0, microsecond=0)
    body = {
        "instruction": "Check the weather in Austin",
        "at": at.replace(tzinfo=None).isoformat(),
        **args,
    }
    return ToolCall(id="p1", name="schedule_task", arguments_json=json.dumps(body))


async def test_approved_once_run_in_its_own_chat_and_reported_where_it_was_set(
    store: Store, schedule: Schedule
) -> None:
    """Spec §9.3 Verify: it runs once, reports once, and stops at its stop condition."""
    brain = ScriptedBrain(
        [
            Message.assistant(tool_calls=(plan(),)),
            Message.assistant("Scheduled for tomorrow at that time."),
            Message.assistant("Sunny tomorrow, 21°C. No rain."),  # the run itself
        ]
    )
    svc, runner, tasks, bus, shown = runner_for(store, brain, schedule)
    async with client(svc, background=Background(schedule, bus, runner)) as c:
        cid = await new_conversation(c)
        got = events(
            (
                await c.post(
                    f"/conversations/{cid}/turns",
                    json={
                        "message": "check the weather tomorrow",
                        "timezone": "America/Los_Angeles",
                    },
                )
            ).text
        )
        (card,) = [d for k, d in got if k == "approval_needed"]
        assert card["summary"].startswith("Schedule a task, once, ")
        assert card["summary"].endswith(": Check the weather in Austin")
        assert await schedule.active() == []  # nothing is scheduled before the user approves
        await c.post(f"/conversations/{cid}/approvals/{card['id']}", json={"approved": True})
        events((await c.post(f"/conversations/{cid}/resume")).text)
        (job,) = (await c.get("/scheduled")).json()["scheduled"]
        assert (job["kind"], job["conversation_id"]) == ("task", cid)

        (start,) = await schedule.claim_tasks(datetime.fromisoformat(job["next_at"]))
        end = await tasks.run(start)
        assert (end.outcome, end.stopped) == ("reported", True)  # once: that was its run
        assert shown == [
            "Check the weather in Austin: Sunny tomorrow, 21°C. No rain. That was its last run."
        ]

        (notice,) = (await c.get(f"/conversations/{cid}/scheduled")).json()
        assert (notice["kind"], notice["outcome"], notice["text"]) == (
            "task",
            "reported",
            "Sunny tomorrow, 21°C. No rain.",
        )
        own = notice["chat_id"]
        assert own is not None
        assert own != cid
        stored = (await c.get(f"/conversations/{own}/messages")).json()
        assert stored[0]["origin"] == "schedule"  # the apps show a run, not the user typing
        assert stored[0]["parts"][0]["text"] == "Check the weather in Austin"
        assert "Scheduled task #" in stored[0]["parts"][1]["text"]  # the brain was told
        assert (await c.get(f"/conversations/{cid}/messages")).json()[0].get("origin") is None
        assert (await c.get("/scheduled")).json()["scheduled"] == []


async def test_a_watch_stays_quiet_until_its_end_then_reports_once_and_stops(
    store: Store, schedule: Schedule
) -> None:
    finish = ToolCall(
        id="f1", name="finish_task", arguments_json=json.dumps({"why": "table at 7pm"})
    )
    brain = ScriptedBrain(
        [
            Message.assistant("Not yet: nothing free on Friday."),
            Message.assistant(tool_calls=(finish,)),
            Message.assistant("A table for two is free Friday at 7pm."),
        ]
    )
    _svc, _runner, tasks, _bus, shown = runner_for(store, brain, schedule)
    tid = await task(
        schedule,
        "Look for a table for two on Friday",
        repeat="hourly",
        max_runs=24,
        stop_when="a table is free",
    )
    first = await tasks.run((await schedule.claim_tasks(la(21, 8)))[0])
    second = await tasks.run((await schedule.claim_tasks(la(21, 9)))[0])
    assert (first.outcome, first.stopped) == ("quiet", False)
    assert (second.outcome, second.stopped) == ("finished", True)
    found = "Look for a table for two on Friday: A table for two is free Friday at 7pm."
    assert shown == [f"{found} Done: it stops now."]  # the quiet run did not interrupt anyone
    assert await schedule.claim_tasks(la(22, 12)) == []
    assert (await schedule.get(tid)).status == "done"  # type: ignore[union-attr]


async def test_a_run_that_needs_an_approval_waits_for_the_user(
    store: Store, schedule: Schedule
) -> None:
    email = ToolCall(id="e1", name="send_email", arguments_json='{"value": "the report"}')
    brain = ScriptedBrain([Message.assistant(tool_calls=(email,))])
    _svc, _runner, tasks, _bus, shown = runner_for(store, brain, schedule)
    await task(schedule, "Email the weekly report to Bob")
    end = await tasks.run((await schedule.claim_tasks(la(21, 8)))[0])
    assert end.outcome == "needs_approval"
    assert t.ran == []  # a schedule can never send on its own (§12.3)
    waits = "Email the weekly report to Bob: It waits for your approval to go on."
    assert shown == [f"{waits} That was its last run."]


async def test_a_failing_run_is_reported_and_the_task_goes_on(
    store: Store, schedule: Schedule
) -> None:
    brain = ScriptedBrain([RateLimitError("slow down", provider="fake", model="m-main")] * 2)
    _svc, _runner, tasks, _bus, shown = runner_for(store, brain, schedule)
    await task(schedule, repeat="hourly", max_runs=5)
    end = await tasks.run((await schedule.claim_tasks(la(21, 8)))[0])
    assert (end.outcome, end.stopped) == ("failed", False)
    assert len(shown) == 1


async def test_the_runner_starts_due_tasks_and_is_busy_while_they_run(
    store: Store, schedule: Schedule
) -> None:
    brain = ScriptedBrain([Message.assistant("Done.")])
    _svc, runner, tasks, _bus, _shown = runner_for(store, brain, schedule)
    await schedule.schedule_task(
        "Tidy my notes", at=datetime.now(UTC) + timedelta(milliseconds=50), timezone="UTC"
    )
    import asyncio  # noqa: PLC0415

    await asyncio.sleep(0.1)
    await runner.tick()
    assert tasks.running == 1  # started, without waiting for it
    for _ in range(100):
        if tasks.running == 0:
            break
        await asyncio.sleep(0.02)
    assert tasks.running == 0
    assert await schedule.active() == []


async def test_finish_task_only_works_inside_a_run(schedule: Schedule) -> None:
    finished = FinishedTasks()
    registry = ToolRegistry(schedule_tools(schedule, finished))
    call = ToolCall(id="f1", name="finish_task", arguments_json=json.dumps({"why": "done now"}))
    outside = await registry.execute(call)
    assert outside.status == "failed"
    assert "Only a scheduled task's run can finish it" in outside.result.content
    token = job_var.set(7)
    try:
        inside = await registry.execute(call)
    finally:
        job_var.reset(token)
    assert inside.status == "ok"
    assert finished.take(7) == "done now"


async def test_a_cancelled_task_never_runs_again(schedule: Schedule) -> None:
    tid = await task(schedule, repeat="daily", max_runs=7)
    await schedule.cancel(tid)
    assert await schedule.claim_tasks(la(30, 12)) == []


def _job(**more: object):  # type: ignore[no-untyped-def]
    from enzo.schedule.store import Job  # noqa: PLC0415

    fields = {
        "id": 7,
        "kind": "task",
        "text": "Check the weather",
        "timezone": "America/Los_Angeles",
        "first_at": la(21, 8),
        "repeat": "once",
        "every": 1,
        "ends_on": None,
        "max_runs": None,
        "runs": 0,
        "next_run_at": la(21, 8),
        "status": "active",
        "conversation_id": None,
        "created_at": la(20, 9),
        **more,
    }
    return Job(**fields)  # type: ignore[arg-type]


def _done(stop: str, text: str):  # type: ignore[no-untyped-def]
    from enzo.agent.loop import TurnOutcome  # noqa: PLC0415

    return TurnOutcome(stop=stop, text=text, messages=(), steps=1)  # type: ignore[arg-type]


async def test_all_of_a_tasks_runs_happen_in_its_one_chat(store: Store, schedule: Schedule) -> None:
    brain = ScriptedBrain([Message.assistant("Run one."), Message.assistant("Run two.")])
    _svc, _runner, tasks, _bus, _shown = runner_for(store, brain, schedule)
    tid = await task(schedule, repeat="hourly", max_runs=5)
    await tasks.run((await schedule.claim_tasks(la(21, 8)))[0])
    chat = (await schedule.get(tid)).chat_id  # type: ignore[union-attr]
    await tasks.run((await schedule.claim_tasks(la(21, 9)))[0])
    assert chat is not None
    assert (await schedule.get(tid)).chat_id == chat  # type: ignore[union-attr]
    assert await store.scheduled(chat) == {0, 2}  # both runs, one after the other


async def test_a_run_that_breaks_never_finishes_the_next_one(
    store: Store, schedule: Schedule, monkeypatch: pytest.MonkeyPatch
) -> None:
    brain = ScriptedBrain([Message.assistant("Still nothing.")])
    _svc, _runner, tasks, _bus, _shown = runner_for(store, brain, schedule)
    tid = await task(schedule, repeat="hourly", max_runs=5, stop_when="it rains")
    real = TaskRunner._turn

    async def breaks(self: TaskRunner, *_args: object) -> object:
        self._finished.mark(tid, "rain")  # it said "done"…
        raise RuntimeError("…and then the run broke")

    monkeypatch.setattr(TaskRunner, "_turn", breaks)
    broken = await tasks.run((await schedule.claim_tasks(la(21, 8)))[0])
    monkeypatch.setattr(TaskRunner, "_turn", real)
    after = await tasks.run((await schedule.claim_tasks(la(21, 9)))[0])
    assert (broken.outcome, after.outcome) == ("failed", "quiet")  # not "finished"


async def test_a_gateway_running_a_task_is_never_idle(schedule: Schedule) -> None:
    import asyncio  # noqa: PLC0415

    idle: list[bool] = []
    busy = [True]
    runner = Runner(schedule, Bus(), tick_s=0.05, on_idle=lambda: idle.append(True))
    runner.connect(lambda _s: None, lambda: busy[0])
    runner.start()
    try:
        await asyncio.sleep(0.2)
        assert idle == []  # nothing left to schedule, but a run is still going
        busy[0] = False
        for _ in range(40):
            if idle:
                break
            await asyncio.sleep(0.05)
    finally:
        await runner.stop()
    assert idle
