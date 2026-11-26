"""What the brain is sent: the full conversation, seen through the latest summary (ADR 0024).

The stored transcript never changes. The prompt is a pure function of it and the latest summary
record, so it is byte-identical on every later call: provider caches stay warm between
compactions, and reasoning replayed from after a compaction stays valid.

- Turns: a turn starts at a user message (a scheduled run's message counts as one). Leading system
  messages (instructions) are not part of any turn.
- Turns the summary covers are left out; the summary takes their place, as a user-role message
  right after the instructions (facts, not instructions: it holds the user's and pages' words).
- Kept turns that existed when the summary was made lose their provider replay data (Claude's
  thinking, OpenAI's reasoning items): it was produced with the full history before it and would
  fail the providers' prefix checks. Their words and tool calls stay exactly as they were.
- A pasted message longer than the paste limit is replaced by its start and a pointer to the
  stored whole (the same id every time: the text's hash).
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from enzo.brain.models import Message, NativePart, TextPart
from enzo.compaction.summary import Summary, render

type Shorten = Callable[[str], str]


@dataclass(frozen=True, slots=True)
class Compaction:
    """One summary of older turns, as stored (one row per compaction)."""

    summary: Summary
    #: Turns [0, turns) are summarized; the prompt replays turns from ``turns`` on.
    turns: int
    #: When it was made: in turn ``made_turn``, after its first ``made_offset`` messages.
    #: Messages from before that moment lose their replay data in every later prompt.
    made_turn: int
    made_offset: int
    #: The brain that wrote it, and the prompt's size before and after, in tokens.
    brain: str
    before: int
    after: int


def split_lead(messages: Sequence[Message]) -> tuple[list[Message], list[Message]]:
    """The leading instructions (system messages) and the conversation after them."""
    lead = 0
    while lead < len(messages) and messages[lead].role == "system":
        lead += 1
    return list(messages[:lead]), list(messages[lead:])


def turn_positions(conversation: Sequence[Message]) -> list[tuple[int, int]]:
    """(turn, offset within the turn) of each message. Anything before the first user message
    belongs to turn 0."""
    out: list[tuple[int, int]] = []
    turn, offset = 0, 0
    seen_user = False
    for message in conversation:
        if message.role == "user":
            if seen_user:
                turn, offset = turn + 1, 0
            seen_user = True
        out.append((turn, offset))
        offset += 1
    return out


def turn_count(conversation: Sequence[Message]) -> int:
    positions = turn_positions(conversation)
    return positions[-1][0] + 1 if positions else 0


def without_replay(message: Message) -> Message | None:
    """The message without provider replay data; None if nothing else was in it."""
    parts = tuple(p for p in message.parts if not isinstance(p, NativePart))
    if len(parts) == len(message.parts):
        return message
    if not parts:
        return None
    return Message(role=message.role, parts=parts)


def summary_message(summary: Summary) -> Message:
    return Message.user(render(summary))


def build_view(
    messages: Sequence[Message],
    record: Compaction | None,
    *,
    paste_limit: int | None = None,
    shorten: Shorten | None = None,
) -> list[Message]:
    """The prompt for ``messages`` (instructions + conversation) under ``record``."""
    lead, conversation = split_lead(messages)
    view = list(lead)
    if record is not None:
        view.append(summary_message(record.summary))
    for stored, (turn, offset) in zip(conversation, turn_positions(conversation), strict=True):
        message: Message | None = stored
        if record is not None:
            if turn < record.turns:
                continue
            if turn < record.made_turn or (
                turn == record.made_turn and offset < record.made_offset
            ):
                message = without_replay(stored)
        if message is None:  # nothing but replay data in it
            continue
        if message.role == "user" and shorten is not None and paste_limit is not None:
            message = _shorten_paste(message, paste_limit, shorten)
        view.append(message)
    return view


def _shorten_paste(message: Message, limit: int, shorten: Shorten) -> Message:
    first = message.parts[0]
    if not isinstance(first, TextPart) or len(first.text) <= limit:
        return message
    return Message(role="user", parts=(TextPart(text=shorten(first.text)), *message.parts[1:]))
