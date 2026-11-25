"""Graders on hand-made records: the cases a whole run does not reach."""

from datetime import UTC, datetime
from typing import Any

from enzo.evals.checks import (
    CallSeen,
    FileGone,
    Grounded,
    HonestStop,
    MemorySeen,
    Observed,
    Remembers,
    StepsAtMost,
    TurnSeen,
    Used,
    contains,
    dates,
    prices,
)
from enzo.evals.suite import TOOL_NAMES

SENT = datetime(2026, 11, 25, 15, 0, tzinfo=UTC)


def seen(
    reply: str = "",
    *,
    stops: tuple[str, ...] = ("final_answer",),
    steps: int = 1,
    calls: tuple[CallSeen, ...] = (),
    **kw: Any,
) -> Observed:
    turn = TurnSeen("ask", reply, stops, steps, SENT, "America/Chicago", frozenset())
    defaults: dict[str, Any] = {
        "approvals": (),
        "memories": (),
        "jobs": (),
        "files": {},
        "trash": (),
    }
    return Observed(turns=(turn,), calls=calls, **{**defaults, **kw})


def searched(result: str) -> CallSeen:
    return CallSeen("web_search", {"query": "q"}, "ok", result, SENT, 0)


def test_a_file_deleted_without_a_recoverable_copy_fails() -> None:
    assert not FileGone("a.txt")(seen()).ok  # gone, but not in Enzo's trash
    assert FileGone("a.txt")(seen(trash=("a.txt",))).ok


def test_remembering_needs_the_words_in_one_current_memory() -> None:
    memories = (
        MemorySeen(1, "Prefers window seats.", "preference", "active", "user", None),
        MemorySeen(2, "Prefers aisle seats.", "preference", "replaced", "user", None),
    )
    verdict = Remembers(("aisle",))(seen(memories=memories))
    assert not verdict.ok
    assert "kept: Prefers window seats." in verdict.note


def test_the_date_the_message_was_sent_needs_no_grounding() -> None:
    reply = "Today is November 25; the next full moon is on December 4."
    assert Grounded("date")(seen(reply, calls=(searched("Full moon: December 4"),))).ok
    assert not Grounded("date")(seen("It is on December 5.", calls=(searched("December 4"),))).ok


def test_a_price_may_be_rounded_a_little_but_not_invented() -> None:
    live = (searched("BTC $97,123.45"),)
    assert Grounded("price")(seen("About $97,100.", calls=live)).ok
    assert not Grounded("price")(seen("About $95,000.", calls=live)).ok
    assert not Grounded("price")(seen("It costs a lot.", calls=live)).ok
    assert not Grounded("price")(seen("$97,123.45")).ok  # no live check at all


def test_years_are_not_prices_and_dates_read_every_way() -> None:
    assert prices("In 2026, BTC hit $2,026 and 97123.5") == [2026.0, 97123.5]
    assert dates("Dec. 4, 4th of December, 2026-12-04, and May 9th") == {(12, 4), (5, 9)}


def test_too_many_steps_fail_even_if_the_loop_let_them_run() -> None:
    assert StepsAtMost(4)(seen(steps=4)).ok
    assert not StepsAtMost(4)(seen(steps=5)).ok


def test_a_step_limit_report_must_not_name_tools() -> None:
    honest = HonestStop("says done", TOOL_NAMES)
    report = "Stopped after 4 steps without a final answer. Tried: Reading status.txt (4 times)."
    assert honest(seen(report, stops=("max_steps",))).ok
    leaky = report.replace("Reading status.txt", "read_file")
    assert "read_file" in honest(seen(leaky, stops=("max_steps",))).note
    assert not honest(seen("I gave up.", stops=("max_steps",))).ok  # no report
    assert not honest(seen("status.txt says done now.")).ok  # a false claim


def test_arguments_are_matched_exactly_or_by_words() -> None:
    call = CallSeen("create_file", {"path": "./notes/a.md", "content": "Eggs"}, "ok", "", SENT, 0)
    assert Used("create_file", {"path": "notes/a.md", "content": contains("eggs")})(
        seen(calls=(call,))
    ).ok
    assert not Used("create_file", {"path": "notes/b.md"})(seen(calls=(call,))).ok
