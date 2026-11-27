"""``/memory``: what Enzo remembers, with where each fact came from, and forgetting one (§15.2)."""

import uuid
from datetime import date, datetime

from fastapi import APIRouter, HTTPException, params, status
from pydantic import BaseModel

from enzo.gateway.conversations import ServiceDep
from enzo.memory import Fact, Memory, MemoryRefusedError, provenance
from enzo.paths import shown
from enzo.semantic import status_line


class MemoryOut(BaseModel):
    id: int
    text: str
    kind: str
    about: str | None
    said_by: str
    happened_on: date | None
    learned_at: datetime
    #: The chat it was learned in (its title), when that chat still exists.
    chat: str | None
    conversation_id: uuid.UUID | None
    #: All of it in words: "you said, 20 Nov 2026, in “Trip to NYC”".
    source: str


class MemoryListOut(BaseModel):
    memories: list[MemoryOut]
    #: Where the readable copy is, as the user types it: ~/Enzo/Memory.md.
    file: str | None
    #: Whether Enzo also finds things by meaning, in plain words (ADR 0030); None without it.
    search: str | None = None


def memory_routes(auth: params.Depends) -> APIRouter:
    router = APIRouter(prefix="/memory", dependencies=[auth])

    @router.get("")
    async def list_memory(s: ServiceDep) -> MemoryListOut:
        memory = _memory(s.memory)
        return MemoryListOut(
            memories=[_out(f) for f in await memory.all()],
            file=shown(memory.file) if memory.file else None,
            search=status_line(await s.index.status()) if s.index else None,
        )

    @router.delete("/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def forget(memory_id: int, s: ServiceDep) -> None:
        try:
            await _memory(s.memory).forget(memory_id)
        except MemoryRefusedError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None

    return router


def _memory(memory: Memory | None) -> Memory:
    if memory is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "memory is not set up")
    return memory


def _out(f: Fact) -> MemoryOut:
    return MemoryOut(
        id=f.id,
        text=f.text,
        kind=f.kind,
        about=f.about,
        said_by=f.said_by,
        happened_on=f.happened_on,
        learned_at=f.learned_at,
        chat=f.chat,
        conversation_id=f.conversation_id,
        source=provenance(f),
    )
