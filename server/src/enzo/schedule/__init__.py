"""Things Enzo does at a time for the user (spec §9.2, §9.3): reminders and scheduled tasks."""

from dataclasses import dataclass

from enzo.schedule.bus import Bus, Event
from enzo.schedule.runner import Runner
from enzo.schedule.store import Delivery, Job, Schedule, ScheduleRefusedError, TaskEnd, TaskStart
from enzo.schedule.tools import FinishedTasks, schedule_tools

__all__ = [
    "Background",
    "Bus",
    "Delivery",
    "Event",
    "FinishedTasks",
    "Job",
    "Runner",
    "Schedule",
    "ScheduleRefusedError",
    "TaskEnd",
    "TaskStart",
    "schedule_tools",
]


@dataclass(frozen=True, slots=True)
class Background:
    """The gateway's background work: what is scheduled, who runs it, and who is listening."""

    schedule: Schedule
    bus: Bus
    runner: Runner
