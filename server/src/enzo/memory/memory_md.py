"""``Memory.md``: Enzo's memory as a file the user reads and edits by hand (spec §11.2, §15.2).

Enzo writes it from the memory table; the user may change it in any editor. The format is plain
Markdown that a person can follow:

- one memory per ``- `` line, under the heading of its kind, ending with where it came from and
  its number: ``- Prefers aisle seats. _(you said, 20 Nov 2026; #3)_``. The number tells Enzo
  which memory a line is, so a changed line corrects that memory;
- a comment near the top says when Enzo wrote this copy. Lines missing from the copy count as
  removed only if their memory existed then: an editor that saves an old copy never makes Enzo
  forget what it learned since;
- lines Enzo cannot read as a memory are kept for the user under "Not saved", with Enzo's notes
  on what it did not save (never the words of a refused secret).
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from enzo.memory.facts import KINDS, Fact, Kind, provenance

MEMORY_FILE = "Memory.md"
TITLE = "# What Enzo remembers"
NOT_SAVED = "Not saved"
INTRO = (
    "You can edit this file. Enzo reads your changes before your next message: change a line to",
    "correct it, add a line under a heading to teach Enzo something, delete a line to make it",
    "forget. Keep the #number at the end of each line: it tells Enzo which memory the line is.",
)
EMPTY = "Nothing yet."
NOT_SAVED_INTRO = (
    "Enzo did not save the lines below. Delete a note once you have read it. To make a line a",
    "memory, start it with “- ” and move it under one of the headings above.",
)
#: The introduction of earlier versions of this file, so upgrading never mistakes it for an edit.
_EARLIER = (
    'Enzo writes this file from its memory. To change something, tell Enzo ("I now prefer',
    'aisle seats", "forget my old address") or use /memory in the terminal app. Edits',
    "made here are replaced the next time Enzo learns something.",
)
_OWN = {TITLE, *INTRO, EMPTY, *NOT_SAVED_INTRO, *_EARLIER}

_STAMP = re.compile(r"^<!-- Enzo wrote this copy at (\S+?)\. .*-->$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_ITEM = re.compile(r"^ {0,3}[-*+]\s+(.*)$")
#: " _(you said, 20 Nov 2026, in “Trip”; #3)_" at the end of a line: its memory's number.
_TAG = re.compile(r"\s*_\(.*#(\d+)\)_\s*$")
_ABOUT = re.compile(r"^\*\*(.+?)\*\*:\s*(.*)$")
_HEADINGS: dict[str, Kind] = {
    **{heading.casefold(): kind for kind, heading in KINDS.items()},
    **{kind: kind for kind in KINDS},
}


@dataclass(frozen=True, slots=True)
class Line:
    """One memory as the file has it."""

    kind: Kind
    about: str | None
    text: str
    #: The number at its end: which memory it is. None for a line the user added.
    id: int | None

    def same_as(self, kind: str, about: str | None, text: str) -> bool:
        return (self.kind, self.about, self.text) == (kind, about, text)


@dataclass(slots=True)
class Parsed:
    #: When Enzo wrote the copy the user edited; None when that line is gone.
    written_at: datetime | None = None
    lines: list[Line] = field(default_factory=list)
    #: The user's lines that are not memories Enzo can read, kept for them under "Not saved".
    kept: list[str] = field(default_factory=list)
    #: Some of those were elsewhere in the file: they move under "Not saved".
    moved: bool = False
    #: Enzo's notes from earlier reads ("> …"), kept until the user deletes them.
    notes: list[str] = field(default_factory=list)
    #: Numbers of lines left with no words: those memories stay as they are.
    emptied: list[int] = field(default_factory=list)


def parse(body: str) -> Parsed:
    out = Parsed()
    section: Kind | None = None  # None: the top of the file, before the first heading
    in_not_saved = False
    item: list[str] | None = None
    item_kind: Kind = "fact"

    def close() -> None:
        nonlocal item
        if item is not None:
            _add(out, item_kind, " ".join(item))
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
            name = heading.group(2).casefold()
            in_not_saved = name == NOT_SAVED.casefold()
            section = None if in_not_saved else _HEADINGS.get(name, "fact")
            continue
        if found := _ITEM.match(line):
            close()
            item, item_kind = [found.group(1)], section or "fact"
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


def _add(out: Parsed, kind: Kind, body: str) -> None:
    number: int | None = None
    if tag := _TAG.search(body):
        number = int(tag.group(1))
        body = body[: tag.start()]
    about: str | None = None
    if who := _ABOUT.match(body.strip()):
        about, body = " ".join(who.group(1).split()) or None, who.group(2)
    text = " ".join(body.split())
    if not text:
        if number is not None:
            out.emptied.append(number)
        return
    out.lines.append(Line(kind=kind, about=about, text=text, id=number))


def _when(value: str) -> datetime | None:
    try:
        found = datetime.fromisoformat(value)
    except ValueError:
        return None
    return found if found.tzinfo else None


def render(
    facts: Sequence[Fact],
    *,
    written_at: datetime,
    kept: Sequence[str] = (),
    notes: Sequence[str] = (),
) -> str:
    """Every current memory by kind, each with where it came from, then what was not saved."""
    stamp = (
        f"<!-- Enzo wrote this copy at {written_at.isoformat()}. Leave this line as it is: it "
        "tells your edits apart from Enzo's. -->"
    )
    lines = [TITLE, "", stamp, "", *INTRO]
    if not facts:
        lines += ["", EMPTY]
    for kind, heading in KINDS.items():
        group = sorted((f for f in facts if f.kind == kind), key=lambda f: f.learned_at)
        if not group:
            continue
        lines += ["", f"## {heading}", ""]
        for f in group:
            who = f"**{f.about}**: " if f.about else ""
            lines.append(f"- {who}{f.text} _({provenance(f)}; #{f.id})_")
    if kept or notes:
        lines += ["", f"## {NOT_SAVED}", "", *NOT_SAVED_INTRO]
        if notes:
            lines += ["", *notes]
        if kept:
            lines += ["", *kept]
    return "\n".join(lines) + "\n"
