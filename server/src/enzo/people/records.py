"""What Enzo knows about the people in the user's life (spec §6.4, §6.5, ADR 0036).

A person is a name, the other names they go by, and facts. Every fact keeps its evidence: who
said it (the user, or Enzo inferred it), when, in which chat and run, and when it happened if it
is an event. A fact is corrected in place: the new one names the one it replaces, which is kept
for the record but never used again (never two truths).
"""

import calendar
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

type FactKind = Literal["relation", "birthday", "likes", "food", "contact", "other", "tie"]
type SaidBy = Literal["user", "inferred"]
#: Facts a person has one of at a time: a new one replaces the one before.
SINGLE: frozenset[str] = frozenset({"relation", "birthday"})
#: How each kind starts its line in People.md and in /people ("Likes: dark chocolate").
LABELS: dict[FactKind, str] = {
    "relation": "Relation",
    "birthday": "Birthday",
    "likes": "Likes",
    "food": "Food",
    "contact": "Contact",
    "other": "Note",
}
#: The order facts are shown in.
ORDER: tuple[FactKind, ...] = ("relation", "birthday", "likes", "food", "contact", "other", "tie")
#: Where a fact was learned when the user wrote it in People.md by hand, not in a chat run.
HAND_EDIT = "People.md"


class PeopleRefusedError(Exception):
    """A plain-language reason a person or fact was not saved or changed, for the brain."""


@dataclass(frozen=True, slots=True)
class Birthday:
    month: int
    day: int
    year: int | None = None

    def words(self) -> str:
        """ "14 March", or "14 March 1990" when the year is known."""
        out = f"{self.day} {calendar.month_name[self.month]}"
        return f"{out} {self.year}" if self.year else out

    def on(self, year: int) -> date:
        """The birthday in ``year`` (29 February falls on the 28th in other years)."""
        return date(year, self.month, min(self.day, calendar.monthrange(year, self.month)[1]))


_ISO = re.compile(r"^(?:(\d{4})-)?(\d{1,2})-(\d{1,2})$")
_MONTHS = {
    name.casefold(): n
    for n in range(1, 13)
    for name in (calendar.month_name[n], calendar.month_abbr[n], calendar.month_abbr[n] + ".")
}
_DAY_MONTH = re.compile(r"^(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([A-Za-z.]+),?(?:\s+(\d{4}))?$")
_MONTH_DAY = re.compile(r"^([A-Za-z.]+)\s+(\d{1,2})(?:st|nd|rd|th)?,?(?:\s+(\d{4}))?$")


def parse_birthday(value: str, today: date | None = None) -> Birthday:
    """A birthday from "03-14", "1990-03-14", "14 March", "March 14th, 1990" and the like."""
    words = " ".join(value.split())
    month: int | None
    if found := _ISO.match(words):
        year, month, day = found.group(1), int(found.group(2)), int(found.group(3))
    elif found := _DAY_MONTH.match(words):
        day, month, year = int(found.group(1)), _MONTHS.get(found.group(2).casefold()), found[3]
    elif found := _MONTH_DAY.match(words):
        month, day, year = _MONTHS.get(found.group(1).casefold()), int(found.group(2)), found[3]
    else:
        month = None
    if month is None:
        msg = f"“{words}” is not a date Enzo can read: write it like 03-14 or 1990-03-14."
        raise PeopleRefusedError(msg)
    born = int(year) if year else None
    this_year = (today or date.today()).year
    try:
        date(born or 2000, month, day)  # 2000 was a leap year: 29 February is a birthday
    except ValueError:
        msg = f"“{words}” is not a real date."
        raise PeopleRefusedError(msg) from None
    if born is not None and not 1900 <= born <= this_year:
        msg = f"A birth year of {born} cannot be right."
        raise PeopleRefusedError(msg)
    return Birthday(month, day, born)


@dataclass(frozen=True, slots=True)
class PersonFact:
    id: int
    person_id: int
    kind: FactKind
    #: The fact in plain words; a birthday's date in words; a tie's relation ("sister").
    text: str
    said_by: SaidBy
    happened_on: date | None
    learned_at: datetime
    #: The chat it was learned in, by its title, when that chat still exists.
    chat: str | None
    conversation_id: uuid.UUID | None
    #: The user wrote or changed it in People.md by hand (spec §11.2).
    edited_by_hand: bool = False
    #: A tie: the other person, by number and name; and the name of the one who holds it.
    other_id: int | None = None
    other_name: str | None = None
    holder_name: str | None = None
    birthday: Birthday | None = None

    def line(self) -> str:
        """The fact as its line says it: "Likes: dark chocolate", "Sister of: Sam"."""
        if self.kind == "tie":
            return f"{self.text[:1].upper()}{self.text[1:]} of: {self.other_name}"
        return f"{LABELS[self.kind]}: {self.text}"


def provenance(fact: PersonFact) -> str:
    """Where a fact came from, in words: "you said, 20 Nov 2026, in “Gift ideas”"."""
    if fact.edited_by_hand:
        who = "edited by hand in People.md"
    else:
        who = "you said" if fact.said_by == "user" else "Enzo inferred"
    parts = [who, f"{fact.learned_at:%-d %b %Y}"]
    if fact.happened_on:
        parts.append(f"happened {fact.happened_on:%-d %b %Y}")
    if fact.chat:
        parts.append(f"in “{fact.chat}”")
    return ", ".join(parts)


@dataclass(frozen=True, slots=True)
class Link:
    """A goal, reminder or task about someone."""

    kind: Literal["goal", "reminder", "task"]
    id: int
    text: str
    #: A goal's status, or a reminder's schedule in words ("every year on 14 Mar at 09:00").
    status: str
    #: A reminder's next time in words ("Sat 14 Mar 09:00").
    next: str | None = None
    #: A yearly birthday reminder, this many days before (0: on the day).
    days_before: int | None = None


@dataclass(frozen=True, slots=True)
class Person:
    id: int
    name: str
    aliases: tuple[str, ...]
    #: Current facts, in the order they are shown.
    facts: tuple[PersonFact, ...]
    #: Ties other people hold about this one ("Priya is Sam's sister", on Sam).
    tied_by: tuple[PersonFact, ...]
    created_at: datetime
    chat: str | None
    conversation_id: uuid.UUID | None
    edited_by_hand: bool = False
    links: tuple[Link, ...] = ()

    def first(self, kind: FactKind) -> PersonFact | None:
        return next((f for f in self.facts if f.kind == kind), None)

    @property
    def relation(self) -> str | None:
        found = self.first("relation")
        return found.text if found else None

    @property
    def birthday(self) -> Birthday | None:
        found = self.first("birthday")
        return found.birthday if found else None

    @property
    def names(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)

    def tie_words(self, fact: PersonFact) -> str:
        """A tie either way, as a sentence: "Priya is Sam's sister"."""
        return f"{fact.holder_name} is {fact.other_name}'s {fact.text}"
