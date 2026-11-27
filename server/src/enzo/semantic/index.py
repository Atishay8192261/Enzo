"""Tier 3 memory: search by meaning over what Enzo knows (spec §6.3, ADR 0030).

1. **Chunk** memories, goals, past chats (a turn at a time) and stored long texts (``chunking``).
2. **Embed** each chunk with the swappable embedding model, in the background: new ones as they
   are written, older ones (a backfill) when the model first becomes ready, and all of them again
   when the model changes (each vector keeps the id of the model that made it).
3. **Embed the question** the same way.
4. **Retrieve** by meaning (exact nearest neighbours, cosine) and by words (Postgres full-text
   search), fused by reciprocal rank. Only what still holds is searched: a memory that was
   corrected or forgotten, a chat in the trash and another person's chat never come back.
5. **Verify** each hit against its source before use: the memory is still current and says the
   same, the chat's messages are still there and say the same, the stored text still holds those
   words at that place. A hit that fails is dropped. The brain gets the source's own words, their
   date and where they came from ("Similarity is not truth").

Vectors live only in the local database. The default model runs on this computer, so no text
leaves it to be embedded.
"""

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections import defaultdict
from collections.abc import Collection, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import case, delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from enzo.brain.models import Message
from enzo.semantic.chunking import (
    STORED_PIECES,
    Piece,
    Saved,
    goal_text,
    memory_text,
    split,
    turns,
)
from enzo.semantic.embedder import Embedder, EmbedderNotReadyError, State
from enzo.semantic.model import Tuning
from enzo.store.schema import (
    ChunkRow,
    Conversation,
    GoalRow,
    GoalStepRow,
    MemoryRow,
    MessageRow,
)

log = logging.getLogger("enzo.semantic")

type Source = Literal["memory", "goal", "turn", "stored"]
#: recall: what rides with a message unasked (precise: higher bars, close to the best hit).
#: search: what the brain looks for itself (it asked, and it checks what it gets).
type Mode = Literal["recall", "search"]
SOURCES: tuple[Source, ...] = ("memory", "goal", "turn", "stored")

#: Reciprocal rank fusion's constant (Cormack, Clarke and Büttcher 2009): 60, as in the paper.
RRF_K = 60
#: Candidates each way before fusing.
CANDIDATES = 24
#: A piece counts as a word match when it holds at least half of the question's words (after
#: stemming and dropping common words). Any single shared word was too loose: "where do I like
#: to sit on a plane" matched a note about the theatre by "like" and "sit".
COVERAGE = 0.5
#: Chunks embedded per pass before the loop looks for new ones again.
PASS = 256
#: Between passes when nothing wakes the indexer; and before fetching a failed model again.
PERIOD_S = 60.0
RETRY_S = 15 * 60.0
#: Memories and goals written moments ago are embedded before a search, up to this many.
CATCH_UP = 64

_PRIORITY = case(
    (ChunkRow.source == "memory", 0),
    (ChunkRow.source == "goal", 1),
    (ChunkRow.source == "turn", 2),
    else_=3,
)


@dataclass(frozen=True, slots=True)
class Hit:
    """A piece of what Enzo knows, checked against its source: the source's own words now."""

    source: Source
    text: str
    #: When it was said, written or learned.
    happened_at: datetime
    #: Cosine similarity to the question; None when only its words matched.
    similarity: float | None
    memory_id: int | None = None
    goal_id: int | None = None
    conversation_id: uuid.UUID | None = None
    #: The chat's title, when it has one.
    chat: str | None = None
    seq_from: int | None = None
    stored_id: str | None = None
    #: Where a stored text came from ("read_page", "a long message the user pasted").
    stored_from: str | None = None
    start: int = 0
    #: Outside content (a page, a file): shown fenced, never recalled unasked.
    untrusted: bool = False


@dataclass(frozen=True, slots=True)
class IndexStatus:
    state: State
    model: str
    chunks: int
    embedded: int
    waiting: int
    problem: str | None


@dataclass(slots=True)
class Pass:
    """What one indexing pass did (logged with counts only, never words)."""

    chunked: int = 0
    embedded: int = 0
    removed: int = 0


class SemanticIndex:
    def __init__(
        self, engine: AsyncEngine, embedder: Embedder, *, stored: Path | None = None
    ) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)
        self.embedder = embedder
        #: Where long texts are kept (``OutputStore.root``): one folder per chat.
        self._stored = stored
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        #: One writer per kind of source at a time (inserts are idempotent anyway).
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._next_try = 0.0

    @property
    def by_meaning(self) -> bool:
        """The model is loaded: searches use meaning as well as words."""
        return self.embedder.ready

    # ---- lifecycle -------------------------------------------------------------------------

    def start(self) -> None:
        """Keep the index current in the background: fetch and load the model, embed what is
        not yet (a backfill the first time), then whatever is written later."""
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._loop())

    def wake(self) -> None:
        """Something was written: index it soon."""
        self._wake.set()

    async def close(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _loop(self) -> None:
        while True:
            self._wake.clear()
            try:
                await self._prepare()
                await self.sync()
            except asyncio.CancelledError:
                raise
            except Exception:  # never let indexing take the gateway down; try again later
                log.exception("semantic.sync_failed")
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(PERIOD_S):
                    await self._wake.wait()

    async def _prepare(self) -> None:
        if self.embedder.ready or self.embedder.state in ("off", "unavailable"):
            return
        if time.monotonic() < self._next_try:
            return
        if not await self.embedder.prepare():
            self._next_try = time.monotonic() + RETRY_S

    # ---- indexing --------------------------------------------------------------------------

    async def sync(self, *, embed: int | None = None) -> Pass:
        """One pass: chunk what is new or changed, drop what no longer holds, then embed what
        is waiting (all of it, or up to ``embed`` chunks)."""
        began = time.perf_counter()
        done = Pass()
        await self._chunk(done, SOURCES)
        if self.embedder.ready:
            while True:
                count = await self._embed_waiting(PASS, SOURCES)
                done.embedded += count
                if count < PASS or (embed is not None and done.embedded >= embed):
                    break
        if done.chunked or done.embedded or done.removed:
            log.info(
                "semantic.indexed",
                extra={
                    "chunked": done.chunked,
                    "embedded": done.embedded,
                    "removed": done.removed,
                    "ms": round((time.perf_counter() - began) * 1000),
                },
            )
        return done

    async def _chunk(self, done: Pass, sources: Collection[Source]) -> None:
        if "memory" in sources:
            async with self._locks["memory"], self._sessions.begin() as session:
                await _chunk_memories(session, done)
        if "goal" in sources:
            async with self._locks["goal"], self._sessions.begin() as session:
                await _chunk_goals(session, done)
        if "turn" in sources:
            async with self._locks["turn"]:
                await self._chunk_turns(done)
        if "stored" in sources and self._stored is not None:
            async with self._locks["stored"]:
                await self._chunk_stored(done)

    async def _chunk_turns(self, done: Pass) -> None:
        async with self._sessions() as session:
            grown = (
                await session.execute(
                    text(
                        """
                        SELECT m.conversation_id,
                               (SELECT max(c.seq_from) FROM chunks AS c
                                WHERE c.source = 'turn' AND c.conversation_id = m.conversation_id)
                        FROM messages AS m
                        JOIN conversations AS v ON v.id = m.conversation_id
                        WHERE v.kind <> 'external'
                        GROUP BY m.conversation_id
                        HAVING max(m.seq) > coalesce(
                            (SELECT max(c.seq_to) FROM chunks AS c
                             WHERE c.source = 'turn' AND c.conversation_id = m.conversation_id),
                            -1)
                        """
                    )
                )
            ).all()
        for chat, last_turn in grown:
            async with self._sessions.begin() as session:
                rows = await session.execute(
                    select(
                        MessageRow.seq, MessageRow.body, MessageRow.created_at, MessageRow.origin
                    )
                    .where(MessageRow.conversation_id == chat, MessageRow.seq >= (last_turn or 0))
                    .order_by(MessageRow.seq)
                )
                saved = [
                    Saved(seq, Message.model_validate(body), at, origin == "schedule")
                    for seq, body, at, origin in rows
                ]
                for turn in turns(saved):
                    done.chunked += await _replace(
                        session,
                        key=f"turn:{chat}:{turn.seq_from}",
                        source="turn",
                        pieces=turn.pieces(),
                        happened_at=turn.at,
                        conversation_id=chat,
                        seq_from=turn.seq_from,
                        seq_to=turn.seq_to,
                    )

    async def _chunk_stored(self, done: Pass) -> None:
        root = self._stored
        if root is None:
            return
        async with self._sessions() as session:
            known = set(
                await session.scalars(
                    select(ChunkRow.key).where(ChunkRow.source == "stored").distinct()
                )
            )
            chats = {
                str(i)
                for i in await session.scalars(
                    select(Conversation.id).where(Conversation.kind != "external")
                )
            }
        found = await asyncio.to_thread(_stored_texts, root, chats, known)
        for chat, stored, meta, body, mtime in found:
            async with self._sessions.begin() as session:
                done.chunked += await _replace(
                    session,
                    key=f"stored:{chat}:{stored}",
                    source="stored",
                    pieces=split(body)[:STORED_PIECES],
                    happened_at=mtime,
                    conversation_id=uuid.UUID(chat),
                    stored_id=stored,
                    untrusted=bool(meta.get("untrusted", True)),
                )

    async def _embed_waiting(self, most: int, sources: Collection[Source]) -> int:
        """Embed up to ``most`` chunks that have no vector from the current model."""
        model = self.embedder.id
        async with self._sessions() as session:
            waiting = (
                await session.execute(
                    select(ChunkRow.id, ChunkRow.text, ChunkRow.digest)
                    .where(
                        or_(ChunkRow.model.is_(None), ChunkRow.model != model),
                        ChunkRow.source.in_(list(sources)),
                    )
                    .order_by(_PRIORITY, ChunkRow.happened_at.desc(), ChunkRow.id)
                    .limit(most)
                )
            ).all()
        if not waiting:
            return 0
        try:
            vectors = await self.embedder.embed([w.text for w in waiting], use="document")
        except EmbedderNotReadyError:
            return 0
        async with self._sessions.begin() as session:
            for row, vector in zip(waiting, vectors, strict=True):
                # Only if the chunk still says what was embedded (it may have changed meanwhile).
                await session.execute(
                    update(ChunkRow)
                    .where(ChunkRow.id == row.id, ChunkRow.digest == row.digest)
                    .values(embedding=vector, model=model)
                )
        return len(waiting)

    async def catch_up(self, sources: Collection[Source]) -> None:
        """Memories and goals written moments ago: chunked and embedded before a search."""
        quick = [s for s in sources if s in ("memory", "goal")]
        if not quick:
            return
        await self._chunk(Pass(), quick)
        if self.embedder.ready:
            await self._embed_waiting(CATCH_UP, quick)

    # ---- search ----------------------------------------------------------------------------

    async def find(
        self,
        query: str,
        *,
        sources: Collection[Source],
        limit: int,
        mode: Mode,
        chat: uuid.UUID | None = None,
        other_chats_only: bool = False,
        by_meaning_only: bool = False,
    ) -> list[Hit]:
        """The pieces ``query`` is about, best first, each checked against its source.

        ``chat``: the chat the question is asked in (``other_chats_only`` leaves it out).
        ``by_meaning_only``: no hits found by words alone (recall of past chats, where long
        texts share common words with anything).
        """
        query = " ".join(query.split())[:2_000]
        if not query:
            return []
        await self.catch_up(sources)
        tuning = self.embedder.tuning
        near: list[tuple[int, float]] = []
        if self.embedder.ready:
            try:
                (vector,) = await self.embedder.embed([query], use="query")
            except EmbedderNotReadyError:
                vector = None
            if vector is not None:
                near = await self._nearest(
                    vector, sources, chat if other_chats_only else None, untrusted=mode == "search"
                )
                near = _keep(near, mode, sources, tuning)
        words = (
            []
            if by_meaning_only
            else await self._words(
                query, sources, chat if other_chats_only else None, untrusted=mode == "search"
            )
        )
        similarity = dict(near)
        hits: list[Hit] = []
        for chunk_id in fuse(near, words):
            hit = await self._verified(chunk_id, similarity.get(chunk_id))
            if hit is not None:
                hits.append(hit)
            if len(hits) >= limit:
                break
        if hits:
            log.info(
                "semantic.found",
                extra={
                    "mode": mode,
                    "sources": sorted(sources),
                    "hits": len(hits),
                    "by_meaning": sum(h.similarity is not None for h in hits),
                },
            )
        return hits

    async def _nearest(
        self,
        vector: Sequence[float],
        sources: Collection[Source],
        leave_out: uuid.UUID | None,
        *,
        untrusted: bool,
    ) -> list[tuple[int, float]]:
        where, params = _filters(sources, leave_out, untrusted=untrusted)
        params |= {"q": _literal(vector), "model": self.embedder.id, "k": CANDIDATES}
        async with self._sessions() as session:
            rows = await session.execute(
                text(
                    f"""
                    SELECT c.id, 1 - (c.embedding <=> CAST(:q AS vector)) AS similarity
                    {where} AND c.model = :model AND c.embedding IS NOT NULL
                    ORDER BY c.embedding <=> CAST(:q AS vector), c.id
                    LIMIT :k
                    """
                ),
                params,
            )
            return [(int(i), float(s)) for i, s in rows]

    async def _words(
        self,
        query: str,
        sources: Collection[Source],
        leave_out: uuid.UUID | None,
        *,
        untrusted: bool,
    ) -> list[int]:
        where, params = _filters(sources, leave_out, untrusted=untrusted)
        params |= {"query": query, "k": CANDIDATES, "coverage": COVERAGE}
        async with self._sessions() as session:
            rows = await session.execute(
                text(
                    f"""
                    WITH q AS (
                        SELECT lex, to_tsquery('simple', array_to_string(ARRAY(
                                   SELECT quote_literal(l) FROM unnest(lex) AS l), ' | ')) AS tsq
                        FROM (SELECT ARRAY(SELECT DISTINCT unnest(tsvector_to_array(
                                  to_tsvector('english', :query)))) AS lex) AS words
                        WHERE cardinality(lex) > 0
                    )
                    SELECT id FROM (
                        SELECT c.id, c.happened_at,
                               cardinality(ARRAY(
                                   SELECT unnest(tsvector_to_array(c.search))
                                   INTERSECT SELECT unnest(q.lex)))::float
                                   / cardinality(q.lex) AS coverage,
                               ts_rank_cd(c.search, q.tsq) AS rank
                        {where.replace("FROM chunks AS c", "FROM q, chunks AS c")}
                          AND c.search @@ q.tsq
                    ) AS found
                    WHERE coverage >= :coverage
                    ORDER BY coverage DESC, rank DESC, happened_at DESC, id
                    LIMIT :k
                    """  # noqa: S608 - only Enzo's own fixed fragments are formatted in
                ),
                params,
            )
            return [int(i) for (i,) in rows]

    async def _verified(self, chunk_id: int, similarity: float | None) -> Hit | None:
        """§6.3 step 5: the chunk, read again from its source; None when the source no longer
        says it (corrected, forgotten, deleted, changed)."""
        async with self._sessions() as session:
            found = (
                await session.execute(
                    select(ChunkRow, Conversation.title, Conversation.archived_at)
                    .outerjoin(Conversation, Conversation.id == ChunkRow.conversation_id)
                    .where(ChunkRow.id == chunk_id)
                )
            ).first()
            if found is None:
                return None
            chunk, title, archived = found
            if archived is not None:
                return None
            hit = Hit(
                source=chunk.source,  # type: ignore[arg-type]  # the table's check
                text=chunk.text,
                happened_at=chunk.happened_at,
                similarity=similarity,
                memory_id=chunk.memory_id,
                goal_id=chunk.goal_id,
                conversation_id=chunk.conversation_id,
                chat=title,
                seq_from=chunk.seq_from,
                stored_id=chunk.stored_id,
                start=chunk.start,
                untrusted=chunk.untrusted,
            )
            if chunk.source == "memory":
                return await _check_memory(session, chunk, hit)
            if chunk.source == "goal":
                return await _check_goal(session, chunk, hit)
            if chunk.source == "turn":
                return await _check_turn(session, chunk, hit)
        return await asyncio.to_thread(_check_stored, self._stored, chunk, hit)

    async def status(self) -> IndexStatus:
        model = self.embedder.id
        async with self._sessions() as session:
            total, embedded = (
                await session.execute(
                    select(
                        func.count(ChunkRow.id),
                        func.count(ChunkRow.id).filter(ChunkRow.model == model),
                    )
                )
            ).one()
        return IndexStatus(
            state=self.embedder.state,
            model=model,
            chunks=total,
            embedded=embedded,
            waiting=total - embedded,
            problem=getattr(self.embedder, "problem", None),
        )


def status_line(status: IndexStatus) -> str:
    """The state of search by meaning in plain words, for /memory."""
    if status.state == "ready":
        waiting = f", {status.waiting:,} still to go" if status.waiting else ""
        return (
            "Enzo also finds memories and past chats by meaning, with a model on this computer "
            f"(nothing is sent anywhere): {status.embedded:,} pieces indexed{waiting}."
        )
    if status.state == "preparing":
        return (
            "Search by meaning is getting ready (a one-time download, then indexing). Until "
            "then Enzo finds memories and past chats by their words."
        )
    if status.state == "failed":
        return (
            f"Search by meaning could not start ({status.problem}). Enzo finds memories and "
            "past chats by their words, and tries again later."
        )
    if status.state == "unavailable":
        return (
            "Search by meaning does not run on this computer, so Enzo finds memories and past "
            "chats by their words."
        )
    return "Search by meaning is off (ENZO_EMBEDDINGS=off): Enzo finds things by their words."


def fuse(near: Sequence[tuple[int, float]], words: Sequence[int], k: int = RRF_K) -> list[int]:
    """Reciprocal rank fusion of the two lists: a chunk found both ways comes first; a tie goes
    to the closer meaning."""
    score: dict[int, float] = defaultdict(float)
    for rank, (chunk_id, _similarity) in enumerate(near):
        score[chunk_id] += 1 / (k + rank + 1)
    for rank, chunk_id in enumerate(words):
        score[chunk_id] += 1 / (k + rank + 1)
    similarity = dict(near)
    return sorted(score, key=lambda c: (-score[c], -similarity.get(c, -1.0), c))


def _keep(
    near: list[tuple[int, float]], mode: Mode, sources: Collection[Source], tuning: Tuning
) -> list[tuple[int, float]]:
    """Hits close enough to count: above the model's floor and, for recall, close to the best."""
    if mode == "search":
        floor = tuning.search_floor
    elif set(sources) <= {"memory"}:
        floor = tuning.memory_floor
    else:
        floor = tuning.chat_floor
    kept = [(c, s) for c, s in near if s >= floor]
    if mode == "recall" and kept:
        best = max(s for _c, s in kept)
        kept = [(c, s) for c, s in kept if s >= best - tuning.margin]
    return kept


def _filters(
    sources: Collection[Source], leave_out: uuid.UUID | None, *, untrusted: bool
) -> tuple[str, dict[str, Any]]:
    """What may be searched: what still holds, outside another person's chat or the trash."""
    clause = """
        FROM chunks AS c
        LEFT JOIN memories AS m ON m.id = c.memory_id
        LEFT JOIN conversations AS v ON v.id = c.conversation_id
        WHERE c.source = ANY(CAST(:sources AS text[]))
          AND (c.memory_id IS NULL OR m.status = 'active')
          AND (c.conversation_id IS NULL OR (v.archived_at IS NULL AND v.kind <> 'external'))
    """
    params: dict[str, Any] = {"sources": sorted(sources)}
    if leave_out is not None:
        clause += " AND c.conversation_id IS DISTINCT FROM CAST(:leave_out AS uuid)"
        params["leave_out"] = str(leave_out)
    if not untrusted:
        clause += " AND NOT c.untrusted"
    return clause, params


def _literal(vector: Sequence[float]) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in vector) + "]"


async def _replace(  # noqa: PLR0913 - keyword-only parts of a chunk's source
    session: AsyncSession,
    *,
    key: str,
    source: Source,
    pieces: Sequence[Piece],
    happened_at: datetime,
    memory_id: int | None = None,
    goal_id: int | None = None,
    conversation_id: uuid.UUID | None = None,
    seq_from: int | None = None,
    seq_to: int | None = None,
    stored_id: str | None = None,
    untrusted: bool = False,
) -> int:
    """Make a source's chunks ``pieces``: unchanged ones keep their vectors, changed ones wait
    for a new one, extra ones go. Returns how many were added or changed."""
    existing = {
        part: (chunk_id, digest)
        for chunk_id, part, digest in await session.execute(
            select(ChunkRow.id, ChunkRow.part, ChunkRow.digest).where(ChunkRow.key == key)
        )
    }
    changed = 0
    for part, piece in enumerate(pieces):
        now = existing.get(part)
        if now is not None and now[1] == piece.digest:
            if seq_to is not None:  # a turn that grew without new words still covers more
                await session.execute(
                    update(ChunkRow).where(ChunkRow.id == now[0]).values(seq_to=seq_to)
                )
            continue
        values: dict[str, Any] = {
            "source": source,
            "key": key,
            "part": part,
            "memory_id": memory_id,
            "goal_id": goal_id,
            "conversation_id": conversation_id,
            "seq_from": seq_from,
            "seq_to": seq_to,
            "stored_id": stored_id,
            "start": piece.start,
            "text": piece.text,
            "digest": piece.digest,
            "untrusted": untrusted,
            "happened_at": happened_at,
            "model": None,
            "embedding": None,
        }
        await session.execute(
            insert(ChunkRow)
            .values(values)
            .on_conflict_do_update(
                index_elements=[ChunkRow.key, ChunkRow.part],
                set_={k: v for k, v in values.items() if k not in ("key", "part")},
            )
        )
        changed += 1
    extra = [chunk_id for part, (chunk_id, _d) in existing.items() if part >= len(pieces)]
    if extra:
        await session.execute(delete(ChunkRow).where(ChunkRow.id.in_(extra)))
    return changed


async def _chunk_memories(session: AsyncSession, done: Pass) -> None:
    """A chunk for each current memory without one; corrected memories' chunks go (forgotten
    ones went with their rows)."""
    missing = await session.execute(
        select(MemoryRow.id, MemoryRow.text, MemoryRow.about, MemoryRow.created_at)
        .outerjoin(ChunkRow, ChunkRow.memory_id == MemoryRow.id)
        .where(MemoryRow.status == "active", ChunkRow.id.is_(None))
    )
    for memory_id, words, about, learned in missing:
        whole = memory_text(words, about)
        done.chunked += await _replace(
            session,
            key=f"memory:{memory_id}",
            source="memory",
            pieces=[Piece(0, whole)],
            happened_at=learned,
            memory_id=memory_id,
        )
    gone = await session.execute(
        delete(ChunkRow).where(
            ChunkRow.source == "memory",
            ChunkRow.memory_id.in_(select(MemoryRow.id).where(MemoryRow.status != "active")),
        )
    )
    done.removed += gone.rowcount or 0  # type: ignore[attr-defined]


async def _goal_words(session: AsyncSession) -> dict[int, tuple[str, datetime]]:
    steps: dict[int, list[tuple[str, bool]]] = defaultdict(list)
    for goal_id, words, done_at in await session.execute(
        select(GoalStepRow.goal_id, GoalStepRow.text, GoalStepRow.done_at).order_by(
            GoalStepRow.goal_id, GoalStepRow.position
        )
    ):
        steps[goal_id].append((words, done_at is not None))
    return {
        g.id: (goal_text(g.title, g.status, g.due_on, steps[g.id]), g.created_at)
        for g in await session.scalars(select(GoalRow))
    }


async def _chunk_goals(session: AsyncSession, done: Pass) -> None:
    for goal_id, (words, created) in (await _goal_words(session)).items():
        done.chunked += await _replace(
            session,
            key=f"goal:{goal_id}",
            source="goal",
            pieces=split(words),
            happened_at=created,
            goal_id=goal_id,
        )


async def _check_memory(session: AsyncSession, chunk: ChunkRow, hit: Hit) -> Hit | None:
    row = await session.get(MemoryRow, chunk.memory_id)
    if row is None or row.status != "active":
        return None
    if memory_text(row.text, row.about) != chunk.text:
        return None
    return hit


async def _check_goal(session: AsyncSession, chunk: ChunkRow, hit: Hit) -> Hit | None:
    """A goal's words now (its status may have changed since it was indexed)."""
    row = await session.get(GoalRow, chunk.goal_id)
    if row is None:
        return None
    steps = [
        (words, done_at is not None)
        for words, done_at in await session.execute(
            select(GoalStepRow.text, GoalStepRow.done_at)
            .where(GoalStepRow.goal_id == row.id)
            .order_by(GoalStepRow.position)
        )
    ]
    now = goal_text(row.title, row.status, row.due_on, steps)
    pieces = split(now)
    if chunk.part >= len(pieces):
        return None
    return replace(hit, text=pieces[chunk.part].text)


async def _check_turn(session: AsyncSession, chunk: ChunkRow, hit: Hit) -> Hit | None:
    """The chat's messages are still there and still give these words."""
    if chunk.seq_from is None or chunk.seq_to is None:
        return None
    rows = await session.execute(
        select(MessageRow.seq, MessageRow.body, MessageRow.created_at, MessageRow.origin)
        .where(
            MessageRow.conversation_id == chunk.conversation_id,
            MessageRow.seq >= chunk.seq_from,
            MessageRow.seq <= chunk.seq_to,
        )
        .order_by(MessageRow.seq)
    )
    saved = [
        Saved(seq, Message.model_validate(body), at, origin == "schedule")
        for seq, body, at, origin in rows
    ]
    again = turns(saved)
    if not again or again[0].seq_from != chunk.seq_from:
        return None
    pieces = again[0].pieces()
    if chunk.part >= len(pieces) or pieces[chunk.part].text != chunk.text:
        return None
    return hit


def _check_stored(root: Path | None, chunk: ChunkRow, hit: Hit) -> Hit | None:
    """The stored text still holds these words at this place."""
    if root is None or chunk.conversation_id is None or chunk.stored_id is None:
        return None
    folder = root / str(chunk.conversation_id)
    try:
        body = (folder / f"{chunk.stored_id}.txt").read_text(encoding="utf-8")
        meta = json.loads((folder / f"{chunk.stored_id}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if body[chunk.start : chunk.start + len(chunk.text)] != chunk.text:
        return None
    return replace(hit, stored_from=str(meta.get("source", "a stored text")))


def _stored_texts(
    root: Path, chats: set[str], known: set[str]
) -> list[tuple[str, str, dict[str, Any], str, datetime]]:
    """Stored texts not indexed yet: (chat, id, meta, text, when it was kept)."""
    found: list[tuple[str, str, dict[str, Any], str, datetime]] = []
    if not root.is_dir():
        return found
    for folder in sorted(root.iterdir()):
        if folder.name not in chats or not folder.is_dir():
            continue  # "unscoped" work, another person's chat, a chat that was deleted
        for meta_path in sorted(folder.glob("s_*.json")):
            stored = meta_path.stem
            if f"stored:{folder.name}:{stored}" in known:
                continue
            body_path = folder / f"{stored}.txt"
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                body = body_path.read_text(encoding="utf-8")
                kept = datetime.fromtimestamp(body_path.stat().st_mtime, UTC)
            except (OSError, ValueError):
                continue
            found.append((folder.name, stored, meta, body, kept))
    return found
