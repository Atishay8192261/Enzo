"""The outbound check (spec §12.5, ADR 0029): before text leaves for a third party, Enzo looks for
private details in it and says so on the card, or asks first when the send would not have asked.

It reads what Enzo knows is private each time (the user's memory, this chat, their rules), so a
memory or rule added a minute ago counts. It never blocks: a gated send is noted on its card and
the user decides; an ungated one (a search, a page address, a typed field) waits for a yes.
"""

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from enzo.brain.models import Message, TextPart
from enzo.context import current_conversation
from enzo.memory import Memory
from enzo.privacy.detect import (
    LABELS,
    Finding,
    Known,
    Person,
    Remembered,
    addresses_in,
    detect_all,
    is_name,
    people_in,
    relation_of,
)
from enzo.privacy.rules import Rules
from enzo.tools.registry import Outgoing

log = logging.getLogger("enzo.privacy")

#: Most lines a card shows; the rest are counted.
MAX_LINES = 5
UNCHECKED = "Enzo could not check this for private details, so it asks first."


@dataclass(frozen=True, slots=True)
class Verdict:
    #: The card's lines: "This includes a health detail: “my wife is pregnant”".
    lines: tuple[str, ...] = ()
    #: A send that would not ask on its own must ask first.
    ask: bool = False
    #: What was found, in words for the brain ("a health detail"); never the details.
    kinds: tuple[str, ...] = ()


class Discretion:
    """Knows what is private for the user, and checks what is about to be sent."""

    def __init__(self, rules: Rules, memory: Memory | None = None) -> None:
        self.rules = rules
        self._memory = memory

    async def check(self, outgoing: Sequence[Outgoing], history: Sequence[Message]) -> Verdict:
        if not any(o.text.strip() for o in outgoing):
            return Verdict()
        try:
            known = await self.known(history)
        except Exception:  # a check that cannot run never lets a send through unasked
            log.exception("privacy.unchecked")
            return Verdict(lines=(UNCHECKED,), ask=True, kinds=("details Enzo could not check",))
        found = detect_all(outgoing, known)
        if not found:
            return Verdict()
        kinds = sorted({f.kind for f in found})
        log.info(
            "privacy.flagged",
            extra={
                "kinds": kinds,
                "details": len(found),
                "rules": sorted({f.rule.id for f in found if f.rule}),
                "channels": sorted({o.channel for o in outgoing}),
            },
        )
        return Verdict(
            lines=card_lines(found),
            ask=any(f.asks for f in found),
            kinds=tuple(dict.fromkeys(LABELS[f.kind] for f in _ordered(found))),
        )

    async def known(self, history: Sequence[Message]) -> Known:
        """What is private right now: memories, names and addresses from this chat, rules."""
        facts = await self._memory.all() if self._memory is not None else []
        rules = await self.rules.applying(current_conversation())
        said = list(user_words(history))
        people: dict[str, Person] = {}
        for fact in facts:
            if fact.kind == "person" and fact.about and is_name(fact.about):
                people[fact.about] = Person(fact.about, relation_of(fact.text), fact.id)
            for p in people_in([fact.text], loose=True):
                people.setdefault(p.name, Person(p.name, p.relation, fact.id))
        for p in people_in(said):
            people.setdefault(p.name, p)
        return Known(
            people=tuple(people.values()),
            addresses=tuple(addresses_in([*(f.text for f in facts), *said])),
            memories=tuple(Remembered(f.id, f.text) for f in facts),
            rules=tuple(r.terms for r in rules),
        )


def user_words(history: Iterable[Message]) -> Iterable[str]:
    """What the user typed in this chat: each user message's own text (not the context notes
    and memories that ride with it)."""
    for message in history:
        if message.role == "user" and message.parts and isinstance(message.parts[0], TextPart):
            yield message.parts[0].text


def card_lines(found: Sequence[Finding]) -> tuple[str, ...]:
    """The card's words: what the action includes, the user's rules first."""
    ordered = _ordered(found)
    lines = [_line(f) for f in ordered[:MAX_LINES]]
    rest = len(ordered) - MAX_LINES
    if rest > 0:
        lines.append(f"…and {rest} more private detail{'s' if rest != 1 else ''}.")
    return tuple(lines)


def _ordered(found: Sequence[Finding]) -> list[Finding]:
    return sorted(found, key=lambda f: (f.rule is None, not f.asks))


def _line(f: Finding) -> str:
    label = LABELS[f.kind]
    if f.rule is not None:
        rule = f"“{f.rule.text}”"
        head = (
            f"This includes something you asked Enzo to keep private ({rule})"
            if f.kind == "rule"
            else f"This includes {label}, which you asked Enzo to keep private ({rule})"
        )
    else:
        head = f"This includes {label}"
    words = f": “{f.words}”" if f.words else ""
    if f.relation:
        words += f" ({f.relation})"
    elif f.kind == "memory" and f.memory is not None:
        words += f" (memory #{f.memory})"
    return head + words
