"""Watches in the database (spec §9.2, §9.4): off until a dry run the user saw turns them on, one
wake per change even across restarts, backing off and stopping on failures, a rate limit
stopping its whole scope, and an end that is always there."""

import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from enzo.hooks.rules import Reading, Seen, seen
from enzo.hooks.sources import EmailTarget, PageTarget, PollError, Target
from enzo.hooks.store import (
    DRY_RUN_FRESH,
    MAX_FAILURES,
    HookRefusedError,
    Hooks,
    backoff_s,
    every_words,
)
from enzo.logs import JsonLines
from enzo.store.repository import Store

NOW = datetime(2026, 11, 27, 9, 0, tzinfo=UTC)
PAGE = PageTarget(url="https://shop.example/kettle", selector=".price")


@pytest.fixture
def hooks(store: Store) -> Hooks:
    return Hooks(store.engine)


def reads(text: str) -> Seen:
    return seen(Reading(text), None)


async def made(hooks: Hooks, target: Target = PAGE, **more: Any) -> int:
    settings: dict[str, object] = {
        "what": "the kettle's price",
        "target": target,
        "rule": "below",
        "threshold": Decimal(40),
        "instruction": "Tell me the new price",
        "interval_s": 600,
        "timezone": "UTC",
        "max_wakes": 3,
        "now": NOW,
        **more,
    }
    return (await hooks.create(**settings)).id  # type: ignore[arg-type]


async def turned_on(hooks: Hooks, value: str = "$59.99", **more: Any) -> int:
    hid = await made(hooks, **more)
    dry = await hooks.dry_run(hid, reads(value), NOW)
    await hooks.enable(hid, dry.token, NOW)
    return hid


async def check(hooks: Hooks, hid: int, outcome: Seen | PollError, at: datetime) -> object:
    (claimed,) = [h for h in await hooks.claim_polls(at) if h.id == hid]
    return await hooks.record(claimed.id, outcome, at)


async def test_a_watch_starts_off_and_needs_an_end(hooks: Hooks) -> None:
    hid = await made(hooks)
    hook = await hooks.get(hid)
    assert hook is not None
    assert (hook.status, hook.next_poll_at) == ("off", None)
    assert await hooks.claim_polls(NOW + timedelta(days=1)) == []  # off: never checked
    with pytest.raises(HookRefusedError, match="needs an end"):
        await made(hooks, max_wakes=None)
    with pytest.raises(HookRefusedError, match="at most once a minute"):
        await made(hooks, interval_s=59)
    with pytest.raises(HookRefusedError, match="already passed"):
        await made(hooks, ends_at=NOW - timedelta(hours=1))
    with pytest.raises(HookRefusedError, match="never keeps those"):
        await made(hooks, instruction="Log in with password: hunter2")


async def test_turning_on_needs_the_latest_recent_dry_run(hooks: Hooks) -> None:
    hid = await made(hooks)
    with pytest.raises(HookRefusedError, match="Check it first"):
        await hooks.enable(hid, None, NOW)
    first = await hooks.dry_run(hid, reads("$59.99"), NOW)
    second = await hooks.dry_run(hid, reads("$58.00"), NOW + timedelta(minutes=1))
    with pytest.raises(HookRefusedError, match="not the latest check"):
        await hooks.enable(hid, first.token, NOW + timedelta(minutes=2))
    with pytest.raises(HookRefusedError, match="more than 30 minutes old"):
        await hooks.enable(hid, second.token, second.at + DRY_RUN_FRESH + timedelta(seconds=1))
    on = await hooks.enable(hid, second.token, NOW + timedelta(minutes=2))
    assert (on.status, on.shown) == ("on", "$58.00")  # it starts from what the user saw
    assert on.next_poll_at == NOW + timedelta(minutes=12)
    with pytest.raises(HookRefusedError, match="on already"):
        await hooks.enable(hid, second.token, NOW + timedelta(minutes=3))


async def test_unchanged_checks_are_silent_and_a_change_wakes_once(
    hooks: Hooks, store: Store
) -> None:
    hid = await turned_on(hooks)
    at = NOW
    for _ in range(3):  # leave the condition unchanged: nothing to act on
        at += timedelta(minutes=10)
        checked = await check(hooks, hid, reads("$59.99"), at)
        assert checked.wake is None  # type: ignore[attr-defined]
    assert await hooks.claim_wakes(at) == []
    at += timedelta(minutes=10)
    checked = await check(hooks, hid, reads("$39.00"), at)
    wake = checked.wake  # type: ignore[attr-defined]
    assert (wake.before, wake.after) == ("$59.99", "$39.00")
    # A restart: a new store on the same database. The change is not seen again.
    again = Hooks(store.engine)
    assert await again.recover(at) == []
    at += timedelta(minutes=10)
    assert (await check(again, hid, reads("$39.00"), at)).wake is None  # type: ignore[attr-defined]
    at += timedelta(minutes=10)
    assert (await check(again, hid, reads("$35.00"), at)).wake is None  # type: ignore[attr-defined]
    (claimed,) = await again.claim_wakes(at)
    assert claimed.event_id == wake.event_id  # the one change, waiting to be acted on
    assert await again.claim_wakes(at) == []  # claimed once
    hook = await again.get(hid)
    assert (hook.wakes, hook.polls) == (1, 6)  # type: ignore[union-attr]


async def test_a_wake_cut_short_by_a_restart_is_failed_not_run_again(
    hooks: Hooks, store: Store
) -> None:
    hid = await turned_on(hooks)
    await check(hooks, hid, reads("$30.00"), NOW + timedelta(minutes=10))
    (running,) = await hooks.claim_wakes(NOW + timedelta(minutes=10))
    restarted = Hooks(store.engine)
    assert await restarted.recover(NOW + timedelta(minutes=11)) == [running.event_id]
    assert await restarted.claim_wakes(NOW + timedelta(minutes=12)) == []


async def test_one_wake_at_a_time_per_watch(hooks: Hooks) -> None:
    hid = await turned_on(hooks, rule="changes", threshold=None)
    await check(hooks, hid, reads("$50.00"), NOW + timedelta(minutes=10))
    (first,) = await hooks.claim_wakes(NOW + timedelta(minutes=10))
    await check(hooks, hid, reads("$45.00"), NOW + timedelta(minutes=20))
    assert await hooks.claim_wakes(NOW + timedelta(minutes=20)) == []  # the first still runs
    await hooks.finish_wake(first.event_id, "reported", "x", NOW + timedelta(minutes=21))
    (second,) = await hooks.claim_wakes(NOW + timedelta(minutes=21))
    assert (second.before, second.after) == ("$50.00", "$45.00")


async def test_its_last_wake_ends_it(hooks: Hooks) -> None:
    hid = await turned_on(hooks, max_wakes=1)
    checked = await check(hooks, hid, reads("$30.00"), NOW + timedelta(minutes=10))
    assert checked.hook.status == "done"  # type: ignore[attr-defined]
    assert checked.hook.stop_reason == "max_wakes"  # type: ignore[attr-defined]
    assert checked.wake is not None  # type: ignore[attr-defined]  # its last change is acted on
    assert await hooks.claim_polls(NOW + timedelta(days=1)) == []


async def test_failures_back_off_then_stop_with_a_report(hooks: Hooks) -> None:
    hid = await turned_on(hooks)
    at = NOW
    for n in range(1, MAX_FAILURES):
        hook = await hooks.get(hid)
        at = hook.next_poll_at  # type: ignore[union-attr, assignment]
        checked = await check(hooks, hid, PollError("HTTP 503"), at)
        assert checked.hook.next_poll_at == at + timedelta(seconds=backoff_s(600, n))  # type: ignore[attr-defined]
        assert checked.stopped == ()  # type: ignore[attr-defined]
    hook = await hooks.get(hid)
    checked = await check(hooks, hid, PollError("HTTP 503"), hook.next_poll_at)  # type: ignore[union-attr, arg-type]
    (stop,) = checked.stopped  # type: ignore[attr-defined]
    assert stop.reason == "failing"
    assert stop.words.startswith("Enzo stopped watching the kettle's price: the last 5 checks")
    assert "HTTP 503" in stop.words
    assert (await hooks.get(hid)).status == "stopped"  # type: ignore[union-attr]
    # A success in between resets the count.
    other = await turned_on(hooks)
    for outcome in (PollError("x"), PollError("x"), reads("$59.99"), PollError("x")):
        hook = await hooks.get(other)
        checked = await check(hooks, other, outcome, hook.next_poll_at)  # type: ignore[union-attr, arg-type]
    assert checked.hook.failures == 1  # type: ignore[attr-defined]


def test_backoff_doubles_and_is_capped() -> None:
    assert [backoff_s(600, n) for n in (1, 2, 3, 6)] == [1200, 2400, 4800, 6 * 3600]
    assert backoff_s(86400, 3) == 86400  # never shorter than its own interval
    assert every_words(60) == "every minute"
    assert every_words(1800) == "every 30 minutes"
    assert every_words(7200) == "every 2 hours"
    assert every_words(86400) == "every day"


async def test_a_rate_limit_stops_the_whole_scope_at_once(hooks: Hooks) -> None:
    kettle = await turned_on(hooks)
    mug = await turned_on(hooks, target=PageTarget(url="https://www.shop.example/mug"))
    elsewhere = await turned_on(hooks, target=PageTarget(url="https://other.example/"))
    mail = await turned_on(hooks, target=EmailTarget(query="x"), rule="new", threshold=None)
    at = NOW + timedelta(minutes=10)
    checked = await check(hooks, kettle, PollError("429", stop="rate_limited"), at)
    stopped = {s.hook.id: s for s in checked.stopped}  # type: ignore[attr-defined]
    assert set(stopped) == {kettle, mug}  # the same site, and nothing else
    assert "Nothing was retried" in stopped[mug].words
    assert [(await hooks.get(h)).status for h in (elsewhere, mail)] == ["on", "on"]  # type: ignore[union-attr]


async def test_its_last_day_ends_it_with_a_report_and_cancel_is_final(hooks: Hooks) -> None:
    hid = await turned_on(hooks, ends_at=NOW + timedelta(days=1), max_wakes=None)
    (end,) = await hooks.end_due(NOW + timedelta(days=1))
    assert (end.hook.id, end.reason) == (hid, "ended")
    assert (
        end.words == "The watch on the kettle's price ended: its last day passed. It saw no change."
    )
    assert await hooks.listed() == [] or hid not in {h.id for h in await hooks.listed()}
    gone = await turned_on(hooks)
    (claimed,) = await hooks.claim_polls(NOW + timedelta(minutes=10))
    await hooks.cancel(gone, NOW + timedelta(minutes=10))
    late = await hooks.record(claimed.id, reads("$1.00"), NOW + timedelta(minutes=10))
    assert late.wake is None  # cancelled while it was checking: nothing comes of it
    assert await hooks.claim_polls(NOW + timedelta(days=2)) == []


async def test_its_log_lines_name_what_happened_with_numbers_only(hooks: Hooks) -> None:
    """Seen live: an extra field named "event" replaced "hook.changed" in the JSON line."""
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    logger = logging.getLogger("enzo.hooks")
    logger.addHandler(handler)
    level = logger.level
    logger.setLevel(logging.INFO)
    try:
        hid = await turned_on(hooks)
        await check(hooks, hid, reads("$30.00"), NOW + timedelta(minutes=10))
        (wake,) = await hooks.claim_wakes(NOW + timedelta(minutes=10))
        await hooks.finish_wake(wake.event_id, "reported", "words", NOW + timedelta(minutes=11))
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)
    lines = [json.loads(JsonLines().format(r)) for r in records]
    assert [line["event"] for line in lines] == [
        "hook.set",
        "hook.dry_run",
        "hook.enabled",
        "hook.changed",
        "hook.wake",
    ]
    assert lines[3] == {**lines[3], "hook": hid, "wake": wake.event_id, "wakes": 1}
    assert "$30.00" not in json.dumps(lines)  # never what it read, never the instruction
    assert "Tell me" not in json.dumps(lines)
