"""The brain's scheduling tools (spec §7 batch 4, §9.3): remind, schedule a task, finish a task,
list and cancel.

- A reminder needs no approval: it only ever messages the user, the chat shows it as a step
  ("Setting a reminder: …"), and /scheduled lists and cancels it.
- A task needs the user's approval of its exact plan (what, when, until when), like any action
  that acts on its own: each run spends their model quota, and a web page must never be able to
  start one (spec §12.4: "a new scheduled job … the user did not authorise").
- ``finish_task`` works only inside a scheduled run: the run says its task's stop is met.

Times are the user's local times; the zone comes from their app with the message, else this
computer's own.
"""

from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from enzo.context import job_var, timezone_var
from enzo.schedule.recurrence import Repeat, describe, local_zone, zone
from enzo.schedule.store import Job, Schedule, ScheduleRefusedError
from enzo.tools.registry import Tool, plain_words, tool

ACTIVITY_CHARS = 60


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


_AT: Any = Field(
    description=(
        "When, in the user's local time, like 2026-11-21T09:00 (no zone needed). For 'in 20 "
        "minutes', add to the time the message was sent. For a repeat, the first time."
    )
)
_REPEAT: Any = Field(
    default="once",
    description="once, or hourly, daily, weekdays (Monday to Friday), weekly, monthly or yearly.",
)
_EVERY: Any = Field(
    default=1, ge=1, le=366, description="Repeat every this many hours/days/weeks/months/years."
)
_UNTIL: Any = Field(default=None, description="For a repeat: the last local date it may run on.")
_TIMES: Any = Field(default=None, ge=1, le=1000, description="For a repeat: stop after this many.")
_GOAL: Any = Field(
    default=None,
    ge=1,
    description="A check-in on one of the user's goals: its number. Closing the goal stops it.",
)


class RemindParams(_Params):
    text: str = Field(
        min_length=1,
        max_length=200,
        description="What to remind the user of, short, as they would say it: 'Call mom'.",
    )
    at: datetime = _AT
    repeat: Repeat = _REPEAT
    every: int = _EVERY
    until: date | None = _UNTIL
    times: int | None = _TIMES
    goal: int | None = _GOAL


class TaskParams(_Params):
    instruction: str = Field(
        min_length=3,
        max_length=500,
        description=(
            "What Enzo does on each run, as a clear instruction: 'Check the weather in Austin "
            "for tomorrow and tell me if it will rain.'"
        ),
    )
    at: datetime = _AT
    repeat: Repeat = _REPEAT
    every: int = _EVERY
    until: date | None = Field(
        default=None, description="For a repeat: the last local date it may run on."
    )
    times: int | None = Field(
        default=None,
        ge=1,
        le=200,
        description="For a repeat: stop after this many runs. A repeat needs times or until.",
    )
    stop_when: str | None = Field(
        default=None,
        max_length=200,
        description=(
            "For a watch ('tell me when…'): when the task is done, e.g. 'a table for two is "
            "free on Friday'. Runs before that stay quiet; the run that finds it reports."
        ),
    )
    goal: int | None = _GOAL


class ScheduledOut(BaseModel):
    id: int
    kind: str
    text: str
    #: "Fri 21 Nov 09:00", in the user's zone.
    next: str | None
    #: "every weekday at 09:00, until 1 Dec 2026".
    schedule: str
    stop_when: str | None
    timezone: str
    #: The goal it checks in on: closing the goal cancels it.
    goal: int | None = None


class ListParams(_Params):
    pass


class ScheduledList(BaseModel):
    scheduled: list[ScheduledOut]


class CancelParams(_Params):
    id: int = Field(ge=1, description="The number of the reminder or task to cancel.")


class FinishParams(_Params):
    why: str = Field(
        min_length=3, max_length=200, description="What you found that meets the task's end."
    )


class Finished(BaseModel):
    task: int
    why: str


class FinishedTasks:
    """Tasks a run found done: the task runner reads it when the run ends."""

    def __init__(self) -> None:
        self.found: dict[int, str] = {}

    def mark(self, job_id: int, why: str) -> None:
        self.found[job_id] = why

    def take(self, job_id: int) -> str | None:
        return self.found.pop(job_id, None)


def scheduled_out(j: Job) -> ScheduledOut:
    return ScheduledOut(
        id=j.id,
        kind=j.kind,
        text=j.text,
        next=j.next_words,
        schedule=j.schedule,
        stop_when=j.stop_when,
        timezone=j.timezone,
        goal=j.goal_id,
    )


def user_zone() -> str:
    """The zone the user's app sent with this message, or this computer's own."""
    return zone(timezone_var.get()).key if timezone_var.get() else local_zone().key


def _local(at: datetime, tz: str) -> datetime:
    return at if at.tzinfo else at.replace(tzinfo=zone(tz))


def task_plan(p: TaskParams) -> str:
    """The exact plan the approval card asks about: what, when, until when, stop."""
    local = _local(p.at, user_zone()).astimezone(zone(user_zone()))
    when = describe(local, p.repeat, p.every)
    limits = []
    if p.until:
        limits.append(f"until {p.until:%-d %b %Y}")
    if p.times and p.repeat != "once":
        limits.append(f"at most {p.times} runs")
    stop = f" It stops when {p.stop_when}." if p.stop_when else ""
    plan = f"{when}{', ' + ', '.join(limits) if limits else ''}"
    goal = f" It checks in on goal #{p.goal}." if p.goal else ""
    return f"Schedule a task, {plan}: {plain_words(p.instruction, 300)}{stop}{goal}"


def schedule_tools(
    schedule: Schedule,
    finished: FinishedTasks,
    *,
    on_set: Callable[[], None] = lambda: None,
) -> list[Tool]:  # type: ignore[type-arg]
    def setting(p: RemindParams) -> str:
        return f"Setting a reminder: {plain_words(p.text, ACTIVITY_CHARS)}"

    @tool(
        name="remind",
        description=(
            "Remind the user of something at a time, once or repeating: it arrives in this chat "
            "and as a notification on their computer. Use it for 'remind me…' and for anything "
            "they ask to be told about at a time. A repeat may end on a date or after a number "
            "of times, or run until they cancel it."
        ),
        activity=setting,
    )
    async def remind(p: RemindParams) -> ScheduledOut:
        tz = user_zone()
        try:
            made = await schedule.remind(
                p.text,
                at=_local(p.at, tz),
                timezone=tz,
                repeat=p.repeat,
                every=p.every,
                ends_on=p.until,
                max_runs=p.times,
                goal=p.goal,
            )
        except ScheduleRefusedError as exc:
            raise ValueError(str(exc)) from None
        on_set()
        return scheduled_out(made)

    def scheduling(p: TaskParams) -> str:
        return f"Scheduling a task: {plain_words(p.instruction, ACTIVITY_CHARS)}"

    @tool(
        name="schedule_task",
        description=(
            "Schedule Enzo to do something on its own at a time, once or repeating, and report "
            "back: 'every morning at 8 check the weather', 'tell me when a table is free on "
            "Friday'. Each run is a full Enzo turn with tools; the report arrives in this chat "
            "and as a notification. A repeat must end: give times or until (ask the user if "
            "they did not say). For a watch, give stop_when. Plain reminders use remind."
        ),
        requires_approval=True,
        summary=task_plan,
        activity=scheduling,
    )
    async def schedule_task(p: TaskParams) -> ScheduledOut:
        tz = user_zone()
        try:
            made = await schedule.schedule_task(
                p.instruction,
                at=_local(p.at, tz),
                timezone=tz,
                repeat=p.repeat,
                every=p.every,
                ends_on=p.until,
                max_runs=p.times,
                stop_when=p.stop_when,
                goal=p.goal,
            )
        except ScheduleRefusedError as exc:
            raise ValueError(str(exc)) from None
        on_set()
        return scheduled_out(made)

    @tool(
        name="finish_task",
        description=(
            "Only during a scheduled task's run: say its end has been reached (what it was "
            "watching for has happened), so it reports this and runs no more."
        ),
        activity=lambda _p: "Marking the task done",
    )
    async def finish_task(p: FinishParams) -> Finished:
        job_id = job_var.get()
        if job_id is None:
            msg = "Only a scheduled task's run can finish it; to stop one, use cancel_scheduled."
            raise ValueError(msg)
        finished.mark(job_id, p.why)
        return Finished(task=job_id, why=p.why)

    @tool(
        name="list_scheduled",
        description=(
            "List the user's reminders and scheduled tasks still to come, soonest first, with "
            "their numbers."
        ),
        activity=lambda _p: "Looking at what is scheduled",
    )
    async def list_scheduled(_p: ListParams) -> ScheduledList:
        return ScheduledList(scheduled=[scheduled_out(j) for j in await schedule.active()])

    @tool(
        name="cancel_scheduled",
        description=(
            "Cancel a reminder or scheduled task the user no longer wants. Find its number first."
        ),
        activity=lambda _p: "Cancelling something scheduled",
    )
    async def cancel_scheduled(p: CancelParams) -> ScheduledOut:
        try:
            return scheduled_out(await schedule.cancel(p.id))
        except ScheduleRefusedError as exc:
            raise ValueError(str(exc)) from None

    return [remind, schedule_task, finish_task, list_scheduled, cancel_scheduled]
