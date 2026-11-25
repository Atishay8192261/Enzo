"""The brain's reminder tools (spec §7 batch 4, §9.3): remind, list and cancel.

Setting a reminder needs no approval: it only ever messages the user, the chat shows it as a step
("Setting a reminder: …"), and /reminders lists and cancels it. Times are the user's local
times; the zone comes from their app with the message, else this computer's own.
"""

from collections.abc import Callable
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from enzo.context import timezone_var
from enzo.schedule.recurrence import Repeat, local_zone, zone
from enzo.schedule.store import Reminder, ReminderRefusedError, Schedule
from enzo.tools.registry import Tool, plain_words, tool

ACTIVITY_CHARS = 60


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RemindParams(_Params):
    text: str = Field(
        min_length=1,
        max_length=200,
        description="What to remind the user of, short, as they would say it: 'Call mom'.",
    )
    at: datetime = Field(
        description=(
            "When, in the user's local time, like 2026-11-21T09:00 (no zone needed). For 'in 20 "
            "minutes', add to the time the message was sent. For a repeat, the first time."
        )
    )
    repeat: Repeat = Field(
        default="once",
        description="once, or hourly, daily, weekdays (Monday to Friday), weekly or monthly.",
    )
    every: int = Field(
        default=1, ge=1, le=366, description="Repeat every this many hours/days/weeks/months."
    )
    until: date | None = Field(
        default=None, description="For a repeat: the last local date it may fire on."
    )
    times: int | None = Field(
        default=None, ge=1, le=1000, description="For a repeat: stop after this many."
    )


class ReminderOut(BaseModel):
    id: int
    text: str
    #: "Fri 21 Nov 09:00", in the user's zone.
    next: str | None
    #: "every weekday at 09:00, until 1 Dec 2026".
    schedule: str
    timezone: str


class ListParams(_Params):
    pass


class Reminders(BaseModel):
    reminders: list[ReminderOut]


class CancelParams(_Params):
    id: int = Field(ge=1, description="The number of the reminder to cancel.")


def reminder_out(r: Reminder) -> ReminderOut:
    return ReminderOut(
        id=r.id, text=r.text, next=r.next_words, schedule=r.schedule, timezone=r.timezone
    )


def user_zone() -> str:
    """The zone the user's app sent with this message, or this computer's own."""
    return zone(timezone_var.get()).key if timezone_var.get() else local_zone().key


def reminder_tools(schedule: Schedule, *, on_set: Callable[[], None] = lambda: None) -> list[Tool]:  # type: ignore[type-arg]
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
    async def remind(p: RemindParams) -> ReminderOut:
        tz = user_zone()
        at = p.at if p.at.tzinfo else p.at.replace(tzinfo=zone(tz))
        try:
            made = await schedule.remind(
                p.text,
                at=at,
                timezone=tz,
                repeat=p.repeat,
                every=p.every,
                ends_on=p.until,
                max_runs=p.times,
            )
        except ReminderRefusedError as exc:
            raise ValueError(str(exc)) from None
        on_set()
        return reminder_out(made)

    @tool(
        name="list_reminders",
        description="List the user's reminders still to come, soonest first, with their numbers.",
        activity=lambda _p: "Looking at your reminders",
    )
    async def list_reminders(_p: ListParams) -> Reminders:
        return Reminders(reminders=[reminder_out(r) for r in await schedule.active()])

    @tool(
        name="cancel_reminder",
        description="Cancel a reminder the user no longer wants. Find its number first.",
        activity=lambda _p: "Cancelling a reminder",
    )
    async def cancel_reminder(p: CancelParams) -> ReminderOut:
        try:
            return reminder_out(await schedule.cancel(p.id))
        except ReminderRefusedError as exc:
            raise ValueError(str(exc)) from None

    return [remind, list_reminders, cancel_reminder]
