"""What one memory is (spec §6.2, §6.5): a durable fact, who said it, when and where."""

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

type Kind = Literal["about", "preference", "person", "decision", "fact"]
type SaidBy = Literal["user", "inferred"]

KINDS: dict[Kind, str] = {
    "about": "About you",
    "preference": "Preferences",
    "person": "People",
    "decision": "Decisions",
    "fact": "Other facts",
}
#: Where a memory was learned when the user wrote it in Memory.md by hand, not in a chat run.
HAND_EDIT = "Memory.md"


@dataclass(frozen=True, slots=True)
class Fact:
    id: int
    text: str
    kind: Kind
    about: str | None
    said_by: SaidBy
    happened_on: date | None
    learned_at: datetime
    #: The chat it was learned in, by its title, when that chat still exists.
    chat: str | None
    conversation_id: uuid.UUID | None
    #: The user wrote or changed it in Memory.md by hand (spec §11.2).
    edited_by_hand: bool = False


def provenance(fact: Fact) -> str:
    """Where a memory came from, in words: "you said, 20 Nov 2026, in “Trip to NYC”"."""
    if fact.edited_by_hand:
        who = "edited by hand in Memory.md"
    else:
        who = "you said" if fact.said_by == "user" else "Enzo inferred"
    parts = [who, f"{fact.learned_at:%-d %b %Y}"]
    if fact.happened_on:
        parts.append(f"happened {fact.happened_on:%-d %b %Y}")
    if fact.chat:
        parts.append(f"in “{fact.chat}”")
    return ", ".join(parts)
