"""Curated long-term memory (spec §6.2): durable facts, each with who said it, when, and where.

- **Write:** one fact per memory, in plain words. A correction is a new memory that names the one
  it replaces; the old one is kept as replaced and never recalled again (§6.2: never keep a
  superseded fact alongside the new one as if both were true).
- **Forget:** deletes the memory and the versions it replaced. Forgetting means gone.
- **Recall:** Postgres full-text search (English stemming) over the text and who it is about.
  Any word of the query may match; better matches come first. No extension is needed, so the
  bundled database has it too. Retrieval by meaning (§6.3, vectors) is a later step.
- **Never secrets** (§6.2): ``guard.secret_reason`` refuses them before anything is written.
- **Readable copy:** after every change, ``Memory.md`` in the user's Enzo folder is rewritten
  from the table (§15.2: a memory you can inspect).
"""

import asyncio
import logging
import os
import tempfile
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from enzo.context import current_conversation, turn_var
from enzo.memory.guard import secret_reason
from enzo.store.schema import Conversation, MemoryRow

log = logging.getLogger("enzo.memory")

type Kind = Literal["about", "preference", "person", "decision", "fact"]
type SaidBy = Literal["user", "inferred"]

KINDS: dict[Kind, str] = {
    "about": "About you",
    "preference": "Preferences",
    "person": "People",
    "decision": "Decisions",
    "fact": "Other facts",
}
MEMORY_FILE = "Memory.md"
#: How many memories ride with each message at most (spec §3 step 2: only the top relevant).
RECALL = 6


class MemoryRefusedError(Exception):
    """A plain-language reason a memory was not saved or changed, for the brain to relay."""


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


class Memory:
    def __init__(self, engine: AsyncEngine, workspace: Path | None = None) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)
        self._workspace = workspace

    @property
    def file(self) -> Path | None:
        return self._workspace / MEMORY_FILE if self._workspace else None

    async def remember(
        self,
        text: str,
        *,
        kind: Kind,
        about: str | None = None,
        said_by: SaidBy = "user",
        happened_on: date | None = None,
        replaces: int | None = None,
    ) -> Fact:
        text = " ".join(text.split())
        about = " ".join(about.split()) if about else None
        reason = secret_reason(f"{about or ''} {text}")
        if reason:
            msg = f"Not saved: {reason}, and Enzo never keeps those in its memory."
            raise MemoryRefusedError(msg)
        async with self._sessions.begin() as session:
            if replaces is not None:
                old = await session.get(MemoryRow, replaces, with_for_update=True)
                if old is None or old.status != "active":
                    msg = f"There is no current memory {replaces} to replace: search first."
                    raise MemoryRefusedError(msg)
                old.status = "replaced"
            row = MemoryRow(
                text=text,
                kind=kind,
                about=about,
                said_by=said_by,
                happened_on=happened_on,
                replaces=replaces,
                conversation_id=current_conversation(),
                turn_id=turn_var.get(),
            )
            session.add(row)
            await session.flush()
            saved = row.id
        log.info(
            "memory.saved",
            extra={"memory": saved, "kind": kind, "said_by": said_by, "replaces": replaces},
        )
        await self._write_file()
        (fact,) = await self._facts(MemoryRow.id == saved)
        return fact

    async def forget(self, memory_id: int) -> Fact:
        """Delete a current memory and every version before it."""
        found = await self._facts(MemoryRow.id == memory_id)
        if not found:
            msg = f"There is no current memory {memory_id}."
            raise MemoryRefusedError(msg)
        async with self._sessions.begin() as session:
            chain = [memory_id]
            row = await session.get(MemoryRow, memory_id)
            while row is not None and row.replaces is not None:
                chain.append(row.replaces)
                row = await session.get(MemoryRow, row.replaces)
            await session.execute(delete(MemoryRow).where(MemoryRow.id.in_(chain)))
        log.info("memory.forgotten", extra={"memory": memory_id, "versions": len(chain)})
        await self._write_file()
        return found[0]

    async def all(self) -> list[Fact]:
        """Every current memory, newest first."""
        return await self._facts()

    async def search(self, query: str, limit: int = RECALL) -> list[Fact]:
        """Current memories matching any word of ``query``, best match first."""
        async with self._sessions() as session:
            ids = (
                await session.execute(
                    text(
                        """
                        WITH q AS (
                            SELECT to_tsquery('english', string_agg(quote_literal(l), ' | '))
                                AS query
                            FROM unnest(tsvector_to_array(to_tsvector('english', :query))) AS l
                        )
                        SELECT m.id FROM memories AS m, q
                        WHERE m.status = 'active' AND q.query IS NOT NULL
                          AND m.search @@ q.query
                        ORDER BY ts_rank_cd(m.search, q.query) DESC, m.created_at DESC, m.id DESC
                        LIMIT :limit
                        """
                    ),
                    {"query": query, "limit": limit},
                )
            ).scalars()
            order = list(ids)
        if not order:
            return []
        found = {f.id: f for f in await self._facts(MemoryRow.id.in_(order))}
        return [found[i] for i in order if i in found]

    async def recall(self, message: str) -> list[Fact]:
        """What rides with a new message: the few memories it is about (spec §3 step 2)."""
        facts = await self.search(message, RECALL)
        if facts:
            log.info("memory.recalled", extra={"memories": [f.id for f in facts]})
        return facts

    async def _facts(self, *where: object) -> list[Fact]:
        async with self._sessions() as session:
            rows = await session.execute(
                select(MemoryRow, Conversation.title)
                .outerjoin(Conversation, Conversation.id == MemoryRow.conversation_id)
                .where(MemoryRow.status == "active", *where)  # type: ignore[arg-type]
                .order_by(MemoryRow.created_at.desc(), MemoryRow.id.desc())
            )
            return [_fact(row, title) for row, title in rows]

    async def _write_file(self) -> None:
        if self.file is not None:
            await asyncio.to_thread(_write_whole, self.file, render_file(await self.all()))


def _write_whole(path: Path, body: str) -> None:
    """Write the file next to its place, then swap it in: a reader never sees half of it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".memory-", suffix=".tmp")
    try:
        # mkstemp makes the file readable by the user only: personal details stay theirs.
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(body)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def _fact(row: MemoryRow, chat: str | None) -> Fact:
    return Fact(
        id=row.id,
        text=row.text,
        kind=row.kind,  # type: ignore[arg-type]  # the table's check keeps it one of Kind
        about=row.about,
        said_by=row.said_by,  # type: ignore[arg-type]
        happened_on=row.happened_on,
        learned_at=row.created_at,
        chat=chat,
        conversation_id=row.conversation_id,
    )


def provenance(fact: Fact) -> str:
    """Where a memory came from, in words: "you said, 20 Nov 2026, in “Trip to NYC”"."""
    who = "you said" if fact.said_by == "user" else "Enzo inferred"
    when = f"{fact.learned_at:%-d %b %Y}"
    parts = [who, when]
    if fact.happened_on:
        parts.append(f"happened {fact.happened_on:%-d %b %Y}")
    if fact.chat:
        parts.append(f"in “{fact.chat}”")
    return ", ".join(parts)


def render_file(facts: Sequence[Fact]) -> str:
    """``Memory.md``: every current memory by kind, each with where it came from."""
    lines = [
        "# What Enzo remembers",
        "",
        'Enzo writes this file from its memory. To change something, tell Enzo ("I now prefer',
        'aisle seats", "forget my old address") or use /memory in the terminal app. Edits',
        "made here are replaced the next time Enzo learns something.",
    ]
    if not facts:
        lines += ["", "Nothing yet."]
    for kind, heading in KINDS.items():
        group = sorted((f for f in facts if f.kind == kind), key=lambda f: f.learned_at)
        if not group:
            continue
        lines += ["", f"## {heading}", ""]
        for f in group:
            who = f"**{f.about}**: " if f.about else ""
            lines.append(f"- {who}{f.text} _({provenance(f)}; #{f.id})_")
    return "\n".join(lines) + "\n"
