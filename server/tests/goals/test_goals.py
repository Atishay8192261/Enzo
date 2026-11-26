"""Goals (spec §6.4, §6.5, §9.2, §11.3): records with steps, changed only on evidence, closed when
the evidence shows they are done (and then they stop reminding), never closed by one step, in
every message while open, and still there after a restart."""

import io
import json
import stat
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from enzo import logs
from enzo.agent.service import ConversationService
from enzo.brain import Message, ToolCall, ToolResult
from enzo.brain.fake import ScriptedBrain
from enzo.brain.router import Router
from enzo.goals import GoalRefusedError, Goals, goal_tools, goals_note
from enzo.schedule import Background, Bus, FinishedTasks, Runner, Schedule, schedule_tools
from enzo.schedule.store import ScheduleRefusedError
from enzo.store.repository import Store
from enzo.tools.files import FileToolError, Workspace
from enzo.tools.registry import ToolRegistry
from tests.gateway.test_api import client, config, events, new_conversation
from tests.test_logs import reset

LA = "America/Los_Angeles"


@pytest.fixture
def schedule(store: Store) -> Schedule:
    return Schedule(store.engine)


@pytest.fixture
def goals(store: Store, schedule: Schedule, tmp_path: Path) -> Goals:
    return Goals(store.engine, tmp_path / "Enzo", schedule)


def call(call_id: str, name: str, **args: object) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments_json=json.dumps(args, default=str))


def service(
    store: Store, goals: Goals, schedule: Schedule, brain: ScriptedBrain
) -> tuple[ConversationService, Background]:
    router = Router(config(), build=lambda _c: brain, available=lambda _c: True)
    tools = ToolRegistry([*goal_tools(goals), *schedule_tools(schedule, FinishedTasks())])
    svc = ConversationService(store, router, tools, goals=goals)
    return svc, Background(schedule, Bus(), Runner(schedule, Bus()))


def goals_part(message: Message) -> str:
    """The open goals that rode with a user message (in its memory part), or ""."""
    texts = [p.text for p in message.parts if hasattr(p, "text")]
    return next((t for t in texts if t.startswith("Open goals")), "")


def results(brain: ScriptedBrain, n: int) -> list[dict[str, Any]]:
    """What the brain got back from its tool calls, at its n-th call."""
    parts = brain.calls[n].messages[-1].parts
    return [json.loads(p.content) for p in parts if isinstance(p, ToolResult)]


async def test_a_goal_completed_is_marked_done_and_stops_reminding(
    store: Store, goals: Goals, schedule: Schedule
) -> None:
    """Spec §6.4 "Verify" 1: create a goal, complete it, confirm Enzo marks it complete and stops
    reminding about it. Enzo closes it unasked: the user only says what happened."""
    monday = (datetime.now(UTC) + timedelta(days=3)).replace(tzinfo=None, microsecond=0)
    brain = ScriptedBrain(
        [
            Message.assistant(
                tool_calls=(
                    call(
                        "c1",
                        "create_goal",
                        title="Renew my passport",
                        steps=["Get photos", "Fill in the form", "Mail it"],
                        due=str(date.today() + timedelta(days=30)),
                        folder="passport",
                    ),
                )
            ),
            Message.assistant(
                tool_calls=(
                    call(
                        "c2",
                        "remind",
                        text="Work on the passport",
                        at=monday.isoformat(),
                        repeat="weekly",
                        goal=1,
                    ),
                )
            ),
            Message.assistant("Tracking it, with a reminder every week."),
            Message.assistant(
                tool_calls=(
                    call(
                        "c3",
                        "close_goal",
                        goal=1,
                        outcome="done",
                        evidence="The user said their new passport arrived in the mail today.",
                        basis="user_confirmed",
                    ),
                )
            ),
            Message.assistant("Congratulations: that goal is done, and its reminders stopped."),
            Message.assistant("Nothing else is open."),
        ]
    )
    svc, bg = service(store, goals, schedule, brain)
    async with client(svc, background=bg) as c:
        chat = await new_conversation(c)
        said = "I need to renew my passport within a month; nudge me weekly"
        await c.post(f"/conversations/{chat}/turns", json={"message": said, "timezone": LA})
        assert [j.goal_id for j in await schedule.active()] == [1]
        await c.post(f"/conversations/{chat}/turns", json={"message": "My new passport came!"})
        last = await c.post(f"/conversations/{chat}/turns", json={"message": "What's open?"})
        assert events(last.text)[-1][1]["text"] == "Nothing else is open."
        closed = (await c.get("/goals", params={"which": "closed"})).json()
        still = (await c.get("/goals")).json()
        scheduled = (await c.get("/scheduled")).json()

    (goal,) = closed["goals"]
    assert (goal["id"], goal["status"], goal["title"]) == (1, "done", "Renew my passport")
    assert goal["closing"]["basis"] == "user_confirmed"
    assert goal["closing"]["evidence"].startswith("The user said their new passport arrived")
    assert goal["closing"]["said"].startswith("you confirmed, ")
    assert still["goals"] == []
    assert scheduled["scheduled"] == []  # it stops reminding (§6.4)
    (stopped,) = results(brain, 4)
    assert [s["kind"] for s in stopped["stopped"]] == ["reminder"]
    # While open, the goal rode with each message; once done, it no longer does.
    assert "#1 Renew my passport" in goals_part(brain.calls[3].messages[-1])
    assert goals_part(brain.calls[5].messages[-1]) == ""
    assert "Open goals" not in str(brain.calls[0].messages[-1])  # none before the first


async def test_one_step_done_never_completes_the_goal(
    store: Store, goals: Goals, schedule: Schedule
) -> None:
    """Spec §6.4 "Verify" 2: complete one step, and the goal is not marked complete, even when
    the brain tries to close it on that step's evidence."""
    await goals.create("Renew my passport", steps=["Get photos", "Fill in the form", "Mail it"])
    brain = ScriptedBrain(
        [
            Message.assistant(
                tool_calls=(
                    call(
                        "c1",
                        "record_goal_progress",
                        goal=1,
                        done_steps=[1],
                        evidence="The user said they got their passport photos today.",
                        basis="user_confirmed",
                    ),
                    call(
                        "c2",
                        "close_goal",
                        goal=1,
                        outcome="done",
                        evidence="The photos are done.",
                        basis="verified",
                        source="the user's message",
                    ),
                )
            ),
            Message.assistant("Photos done: two steps to go."),
        ]
    )
    svc, bg = service(store, goals, schedule, brain)
    async with client(svc, background=bg) as c:
        chat = await new_conversation(c)
        await c.post(f"/conversations/{chat}/turns", json={"message": "Got my photos!"})
    progress, refused = results(brain, 1)
    assert progress["status"] == "open"
    assert progress["remaining"] == 2
    assert progress["next"] == "2 step(s) remain, so the goal stays open."
    assert "2 step(s) of goal #1 remain" in refused["message"]
    (goal,) = await goals.open()
    assert goal.status == "open"
    assert [s.done for s in goal.steps] == [True, False, False]
    proof = goal.steps[0].evidence
    assert proof is not None
    assert (proof.basis, proof.evidence) == (
        "user_confirmed",
        "The user said they got their passport photos today.",
    )


async def test_every_change_needs_evidence_and_a_plan_is_not_a_result(goals: Goals) -> None:
    goal = await goals.create("Ship the side project", steps=["Write it", "Launch it"])
    with pytest.raises(GoalRefusedError, match="needs its evidence"):
        await goals.progress(goal.id, done=[1])
    with pytest.raises(GoalRefusedError, match="needs its source"):
        await goals.progress(goal.id, done=[1], evidence="It is written.", basis="verified")
    with pytest.raises(GoalRefusedError, match="planned or proposed"):
        await goals.progress(
            goal.id, done=[1], evidence="The user will finish it tonight.", basis="user_confirmed"
        )
    with pytest.raises(GoalRefusedError, match="not by a date"):
        await goals.progress(
            goal.id,
            done=[1],
            evidence="Time is up.",
            basis="timeline_ended",
        )
    with pytest.raises(GoalRefusedError, match="has no step 3"):
        await goals.progress(goal.id, done=[3], evidence="Done.", basis="user_confirmed")
    with pytest.raises(GoalRefusedError, match="never keeps those"):
        await goals.progress(
            goal.id, done=[1], evidence="Logged in with password: hunter2", basis="user_confirmed"
        )
    assert [s.done for s in (await goals.open())[0].steps] == [False, False]  # nothing stuck
    done = await goals.progress(
        goal.id,
        done=[1],
        evidence="The repository shows the first release.",
        basis="verified",
        source="https://example.com/releases/1",
        happened_on=date(2026, 11, 20),
    )
    with pytest.raises(GoalRefusedError, match="already done"):
        await goals.progress(goal.id, done=[1], evidence="Again.", basis="user_confirmed")
    proof = done.steps[0].evidence
    assert proof is not None
    assert (proof.source, proof.happened_on) == (
        "https://example.com/releases/1",
        date(2026, 11, 20),
    )
    # Every step done still does not close it: the outcome itself needs evidence.
    every = await goals.progress(goal.id, done=[2], evidence="It launched.", basis="user_confirmed")
    assert every.status == "open"
    with pytest.raises(GoalRefusedError, match="planned or proposed"):
        await goals.close(
            goal.id, outcome="done", evidence="It should be fine.", basis="verified", source="x"
        )


async def test_closing_rules(goals: Goals, schedule: Schedule) -> None:
    soon = date(2026, 12, 1)
    goal = await goals.create("Run a 10k", steps=["Train"], due_on=soon)
    with pytest.raises(GoalRefusedError, match="time has not run out"):
        await goals.close(
            goal.id, outcome="dropped", evidence="Out of time.", basis="timeline_ended", today=soon
        )
    with pytest.raises(GoalRefusedError, match="1 step"):
        await goals.close(
            goal.id,
            outcome="done",
            evidence="Ran it.",
            basis="timeline_ended",
            today=soon + timedelta(days=1),
        )
    # The user's own word closes it, steps or not; a passed date can close it as given up.
    late = await goals.close(
        goal.id,
        outcome="dropped",
        evidence="The race day passed.",
        basis="timeline_ended",
        today=soon + timedelta(days=1),
    )
    assert late.goal.status == "dropped"
    with pytest.raises(GoalRefusedError, match="already dropped"):
        await goals.close(goal.id, outcome="done", evidence="Ran it.", basis="user_confirmed")
    other = await goals.create("Learn to bake bread", steps=["Buy flour", "Bake"])
    finished = await goals.close(
        other.id, outcome="done", evidence="The user said they baked it.", basis="user_confirmed"
    )
    assert finished.goal.status == "done"
    with pytest.raises(ScheduleRefusedError, match="no open goal 2"):
        await schedule.remind(
            "Bake", at=datetime.now(UTC) + timedelta(days=1), timezone=LA, goal=other.id
        )
    with pytest.raises(GoalRefusedError, match="no goal #99"):
        await goals.progress(99, add=["x"])


async def test_titles_steps_and_folders_are_checked(goals: Goals) -> None:
    with pytest.raises(GoalRefusedError, match="never keeps those"):
        await goals.create("Card 4111 1111 1111 1111 paid off")
    with pytest.raises(GoalRefusedError, match="inside the user's Enzo folder"):
        await goals.create("Escape", path="../secrets")
    with pytest.raises(GoalRefusedError, match="inside the user's Enzo folder"):
        await goals.create("Escape", path="/etc")
    made = await goals.create("Plan the trip", path="~/Enzo/trip/")
    assert made.path == "trip"
    with pytest.raises(GoalRefusedError, match="already an open goal #1"):
        await goals.create("plan the trip")
    more = await goals.progress(made.id, add=["Book flights", "Book a hotel"])
    assert [(s.number, s.text) for s in more.steps] == [(1, "Book flights"), (2, "Book a hotel")]


async def test_goals_survive_a_restart_with_the_same_status(
    store: Store, goals: Goals, database_url: str
) -> None:
    """Spec §9.2 and §11.3: create a goal, restart Enzo, the goal is there with its status."""
    open_one = await goals.create("Renew my passport", steps=["Photos", "Form"])
    await goals.progress(open_one.id, done=[1], evidence="Got them.", basis="user_confirmed")
    done_one = await goals.create("Bake bread")
    await goals.close(done_one.id, outcome="done", evidence="Baked.", basis="user_confirmed")
    again = Store.from_url(database_url)  # a new gateway: nothing carried over in memory
    try:
        restarted = Goals(again.engine)
        (still_open,) = await restarted.open()
        (still_done,) = await restarted.closed()
    finally:
        await again.close()
    assert (still_open.id, still_open.status) == (open_one.id, "open")
    assert [s.done for s in still_open.steps] == [True, False]
    assert (still_done.id, still_done.status) == (done_one.id, "done")
    closing = still_done.closing
    assert closing is not None
    assert closing.evidence == "Baked."


def test_what_the_brain_sees_of_open_goals() -> None:
    from enzo.goals.store import Evidence, Goal, Step  # noqa: PLC0415

    now = datetime(2026, 11, 25, tzinfo=UTC)
    proof = Evidence("step_done", 1, "Got them.", "user_confirmed", None, None, now, None)
    goal = Goal(
        id=3,
        title="Renew my passport",
        status="open",
        due_on=date(2026, 11, 24),
        path="passport",
        created_at=now,
        closed_at=None,
        conversation_id=None,
        chat=None,
        steps=(Step(1, "Photos", now, proof), Step(2, "Form", None)),
        history=(proof,),
    )
    note = goals_note([goal], date(2026, 11, 25))
    assert note is not None
    assert note.startswith("Open goals, from Enzo's records: the user's commitments, not")
    assert "- #3 Renew my passport · due 2026-11-24 (passed) · in ~/Enzo/passport" in note
    assert "  done: 1. Photos (user confirmed, 2026-11-25)" in note
    assert "  left: 2. Form" in note
    assert goals_note([], date(2026, 11, 25)) is None
    many = goals_note([goal] * 12, date(2026, 11, 25))
    assert many is not None
    assert "and 2 more open goals" in many


async def test_goals_md_is_a_read_only_copy_and_a_hand_edit_is_kept_aside(
    goals: Goals, tmp_path: Path
) -> None:
    goal = await goals.create("Renew my passport", steps=["Photos", "Form"], path="passport")
    await goals.progress(goal.id, done=[1], evidence="Got them.", basis="user_confirmed")
    path = tmp_path / "Enzo" / "Goals.md"
    text = path.read_text()
    assert "### Renew my passport (#1)" in text
    assert "- [x] Photos — you confirmed, " in text
    assert "- [ ] Form" in text
    assert "in ~/Enzo/passport" in text or "in " + str(tmp_path) in text
    assert stat.S_IMODE(path.stat().st_mode) == 0o400  # an editor warns before saving over it
    assert await goals.check_file() is None  # untouched
    path.chmod(0o600)
    path.write_text(text.replace("- [ ] Form", "- [x] Form"))
    note = await goals.check_file()
    assert note is not None
    assert "read-only copy" in note
    (kept,) = path.parent.glob("Goals (your edit *).md")
    assert kept.name in note
    assert "- [x] Form" in kept.read_text()  # the user's words are not lost
    assert "- [ ] Form" in path.read_text()  # and the file is Enzo's records again
    assert [s.done for s in (await goals.open())[0].steps] == [True, False]
    assert await goals.check_file() is None
    with pytest.raises(FileToolError, match="read-only copy of the user's goals"):
        Workspace(tmp_path / "Enzo").create("goals.md", "- [x] everything")


async def test_a_hand_edit_of_goals_md_reaches_the_brain(
    store: Store, goals: Goals, schedule: Schedule, tmp_path: Path
) -> None:
    await goals.create("Renew my passport")
    path = tmp_path / "Enzo" / "Goals.md"
    path.chmod(0o600)
    path.write_text(path.read_text() + "\nDone!\n")
    brain = ScriptedBrain([Message.assistant("What did you want to change?")])
    svc, bg = service(store, goals, schedule, brain)
    async with client(svc, background=bg) as c:
        chat = await new_conversation(c)
        await c.post(f"/conversations/{chat}/turns", json={"message": "hi"})
    parts = [p.text for p in brain.calls[0].messages[-1].parts if hasattr(p, "text")]
    assert any(p.startswith("Goals.md was edited by hand") for p in parts)


async def test_goal_events_reach_the_log_as_numbers_never_words(goals: Goals) -> None:
    out = io.StringIO()
    logs.configure(stream=out)
    try:
        goal = await goals.create("Secret surprise party", steps=["Invite people"])
        await goals.progress(goal.id, done=[1], evidence="Invited.", basis="user_confirmed")
        await goals.close(goal.id, outcome="done", evidence="Party held.", basis="user_confirmed")
    finally:
        reset()
    lines = [json.loads(x) for x in out.getvalue().splitlines()]
    assert [x["event"] for x in lines] == ["goal.created", "goal.progress", "goal.closed"]
    assert lines[-1]["outcome"] == "done"
    assert "surprise" not in out.getvalue().lower()
    assert "invited" not in out.getvalue().lower()


async def test_the_tools_say_what_they_do_in_plain_words(goals: Goals) -> None:
    registry = ToolRegistry(goal_tools(goals))
    made = await registry.execute(
        call("c1", "create_goal", title="Renew my passport", steps=["Photos"])
    )
    assert made.status == "ok"
    assert registry.activity(call("c1", "create_goal", title="Renew my passport")) == (
        "Tracking a goal: Renew my passport"
    )
    assert (
        registry.activity(
            call(
                "c2",
                "close_goal",
                goal=1,
                outcome="done",
                evidence="It arrived.",
                basis="user_confirmed",
            )
        )
        == "Marking goal #1 done"
    )
    bad = await registry.execute(
        call(
            "c3",
            "record_goal_progress",
            goal=1,
            done_steps=[1],
            evidence="I think so.",
            basis="inferred",
        )
    )
    assert bad.status == "invalid_arguments"  # an inference is not evidence for a change
    listed = await registry.execute(call("c4", "list_goals", include_closed=True))
    (goal,) = json.loads(listed.result.content)["goals"]
    assert (goal["title"], goal["remaining"]) == ("Renew my passport", 1)


async def test_today_is_the_users_own_date(goals: Goals) -> None:
    from enzo.context import timezone_var  # noqa: PLC0415
    from enzo.goals.store import user_today  # noqa: PLC0415

    token = timezone_var.set("Pacific/Kiritimati")  # UTC+14: often a day ahead
    try:
        assert user_today() == datetime.now(ZoneInfo("Pacific/Kiritimati")).date()
    finally:
        timezone_var.reset(token)
