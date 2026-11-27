"""Cutting what Enzo knows into retrievable pieces (spec §6.3 step 1), and rebuilding a piece from
its source again, which is how a hit is checked before use (§6.3 step 5).

- A memory is one piece: who it is about and the fact, as full-text search reads it.
- A goal is one piece: its title, status, due date and steps.
- A past chat is cut a turn at a time: the user's words and Enzo's reply (never tool results:
  outside content stays out of recall), cut further when long.
- A long stored text (a page, a file, a paste) is cut into overlapping windows.

Everything here is a pure function of its source, so the same source always gives the same
pieces, and a piece that no longer matches its source is known to be stale.
"""

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime

from enzo.brain.models import Message, TextPart

#: Characters per piece. The local model reads 256 tokens; 800 characters of English fit.
CHUNK_CHARS = 800
#: Neighbouring pieces share this much, so a sentence cut in two is whole in one of them.
OVERLAP = 120
#: A turn is indexed up to this many pieces, a stored text up to this many (~160K characters);
#: the rest stays readable from its source, by words, just not found by meaning.
TURN_PIECES = 12
STORED_PIECES = 200


@dataclass(frozen=True, slots=True)
class Piece:
    #: Where it starts in its source's text, in characters.
    start: int
    text: str

    @property
    def digest(self) -> str:
        return digest(self.text)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def split(text: str, *, size: int = CHUNK_CHARS, overlap: int = OVERLAP) -> list[Piece]:
    """Windows of at most ``size`` characters, cut at a space, overlapping by about ``overlap``.

    Offsets refer to ``text`` itself, so ``text[p.start : p.start + len(p.text)] == p.text``,
    and every character of ``text`` is in some piece.
    """
    pieces: list[Piece] = []
    start, length = 0, len(text)
    while start < length:
        end = min(length, start + size)
        if end < length:  # cut after the last space in the second half of the window
            space = max(text.rfind(c, start + size // 2, end) for c in " \n\t")
            if space > start:
                end = space + 1
        if text[start:end].strip():
            pieces.append(Piece(start, text[start:end]))
        if end >= length:
            break
        # The next one starts at a word about ``overlap`` before this one ends.
        back = max(start + 1, end - overlap)
        space = max(text.rfind(c, max(start + 1, back - overlap), back) for c in " \n\t")
        start = space + 1 if space >= start + 1 else back
    return pieces


def memory_text(text: str, about: str | None) -> str:
    """A memory as one line, as full-text search reads it: "Ana: Is allergic to peanuts."."""
    return f"{about}: {text}" if about else text


_STATUS = {"open": "open", "done": "done", "dropped": "given up"}


def goal_text(
    title: str,
    status: str,
    due_on: date | None,
    steps: Sequence[tuple[str, bool]],
) -> str:
    """A goal in words: title, status, due date, then its steps done and left."""
    head = f"Goal: {title} ({_STATUS.get(status, status)}"
    if due_on is not None:
        head += f", due {due_on.isoformat()}"
    lines = [head + ")"]
    done = [t for t, finished in steps if finished]
    left = [t for t, finished in steps if not finished]
    if done:
        lines.append("Done: " + "; ".join(done))
    if left:
        lines.append("Left: " + "; ".join(left))
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class Turn:
    """One exchange of a chat: a message and what Enzo said back, as saved."""

    seq_from: int
    seq_to: int
    at: datetime
    text: str
    #: A scheduled task's run started it, not the user typing.
    scheduled: bool

    def pieces(self) -> list[Piece]:
        return split(self.text)[:TURN_PIECES]


@dataclass(frozen=True, slots=True)
class Saved:
    """A saved message as the index reads it."""

    seq: int
    message: Message
    at: datetime
    scheduled: bool = False


def turns(messages: Sequence[Saved]) -> list[Turn]:
    """A chat's turns: each starts at a message with words from the user (or a schedule)."""
    out: list[Turn] = []
    current: list[Saved] = []
    for saved in messages:
        if _words_of(saved.message) is not None and current:
            out.append(_turn(current))
            current = []
        current.append(saved)
    if current:
        out.append(_turn(current))
    return [t for t in out if t.text]


def _turn(messages: Sequence[Saved]) -> Turn:
    first = messages[0]
    asked = _words_of(first.message)
    said = [
        p.text.strip()
        for m in messages
        if m.message.role == "assistant"
        for p in m.message.parts
        if isinstance(p, TextPart) and p.text.strip()
    ]
    lines = []
    if asked:
        lines.append(f"{'Scheduled task' if first.scheduled else 'User'}: {asked}")
    if said:
        lines.append("Enzo: " + "\n".join(said))
    return Turn(
        seq_from=first.seq,
        seq_to=messages[-1].seq,
        at=first.at,
        text="\n".join(lines),
        scheduled=first.scheduled,
    )


def _words_of(message: Message) -> str | None:
    """What the user typed: a user message's first text part. The parts after it are Enzo's
    own context (time, recalled memory, notes), never indexed as the user's words."""
    if message.role != "user":
        return None
    first = next((p for p in message.parts if isinstance(p, TextPart)), None)
    return first.text.strip() if first is not None else None
