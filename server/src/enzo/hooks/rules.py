"""What a watch compares between two checks, and what counts as a change (spec §9.4).

A check gives a ``Reading``: the text it found, normalized (one line, single spaces), and for
watches of a list (new emails, new comments) the items' ids. It is stored as ``Seen``: the text
itself when short, else a digest, plus the number in it and whether the words to look for are in
it. Only a change by the watch's rule wakes Enzo; everything else is routine and stays silent.

- ``changes``: the reading is different from the last one.
- ``new``: an item appeared that the last reading did not have (a new email, a new comment).
- ``above`` / ``below``: the number in the reading crossed the threshold (it was not past it
  before). Staying past it does not wake Enzo again.
- ``appears``: the words appeared (they were not there before).
"""

import hashlib
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal

from enzo.tools.registry import plain_words

type Rule = Literal["changes", "new", "above", "below", "appears"]

#: Readings longer than this are compared by a digest (the user sees the start).
MAX_KEY = 1000
MAX_SHOWN = 240

# A number as pages write it: "$1,299.99", "1.299,99 €", "12,50", "-3".
_NUMBER = re.compile(r"[-\u2212]?\d(?:[\d.,'\u00a0\u202f ]*\d)?")


@dataclass(frozen=True, slots=True)
class Reading:
    """What one check found."""

    #: The part watched, normalized; empty when it is not there now.
    text: str
    #: For a list (emails, comments, events): each item's key, newest first.
    items: tuple[str, ...] | None = None
    #: What the user sees, when the text alone does not say it well ("3 emails; newest: …").
    label: str | None = None


@dataclass(frozen=True, slots=True)
class Seen:
    """A reading as stored and compared."""

    key: str
    shown: str
    number: Decimal | None
    matched: bool | None


def normalize(text: str) -> str:
    """One printable line, single spaces: layout changes alone are never a change."""
    return " ".join("".join(c if c.isprintable() else " " for c in text).split())


def seen(reading: Reading, match: str | None) -> Seen:
    text = normalize(reading.text)
    if reading.items is not None:
        key = "\n".join(reading.items)
    elif len(text) > MAX_KEY:
        key = "sha256:" + hashlib.sha256(text.encode()).hexdigest()
    else:
        key = text
    shown = plain_words(reading.label or text or "nothing there now", MAX_SHOWN)
    found = contains(text, match) if match else None
    return Seen(key=key, shown=shown, number=parse_number(text), matched=found)


def contains(text: str, words: str) -> bool:
    return normalize(words).casefold() in normalize(text).casefold()


def is_change(rule: Rule, threshold: Decimal | None, before: Seen | None, after: Seen) -> bool:
    """Whether going from ``before`` to ``after`` wakes Enzo. The first reading never does."""
    if before is None:
        return False
    if rule == "changes":
        return after.key != before.key
    if rule == "new":
        return bool(set(after.key.splitlines()) - set(before.key.splitlines()))
    if rule == "appears":
        return bool(after.matched) and not before.matched
    return _past(rule, threshold, after.number) and not _past(rule, threshold, before.number)


def _past(rule: Rule, threshold: Decimal | None, number: Decimal | None) -> bool:
    """The number is past the threshold: above it, or below it."""
    if threshold is None or number is None:
        return False
    return number > threshold if rule == "above" else number < threshold


def needs_number(rule: Rule) -> bool:
    return rule in ("above", "below")


def describe(rule: Rule, threshold: Decimal | None, match: str | None) -> str:
    """What counts as a change, in words: "the number goes below 40"."""
    if rule == "changes":
        return "it changes"
    if rule == "new":
        return "a new one appears"
    if rule == "appears":
        return f"“{plain_words(match or '', 80)}” appears"
    side = "above" if rule == "above" else "below"
    return f"the number in it goes {side} {_plain(threshold)}"


def _plain(number: Decimal | None) -> str:
    if number is None:
        return "?"
    return format(number.normalize(), "f") if number == number.to_integral() else str(number)


def parse_number(text: str) -> Decimal | None:
    """The first number in ``text``, read the way prices are written, or None.

    The last of "." and "," is the decimal mark when both appear ("1,299.99", "1.299,99"). A lone
    one is decimal when one or two digits follow it ("12,50", "9.5"), else it groups thousands
    ("1,299"). Spaces and apostrophes group thousands too ("1 299", "1'299").
    """
    found = _NUMBER.search(text)
    if found is None:
        return None
    raw = re.sub(r"[\u00a0\u202f ']", "", found.group()).replace("\u2212", "-")
    sign, digits = ("-", raw[1:]) if raw.startswith("-") else ("", raw)
    marks = [c for c in digits if c in ".,"]
    if marks:
        last = max(digits.rfind("."), digits.rfind(","))
        decimal = len(set(marks)) == 2 or (len(marks) == 1 and len(digits) - last - 1 in (1, 2))
        if decimal:
            digits = re.sub(r"[.,]", "", digits[:last]) + "." + digits[last + 1 :]
        else:
            digits = re.sub(r"[.,]", "", digits)
    try:
        return Decimal(sign + digits)
    except InvalidOperation:
        return None
