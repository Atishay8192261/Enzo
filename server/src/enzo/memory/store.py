"""Curated long-term memory (spec §6.2): durable facts, each with who said it, when, and where.

- **Write:** one fact per memory, in plain words. A correction is a new memory that names the one
  it replaces; the old one is kept as replaced and never recalled again (§6.2: never keep a
  superseded fact alongside the new one as if both were true).
- **Forget:** deletes the memory and the versions it replaced. Forgetting means gone.
- **Recall:** Postgres full-text search (English stemming) over the text and who it is about.
  Any word of the query may match; better matches come first. No extension is needed, so the
  bundled database has it too. Retrieval by meaning (§6.3, vectors) is a later step.
- **Never secrets** (§6.2): ``guard.secret_reason`` refuses them before anything is written.
- **One truth with Memory.md** (§11.2): the user may edit the file by hand. Before Enzo reads
  its memory or writes the file again, it reads their edits back (cheap: the file's size, time
  and inode first). A changed line corrects its memory, an added line is a new one, a removed
  line is forgotten: each as the user's own words, "edited by hand in Memory.md".
"""

import asyncio
import logging
import os
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from enzo.context import current_conversation, turn_var
from enzo.memory.facts import HAND_EDIT, KINDS, Fact, Kind, SaidBy, provenance
from enzo.memory.guard import secret_reason
from enzo.memory.memory_md import MEMORY_FILE, Line, Parsed, parse, render
from enzo.store.schema import Conversation, MemoryRow

log = logging.getLogger("enzo.memory")

__all__ = [
    "KINDS",
    "MEMORY_FILE",
    "RECALL",
    "Fact",
    "FileEdits",
    "Kind",
    "Memory",
    "MemoryRefusedError",
    "SaidBy",
    "provenance",
]

#: How many memories ride with each message at most (spec §3 step 2: only the top relevant).
RECALL = 6

MISSING = (
    "Memory.md was missing, so Enzo wrote it again and forgot nothing. To make Enzo forget "
    "something, delete its line, not the file."
)
UNREADABLE = (
    "Memory.md was not saved as plain text (UTF-8), so Enzo could not read the changes: it kept "
    "that copy as “{kept}” and wrote Memory.md again from its memory."
)
KEPT = (
    "Some lines in Memory.md were not memories Enzo can read (a memory is a line starting with "
    "“- ” under a heading), so they were kept under “Not saved” in the file."
)


class MemoryRefusedError(Exception):
    """A plain-language reason a memory was not saved or changed, for the brain to relay."""


@dataclass(slots=True)
class FileEdits:
    """What reading back the user's hand edits of Memory.md did."""

    changed: int = 0
    added: int = 0
    removed: int = 0
    #: In plain words, for the user: what was not saved and why (never a refused line's words).
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _Seen:
    """The file as Enzo last wrote or read it: if all of this is the same, nobody changed it."""

    mtime_ns: int
    size: int
    inode: int


class Memory:
    def __init__(self, engine: AsyncEngine, workspace: Path | None = None) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)
        self._workspace = workspace
        #: Reading edits back and writing the file never interleave (one gateway per database).
        self._lock = asyncio.Lock()
        self._seen: _Seen | None = None
        #: The user's lines Enzo could not read, and its notes: they stay in the file.
        self._kept: list[str] = []
        self._notes: list[str] = []
        #: Reading the file failed: never write over edits that were not read.
        self._unread = False

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
        async with self._lock:
            await self._read_back()  # a hand edit made meanwhile counts first, then this
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
        async with self._lock:
            await self._read_back()
            found = await self._facts(MemoryRow.id == memory_id)
            if not found:
                msg = f"There is no current memory {memory_id}."
                raise MemoryRefusedError(msg)
            async with self._sessions.begin() as session:
                versions = await _forget(session, memory_id)
            log.info("memory.forgotten", extra={"memory": memory_id, "versions": versions})
            await self._write_file()
        return found[0]

    async def all(self) -> list[Fact]:
        """Every current memory, newest first."""
        await self.read_edits()
        return await self._facts()

    async def search(self, query: str, limit: int = RECALL) -> list[Fact]:
        """Current memories matching any word of ``query``, best match first."""
        await self.read_edits()
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

    async def read_edits(self) -> FileEdits | None:
        """Read back what the user changed in Memory.md by hand since Enzo last looked.

        None when there is nothing to tell: no change, or only a change of layout. Cheap when the
        file is as Enzo left it: its size, time and inode are compared; it is not even opened.
        """
        if self.file is None:
            return None
        async with self._lock:
            return await self._read_back()

    async def _read_back(self) -> FileEdits | None:
        path = self.file
        if path is None:
            return None
        try:
            return await self._reconcile(path)
        except Exception:  # a broken read never costs the user their edit, nor the turn
            log.exception("memory.file_unread")
            self._unread = True
            return None

    async def _reconcile(self, path: Path) -> FileEdits | None:
        stat = await asyncio.to_thread(_stat, path)
        if stat is not None and stat == self._seen:
            return None
        if stat is None:  # never written, or deleted by hand: write it again, forget nothing
            self._unread = False
            if not await self._facts():
                self._seen = None
                return None
            await self._write_file()
            return FileEdits(notes=[MISSING])
        try:
            body = await asyncio.to_thread(path.read_text, encoding="utf-8")
        except UnicodeDecodeError:
            kept = await asyncio.to_thread(_set_aside, path)
            self._unread = False
            await self._write_file()
            return FileEdits(notes=[UNREADABLE.format(kept=kept)])
        parsed = parse(body)
        saved_at = datetime.fromtimestamp(stat.mtime_ns / 1e9, UTC)
        async with self._sessions.begin() as session:
            edits, file_notes = await _apply(session, parsed, saved_at)
        self._unread = False
        self._kept, kept_notes = _keepable(parsed.kept)
        self._notes = [n for n in parsed.notes if not secret_reason(n)] + file_notes + kept_notes
        edits.notes = file_notes + kept_notes
        if self._kept and parsed.moved:
            edits.notes.append(KEPT)
        if edits.changed or edits.added or edits.removed:
            log.info(
                "memory.file_edits",
                extra={"changed": edits.changed, "added": edits.added, "removed": edits.removed},
            )
        if edits.changed or edits.added or edits.removed or edits.notes:
            await self._write_file()
            return edits
        self._seen = stat  # only the layout changed: the user's file stays as they left it
        return None

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
        path = self.file
        if path is None or self._unread:
            return
        body = render(
            await self._facts(), written_at=datetime.now(UTC), kept=self._kept, notes=self._notes
        )
        self._seen = await asyncio.to_thread(_write_whole, path, body)


async def _apply(
    session: AsyncSession, parsed: Parsed, saved_at: datetime
) -> tuple[FileEdits, list[str]]:
    """Apply a hand edit to the table, in one transaction: all of it or nothing."""
    rows = {r.id: r for r in await session.scalars(select(MemoryRow).with_for_update())}
    merge = _Merge(rows, saved_at)
    merge.match(parsed)
    edits = FileEdits()
    for old, line in merge.changes:
        reason = secret_reason(f"{line.about or ''} {line.text}")
        if reason:
            what = "changed" if old is not None else "added"
            merge.note(
                f"a line you {what} under {KINDS[line.kind]} was not saved: {reason}, and Enzo "
                "never keeps those." + (" Its memory stays as it was." if old is not None else "")
            )
            continue
        if old is not None:
            old.status = "replaced"
            edits.changed += 1
        else:
            edits.added += 1
        session.add(
            MemoryRow(
                text=line.text,
                kind=line.kind,
                about=line.about,
                said_by="user",
                replaces=old.id if old is not None else None,
                turn_id=HAND_EDIT,
            )
        )
    for row in merge.removed(parsed.written_at):
        await _forget(session, row.id)
        edits.removed += 1
    await session.flush()
    return edits, merge.notes


class _Merge:
    """Which lines of the user's copy stand for which memories, and what they change.

    Which wins when the file and the table both changed since Enzo wrote the copy the user edited
    (ADR 0023): a line the user did not change never undoes a change made in chat; a line they
    did change wins when it was saved after the chat's change of that memory; a memory forgotten
    in chat stays forgotten; a memory learned after that copy was written is never "removed".
    """

    def __init__(self, rows: dict[int, MemoryRow], saved_at: datetime) -> None:
        self.rows = rows
        self.after = {r.replaces: r for r in rows.values() if r.replaces is not None}
        self.active = [r for r in rows.values() if r.status == "active"]
        self.saved_at = saved_at
        #: Current memories some line in the file stands for.
        self.stands: set[int] = set()
        #: (the memory a line changes, or None for a new one; the line).
        self.changes: list[tuple[MemoryRow | None, Line]] = []
        self.notes: list[str] = []
        self._day = f"> {datetime.now(UTC):%-d %b %Y}:"

    def note(self, words: str) -> None:
        self.notes.append(f"{self._day} {words}")

    def current(self, memory_id: int | None) -> MemoryRow | None:
        """The current version of a memory: its later corrections followed to the newest."""
        row = self.rows.get(memory_id) if memory_id is not None else None
        while row is not None and row.status != "active":
            row = self.after.get(row.id)
        return row

    def match(self, parsed: Parsed) -> None:
        unnumbered = [line for line in parsed.lines if line.id is None]
        for line in parsed.lines:
            if line.id is not None and not self._numbered(line):
                unnumbered.append(line)  # the same memory twice: the second copy is new words
        for line in unnumbered:
            known = next((r for r in self.active if line.same_as(r.kind, r.about, r.text)), None)
            if known is None:
                self.changes.append((None, line))
            else:
                self.stands.add(known.id)  # it lost its number, or is there twice
        for number in parsed.emptied:
            head = self.current(number)
            if head is not None:
                self.stands.add(head.id)
                self.note(
                    f"the line for #{number} had no words left, so Enzo kept that memory as it "
                    "was. To make Enzo forget it, delete the whole line."
                )

    def _numbered(self, line: Line) -> bool:
        """Match a line by its number; False when that memory already has a line."""
        row = self.rows.get(line.id) if line.id is not None else None
        head = self.current(line.id)
        if row is None or head is None:
            self.note(
                f"a line numbered #{line.id} is for a memory you asked Enzo to forget, so it was "
                "left out. To keep it, add it again as a new line, without the number."
            )
            return True
        if head.id in self.stands:
            return False
        self.stands.add(head.id)
        if line.same_as(row.kind, row.about, row.text):
            return True  # unchanged, or an older copy of it: the newer version stands
        if head is not row and self.saved_at <= head.created_at:
            self.note(
                f"your change to #{line.id} was not saved: Enzo learned a newer version in chat "
                f"(#{head.id}), and the newer one counts."
            )
            return True
        self.changes.append((head, line))
        return True

    def removed(self, written_at: datetime | None) -> list[MemoryRow]:
        """Memories whose line the user deleted: only those their copy had."""
        missing = [r for r in self.active if r.id not in self.stands]
        if missing and written_at is None:
            self.note(
                "Memory.md had lost the line near its top that tells Enzo which copy it is, so "
                "nothing was removed from memory. To make Enzo forget something, delete just its "
                "line."
            )
            return []
        return [r for r in missing if written_at is not None and r.created_at <= written_at]


async def _forget(session: AsyncSession, memory_id: int) -> int:
    """Delete a memory and every version before it; how many rows went."""
    chain = [memory_id]
    row = await session.get(MemoryRow, memory_id)
    while row is not None and row.replaces is not None:
        chain.append(row.replaces)
        row = await session.get(MemoryRow, row.replaces)
    await session.execute(delete(MemoryRow).where(MemoryRow.id.in_(chain)))
    return len(chain)


def _keepable(lines: Sequence[str]) -> tuple[list[str], list[str]]:
    """The user's unread lines to keep in the file, and a note for each one that cannot be."""
    kept, notes = [], []
    day = f"> {datetime.now(UTC):%-d %b %Y}:"
    for line in lines:
        reason = secret_reason(line)
        if reason:
            notes.append(
                f"{day} a line Enzo could not read as a memory was taken out of the file: "
                f"{reason}, and Enzo never keeps those."
            )
        else:
            kept.append(line)
    return kept, notes


def _stat(path: Path) -> _Seen | None:
    try:
        found = path.stat()
    except FileNotFoundError:
        return None
    return _Seen(found.st_mtime_ns, found.st_size, found.st_ino)


def _set_aside(path: Path) -> str:
    """Keep a copy Enzo cannot read next to it, so writing the file again loses nothing."""
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H%M%S")
    target = path.with_name(f"Memory (unreadable copy {stamp}).md")
    path.replace(target)
    return target.name


def _write_whole(path: Path, body: str) -> _Seen:
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
    seen = _stat(path)
    if seen is None:
        msg = f"{path} went missing as it was written"
        raise FileNotFoundError(msg)
    return seen


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
        edited_by_hand=row.turn_id == HAND_EDIT,
    )
