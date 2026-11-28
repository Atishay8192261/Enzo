"""Discretion rules (spec §12.5): what the user told Enzo never to share, in a chat or everywhere.

A rule is stored and changed only when the user states it: the brain must quote the user's own
words from the message it is answering, and the quote is checked here, in code. So a rule is
never inferred, and a web page or an email cannot make or lift one. Rules ride with every message
(the brain follows them) and the outbound check applies them to everything sent out (ADR 0029).
"""

import logging
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, false, or_, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from enzo.context import current_conversation, turn_var
from enzo.privacy.detect import RULE_KINDS, RuleKind, RuleTerms
from enzo.store.schema import Conversation, DiscretionRuleRow

log = logging.getLogger("enzo.privacy")

#: The shortest quote that can state a rule ("never mention work").
MIN_QUOTE = 10

#: How each kind reads in a list of rules.
KIND_WORDS: dict[RuleKind, str] = {
    "health": "health",
    "money": "money",
    "people": "the people in your life",
    "relationships": "relationships",
    "beliefs": "religion and politics",
    "location": "where you live",
    "ids": "ID numbers",
}


class RuleRefusedError(Exception):
    """A plain-language reason a rule was not saved or removed, for the brain to relay."""


@dataclass(frozen=True, slots=True)
class Rule:
    id: int
    text: str
    kinds: tuple[RuleKind, ...]
    words: tuple[str, ...]
    #: The chat it holds in; None: every chat.
    conversation_id: uuid.UUID | None
    #: That chat's title, when it has one.
    chat: str | None
    quote: str
    created_at: datetime

    @property
    def terms(self) -> RuleTerms:
        return RuleTerms(self.id, self.text, frozenset(self.kinds), self.words)

    @property
    def scope(self) -> str:
        if self.conversation_id is None:
            return "every chat"
        return f"the chat “{self.chat}”" if self.chat else "one chat"

    def covers(self) -> str:
        """What it covers, in words: "health, and the words “work”, “Acme”"."""
        parts = [KIND_WORDS[k] for k in self.kinds]
        if self.words:
            parts.append("the words " + ", ".join(f"“{w}”" for w in self.words))
        return ", ".join(parts)


#: Curly quotes and long dashes, as the plain marks a quote may use instead.
_PLAIN_MARKS = str.maketrans(
    {
        **dict.fromkeys(map(chr, (0x2018, 0x2019, 0x201A, 0x201B)), "'"),
        **dict.fromkeys(map(chr, (0x201C, 0x201D, 0x201E, 0x201F)), '"'),
        **dict.fromkeys(map(chr, (0x2013, 0x2014)), "-"),
    }
)


def normalized(text: str) -> str:
    """For comparing a quote with the message: case, quote marks and spacing do not matter."""
    folded = text.casefold().translate(_PLAIN_MARKS)
    return " ".join(re.sub(r"[^\w'\"-]+", " ", folded).split())


def said_by_user(quote: str, message: str | None) -> bool:
    """The quote is the user's own words, from the message Enzo is answering."""
    wanted = normalized(quote)
    return bool(message) and len(wanted) >= MIN_QUOTE and wanted in normalized(message or "")


class Rules:
    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def add(
        self,
        text: str,
        *,
        kinds: Sequence[RuleKind],
        words: Sequence[str],
        conversation_id: uuid.UUID | None,
        quote: str,
        replaces: int | None = None,
    ) -> Rule:
        text = " ".join(text.split())
        kinds = [k for k in RULE_KINDS if k in kinds]  # known kinds, in a fixed order
        words = list(dict.fromkeys(" ".join(w.split()) for w in words if w.strip()))
        if not kinds and not words:
            msg = "Not saved: a rule needs the kinds of detail or the words it keeps private."
            raise RuleRefusedError(msg)
        async with self._sessions.begin() as session:
            if replaces is not None:
                old = await session.get(DiscretionRuleRow, replaces, with_for_update=True)
                if old is None:
                    msg = f"There is no rule {replaces} to replace: the rules are listed above."
                    raise RuleRefusedError(msg)
                await session.delete(old)
            row = DiscretionRuleRow(
                text=text,
                kinds=kinds,
                words=words,
                conversation_id=conversation_id,
                quote=" ".join(quote.split()),
                said_in=current_conversation(),
                turn_id=turn_var.get(),
            )
            session.add(row)
            await session.flush()
            saved = row.id
        log.info(
            "privacy.rule_saved",
            extra={
                "rule": saved,
                "kinds": kinds,
                "words": len(words),
                "everywhere": conversation_id is None,
                "replaces": replaces,
            },
        )
        (rule,) = await self._rules(DiscretionRuleRow.id == saved)
        return rule

    async def remove(self, rule_id: int) -> Rule:
        found = await self._rules(DiscretionRuleRow.id == rule_id)
        if not found:
            msg = f"There is no rule {rule_id}."
            raise RuleRefusedError(msg)
        async with self._sessions.begin() as session:
            await session.execute(delete(DiscretionRuleRow).where(DiscretionRuleRow.id == rule_id))
        log.info("privacy.rule_removed", extra={"rule": rule_id})
        return found[0]

    async def applying(self, conversation_id: uuid.UUID | None) -> list[Rule]:
        """The rules that hold in this chat: its own and the ones for every chat, oldest first."""
        here = (
            DiscretionRuleRow.conversation_id == conversation_id
            if conversation_id is not None
            else false()
        )
        return await self._rules(or_(DiscretionRuleRow.conversation_id.is_(None), here))

    async def get(self, rule_id: int) -> Rule | None:
        found = await self._rules(DiscretionRuleRow.id == rule_id)
        return found[0] if found else None

    async def _rules(self, *where: object) -> list[Rule]:
        async with self._sessions() as session:
            rows = await session.execute(
                select(DiscretionRuleRow, Conversation.title)
                .outerjoin(Conversation, Conversation.id == DiscretionRuleRow.conversation_id)
                .where(*where)  # type: ignore[arg-type]
                .order_by(DiscretionRuleRow.created_at, DiscretionRuleRow.id)
            )
            return [_rule(row, title) for row, title in rows]


def rules_note(rules: Sequence[Rule]) -> str | None:
    """What the brain is told with each message: the rules that hold in this chat."""
    if not rules:
        return None
    lines = [
        f"- #{r.id} ({'every chat' if r.conversation_id is None else 'this chat only'}): "
        f"{r.text} (covers {r.covers()})"
        for r in rules
    ]
    return (
        "Discretion rules the user set: never share what they cover with anyone outside this "
        "chat (emails, invites, forms, posts, searches) unless the user asks for that "
        "detail to go out in this message.\n" + "\n".join(lines)
    )


def _rule(row: DiscretionRuleRow, chat: str | None) -> Rule:
    return Rule(
        id=row.id,
        text=row.text,
        kinds=tuple(k for k in RULE_KINDS if k in row.kinds),
        words=tuple(row.words),
        conversation_id=row.conversation_id,
        chat=chat,
        quote=row.quote,
        created_at=row.created_at,
    )
