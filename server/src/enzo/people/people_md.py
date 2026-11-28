"""``People.md``: the people in the user's life as a file they read and edit by hand (spec §11.2).

Enzo writes it from its records; the user may change it in any editor, and Enzo reads the change
back before the next message, the way it reads Memory.md (ADR 0023). The format:

- one ``## Name (#2)`` heading per person; the number tells Enzo who it is, so a changed name is
  a correction, and a new heading without a number is someone new;
- ``Also called: Pri, Priyanka`` under it: the other names they go by;
- one ``- `` line per fact, starting with what it is (``Relation``, ``Birthday``, ``Likes``,
  ``Food``, ``Contact``, ``Note``, or ``Sister of`` for a tie to someone else here) and ending
  with where it came from and its number: ``- Likes: dark chocolate _(you said, …; #7)_``;
- a comment near the top says when Enzo wrote this copy, so lines missing from an older copy are
  never taken as removed;
- lines Enzo cannot read are kept for the user under "Not saved", with Enzo's notes.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from enzo.people.records import (
    LABELS,
    ORDER,
    Birthday,
    FactKind,
    PeopleRefusedError,
    Person,
    parse_birthday,
    provenance,
)

PEOPLE_FILE = "People.md"
TITLE = "# People in your life"
NOT_SAVED = "Not saved"
INTRO = (
    "You can edit this file. Enzo reads your changes before your next message: change a line to",
    "correct it, add a line under someone to teach Enzo something, delete a line to make it",
    "forget. Add someone with a new “## Name” heading. Keep the #numbers: they tell Enzo which",
    "line is which. A line starts with Relation, Birthday, Likes, Food, Contact or Note, or says",
    "how they are related to someone else here, like “Sister of: Sam”.",
)
EMPTY = "No one yet."
NOT_SAVED_INTRO = (
    "Enzo did not save the lines below. Delete a note once you have read it. To make a line a",
    "fact, start it with “- ” and move it under someone's heading above.",
)
_OWN = {TITLE, *INTRO, EMPTY, *NOT_SAVED_INTRO}

_STAMP = re.compile(r"^<!-- Enzo wrote this copy at (\S+?)\. .*-->$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_NUMBERED = re.compile(r"^(.*?)\s*\(#(\d+)\)$")
_ITEM = re.compile(r"^ {0,3}[-*+]\s+(.*)$")
_ALSO = re.compile(r"^also (?:called|known as)\s*:\s*(.*)$", re.IGNORECASE)
_TAG = re.compile(r"\s*_\(.*#(\d+)\)_\s*$")
_LABEL = re.compile(r"^([^:]{1,40}?)\s*:\s*(.*)$")
_TIE = re.compile(r"^(.+?)\s+of$", re.IGNORECASE)
_KINDS: dict[str, FactKind] = {
    "relation": "relation",
    "relationship": "relation",
    "birthday": "birthday",
    "born": "birthday",
    "likes": "likes",
    "like": "likes",
    "loves": "likes",
    "food": "food",
    "diet": "food",
    "eats": "food",
    "can't eat": "food",
    "cannot eat": "food",
    "contact": "contact",
    "note": "other",
    "notes": "other",
}


@dataclass(frozen=True, slots=True)
class FactLine:
    """One fact as the file has it."""

    kind: FactKind
    #: The fact's words; a tie's relation ("sister"); a birthday's date as written.
    text: str
    #: Its number: which fact it is. None for a line the user added.
    id: int | None
    #: A tie: the other person's name, as written.
    other: str | None = None
    birthday: Birthday | None = None
    #: The whole line as written (without its source and number): a "tie" to no one Enzo
    #: knows ("Head of: marketing") is a note in these words.
    raw: str = ""


@dataclass(slots=True)
class Section:
    """One person's heading and the lines under it."""

    name: str
    id: int | None
    #: None: no "Also called" line.
    aliases: list[str] | None = None
    lines: list[FactLine] = field(default_factory=list)


@dataclass(slots=True)
class Parsed:
    #: When Enzo wrote the copy the user edited; None when that line is gone.
    written_at: datetime | None = None
    sections: list[Section] = field(default_factory=list)
    #: The user's lines that Enzo cannot read, kept for them under "Not saved".
    kept: list[str] = field(default_factory=list)
    #: Some of those were elsewhere in the file: they move under "Not saved".
    moved: bool = False
    #: Enzo's notes from earlier reads ("> …"), kept until the user deletes them.
    notes: list[str] = field(default_factory=list)
    #: Numbers of fact lines left with no words, or whose words Enzo could not read: those
    #: facts stay as they are.
    emptied: list[int] = field(default_factory=list)
    #: Fact numbers whose line could not be read (a birthday that is not a date).
    unreadable: list[int] = field(default_factory=list)

    def numbers(self) -> set[int]:
        """Every fact number in the file, wherever it is."""
        found = {line.id for s in self.sections for line in s.lines if line.id is not None}
        return found | set(self.emptied) | set(self.unreadable)


def parse(body: str) -> Parsed:  # noqa: C901, PLR0912 - one pass over the lines, case by case
    out = Parsed()
    section: Section | None = None
    in_not_saved = False
    item: list[str] | None = None

    def close() -> None:
        nonlocal item
        if item is not None:
            whole = " ".join(item)
            if section is None:
                out.kept.append(f"- {whole}")  # a fact with no one to be about
                out.moved = True
            else:
                _add(out, section, whole)
        item = None

    for raw in body.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            close()
            continue
        if stamp := _STAMP.match(stripped):
            close()
            out.written_at = _when(stamp.group(1))
            continue
        if heading := _HEADING.match(stripped):
            close()
            if len(heading.group(1)) == 1:
                continue  # the title
            title = " ".join(heading.group(2).split())
            in_not_saved = title.casefold() == NOT_SAVED.casefold()
            section = None
            if not in_not_saved and title:
                numbered = _NUMBERED.match(title)
                name, number = (
                    (numbered.group(1).strip(), int(numbered.group(2)))
                    if numbered
                    else (title, None)
                )
                section = Section(name=name, id=number)
                out.sections.append(section)
            continue
        if not in_not_saved and section is not None and (also := _ALSO.match(stripped)):
            close()
            names = [" ".join(n.split()) for n in also.group(1).split(",")]
            section.aliases = [*(section.aliases or []), *(n for n in names if n)]
            continue
        if not in_not_saved and (found := _ITEM.match(line)):
            close()
            item = [found.group(1)]
            continue
        if item is not None and line[:1] in (" ", "\t"):
            item.append(stripped)  # a long line the editor wrapped
            continue
        close()
        if stripped in _OWN:
            continue
        if in_not_saved and stripped.startswith(">"):
            out.notes.append(stripped)
            continue
        out.kept.append(stripped)
        out.moved = out.moved or not in_not_saved
    close()
    return out


def _add(out: Parsed, section: Section, body: str) -> None:
    number: int | None = None
    if tag := _TAG.search(body):
        number = int(tag.group(1))
        body = body[: tag.start()]
    body = " ".join(body.split())
    if not body:
        if number is not None:
            out.emptied.append(number)
        return
    kind: FactKind = "other"
    text, other = body, None
    if labelled := _LABEL.match(body):
        label, value = labelled.group(1).strip(), labelled.group(2).strip()
        known = _KINDS.get(label.casefold())
        tie = _TIE.match(label)
        if known is not None and value:
            kind, text = known, value
        elif tie is not None and value:
            kind, text, other = "tie", tie.group(1).casefold(), value
    if not text:
        if number is not None:
            out.emptied.append(number)
        return
    birthday = None
    if kind == "birthday":
        try:
            birthday = parse_birthday(text)
        except PeopleRefusedError:
            out.kept.append(f"- {body}")  # not a date: kept for the user, the fact stays
            out.moved = True
            if number is not None:
                out.unreadable.append(number)
            return
        text = birthday.words()
    section.lines.append(FactLine(kind, text, number, other, birthday, body))


def _when(value: str) -> datetime | None:
    try:
        found = datetime.fromisoformat(value)
    except ValueError:
        return None
    return found if found.tzinfo else None


def render(
    people: Sequence[Person],
    *,
    written_at: datetime,
    kept: Sequence[str] = (),
    notes: Sequence[str] = (),
) -> str:
    """Everyone, by name, each with their other names and facts; then what was not saved."""
    stamp = (
        f"<!-- Enzo wrote this copy at {written_at.isoformat()}. Leave this line as it is: it "
        "tells your edits apart from Enzo's. -->"
    )
    lines = [TITLE, "", stamp, "", *INTRO]
    if not people:
        lines += ["", EMPTY]
    for person in sorted(people, key=lambda p: (p.name.casefold(), p.id)):
        lines += ["", f"## {person.name} (#{person.id})", ""]
        if person.aliases:
            lines.append(f"Also called: {', '.join(person.aliases)}")
        for kind in ORDER:
            for fact in (f for f in person.facts if f.kind == kind):
                lines.append(f"- {fact.line()} _({provenance(fact)}; #{fact.id})_")
    if kept or notes:
        lines += ["", f"## {NOT_SAVED}", "", *NOT_SAVED_INTRO]
        if notes:
            lines += ["", *notes]
        if kept:
            lines += ["", *kept]
    return "\n".join(lines) + "\n"


__all__ = ["LABELS", "PEOPLE_FILE", "FactLine", "Parsed", "Section", "parse", "render"]
