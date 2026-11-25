"""When a repeating reminder comes next, in the user's own clock (spec §9.3).

Repeats are counted in local wall time: "every day at 9:00" stays at 9:00 across a daylight-saving
change, and "every month on the 31st" falls on the last day of shorter months. Hourly repeats
count real hours. A wall time that does not exist on a given day (the hour skipped in spring)
comes out an hour later, as clocks show it.
"""

import calendar
from datetime import UTC, date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

type Repeat = Literal["once", "hourly", "daily", "weekdays", "weekly", "monthly"]

#: How far ahead an occurrence is searched for: a schedule has one long before this.
_SEARCH = 2000


def zone(name: str | None) -> ZoneInfo:
    """The named time zone, or the computer's own when the name is missing or unknown."""
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return local_zone()


def local_zone() -> ZoneInfo:
    """This computer's zone: Enzo runs on the user's own machine (decision 2026-11-18)."""
    try:
        key = _zone_key(datetime.now().astimezone().tzinfo)
        return ZoneInfo(key) if key else ZoneInfo("UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _zone_key(tzinfo: object) -> str | None:
    key = getattr(tzinfo, "key", None)
    if isinstance(key, str):
        return key
    # /etc/localtime links to …/zoneinfo/America/Los_Angeles on macOS and Linux alike.
    try:
        from pathlib import Path  # noqa: PLC0415 - only needed on this fallback

        target = Path("/etc/localtime").resolve()
    except OSError:
        return None
    parts = target.parts
    return "/".join(parts[parts.index("zoneinfo") + 1 :]) if "zoneinfo" in parts else None


def occurrence(first: datetime, repeat: Repeat, every: int, n: int) -> datetime:
    """The ``n``-th time (0 is ``first``), as an aware time in ``first``'s zone."""
    if n == 0 or repeat == "once":
        return first
    if repeat == "hourly":
        return (first.astimezone(UTC) + timedelta(hours=every * n)).astimezone(first.tzinfo)
    day = first.date()
    if repeat == "daily":
        day = day + timedelta(days=every * n)
    elif repeat == "weekly":
        day = day + timedelta(weeks=every * n)
    elif repeat == "weekdays":
        day = _weekday_after(day, n)
    else:  # monthly
        months = day.month - 1 + every * n
        year, month = day.year + months // 12, months % 12 + 1
        day = date(year, month, min(first.day, calendar.monthrange(year, month)[1]))
    wall = datetime.combine(day, first.timetz().replace(tzinfo=None))
    # Through UTC and back: a wall time skipped by a clock change comes out as clocks show it.
    return wall.replace(tzinfo=first.tzinfo).astimezone(UTC).astimezone(first.tzinfo)


def _weekday_after(day: date, n: int) -> date:
    """The ``n``-th Monday-to-Friday day after ``day`` (which may itself be any day)."""
    weeks, rest = divmod(n, 5)
    day = day + timedelta(weeks=weeks)
    while rest:
        day += timedelta(days=1)
        if day.weekday() < 5:  # Monday to Friday
            rest -= 1
    return day


def next_after(first: datetime, repeat: Repeat, every: int, after: datetime) -> datetime | None:
    """The first occurrence later than ``after``; None once a one-time reminder has passed."""
    if repeat == "once":
        return first if first > after else None
    lo = _estimate(first, repeat, every, after)
    for n in range(lo, lo + _SEARCH):
        at = occurrence(first, repeat, every, n)
        if at > after:
            return at
    return None  # pragma: no cover - a schedule always has a next time within the search


def _estimate(first: datetime, repeat: Repeat, every: int, after: datetime) -> int:
    """A count just below the answer, so long-running schedules never step from the start."""
    if after <= first:
        return 0
    span = after - first
    per = {
        "hourly": timedelta(hours=every),
        "daily": timedelta(days=every),
        "weekly": timedelta(weeks=every),
        "weekdays": timedelta(days=7 / 5),
        "monthly": timedelta(days=28 * every),
    }[repeat]
    # Two steps back from the estimate absorbs clock changes and short months.
    return max(0, int(span / per) - 2) if repeat != "monthly" else max(0, int(span / per) // 2)


def describe(first: datetime, repeat: Repeat, every: int) -> str:
    """The schedule in words: "every weekday at 09:00", "every 2 weeks on Friday at 18:30"."""
    clock = f"{first:%H:%M}"
    if repeat == "once":
        return f"once, {first:%a %-d %b %Y} at {clock}"
    if repeat == "hourly":
        return "every hour" if every == 1 else f"every {every} hours"
    if repeat == "weekdays":
        return f"every weekday at {clock}"
    if repeat == "daily":
        return f"every day at {clock}" if every == 1 else f"every {every} days at {clock}"
    if repeat == "weekly":
        unit = "every week" if every == 1 else f"every {every} weeks"
        return f"{unit} on {first:%A} at {clock}"
    unit = "every month" if every == 1 else f"every {every} months"
    return f"{unit} on day {first.day} at {clock}"
