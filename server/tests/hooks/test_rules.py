"""What a watch compares, and what counts as a change (spec §9.4): only a change by the rule
wakes Enzo; routine, unchanged readings never do."""

from decimal import Decimal

import pytest

from enzo.hooks.rules import Reading, Rule, Seen, describe, is_change, parse_number, seen


@pytest.mark.parametrize(
    ("text", "number"),
    [
        ("$1,299.99", "1299.99"),
        ("1.299,99 €", "1299.99"),
        ("€12,50", "12.50"),
        ("1,299", "1299"),
        ("9.5 kg", "9.5"),
        ("Now 1 299 kr", "1299"),
        ("CHF 1'299.50", "1299.50"),
        ("\u22123 °C", "-3"),
        ("Price: 59 (was 79)", "59"),
        ("1,234,567", "1234567"),
    ],
)
def test_numbers_are_read_the_way_prices_are_written(text: str, number: str) -> None:
    assert parse_number(text) == Decimal(number)


def test_no_number_is_none() -> None:
    assert parse_number("Sold out") is None
    assert parse_number("") is None


def test_a_reading_is_normalized_and_long_ones_are_compared_by_digest() -> None:
    a = seen(Reading("  Price:\n  $59.99  "), None)
    assert (a.key, a.shown, a.matched) == ("Price: $59.99", "Price: $59.99", None)
    assert a.number == Decimal("59.99")
    long = seen(Reading("word " * 400), "WORD")
    assert long.key.startswith("sha256:")
    assert long.matched is True  # looked for in the whole text, not only what is shown
    assert len(long.shown) <= 240
    assert seen(Reading(""), None).shown == "nothing there now"
    listed = seen(Reading("2 emails", items=("t:1", "t:2"), label="2 emails"), None)
    assert listed.key == "t:1\nt:2"


def _s(key: str = "x", number: str | None = None, matched: bool | None = None) -> Seen:
    return Seen(key, key, Decimal(number) if number else None, matched)


def test_the_first_reading_never_wakes() -> None:
    for rule in ("changes", "new", "above", "below", "appears"):
        assert not is_change(rule, Decimal(1), None, _s("y", "5", matched=True))


def test_changes_wakes_on_any_difference_only() -> None:
    assert is_change("changes", None, _s("a"), _s("b"))
    assert not is_change("changes", None, _s("a"), _s("a"))


def test_new_wakes_only_when_an_item_appears() -> None:
    assert is_change("new", None, _s("t:1"), _s("t:2\nt:1"))
    assert not is_change("new", None, _s("t:2\nt:1"), _s("t:1"))  # one went away: not new
    assert not is_change("new", None, _s("t:1\nt:2"), _s("t:2\nt:1"))  # order alone
    assert is_change("new", None, _s(""), _s("t:1"))  # the first one


@pytest.mark.parametrize(
    ("rule", "before", "after", "wakes"),
    [
        ("below", "59", "39", True),  # crossed
        ("below", "39", "35", False),  # still below: no second wake
        ("below", "39", "45", False),  # back above
        ("below", "45", "40", False),  # equal is not below
        ("above", "40", "41", True),
        ("above", "41", "50", False),
        ("above", None, "50", True),  # before had no number
        ("below", "59", None, False),  # no number now
    ],
)
def test_thresholds_wake_once_when_crossed(
    rule: Rule, before: str | None, after: str | None, wakes: bool
) -> None:
    assert is_change(rule, Decimal(40), _s("a", before), _s("b", after)) is wakes


def test_appears_wakes_when_the_words_arrive() -> None:
    assert is_change("appears", None, _s(matched=False), _s(matched=True))
    assert not is_change("appears", None, _s(matched=True), _s(matched=True))
    assert not is_change("appears", None, _s(matched=True), _s(matched=False))


def test_what_counts_as_a_change_in_words() -> None:
    assert describe("below", Decimal("40.00"), None) == "the number in it goes below 40"
    assert describe("above", Decimal("9.5"), None) == "the number in it goes above 9.5"
    assert describe("appears", None, "In stock") == "“In stock” appears"
    assert describe("new", None, None) == "a new one appears"
    assert describe("changes", None, None) == "it changes"
