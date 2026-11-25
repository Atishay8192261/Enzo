"""Things Enzo does at a time for the user (spec §9.2, §9.3): reminders, for now."""

from dataclasses import dataclass

from enzo.schedule.bus import Bus, Event
from enzo.schedule.runner import Runner
from enzo.schedule.store import Delivery, Reminder, ReminderRefusedError, Schedule
from enzo.schedule.tools import reminder_tools

__all__ = [
    "Background",
    "Bus",
    "Delivery",
    "Event",
    "Reminder",
    "ReminderRefusedError",
    "Runner",
    "Schedule",
    "reminder_tools",
]


@dataclass(frozen=True, slots=True)
class Background:
    """The gateway's background work: the reminders, who delivers them, and who is listening."""

    schedule: Schedule
    bus: Bus
    runner: Runner
