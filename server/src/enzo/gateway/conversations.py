"""Conversation endpoints: create, read, choose a brain, run turns over SSE, decide approvals."""

import uuid
from collections.abc import AsyncIterator, Awaitable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, params
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel, ConfigDict, Field

from enzo.agent.loop import ApprovalNeeded, ToolFinished, ToolStarted, TurnDone
from enzo.agent.service import (
    ConversationNotFoundError,
    ConversationService,
    ServiceEvent,
    TurnFailed,
    TurnInProgressError,
    TurnMeta,
)
from enzo.brain.config import TIERS
from enzo.brain.models import TextDelta
from enzo.store.repository import ApprovalError, ConversationInfo, ConversationKind


def get_service(request: Request) -> ConversationService:
    service: ConversationService = request.app.state.service
    return service


ServiceDep = Annotated[ConversationService, Depends(get_service)]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NewConversation(_In):
    kind: ConversationKind = "main"
    title: str | None = Field(default=None, max_length=200)


class ConversationOut(BaseModel):
    id: uuid.UUID
    kind: str
    title: str | None
    brain: str | None


class BrainChoice(_In):
    brain: str | None


class TurnIn(_In):
    message: str = Field(min_length=1, max_length=100_000)
    timezone: str | None = Field(default=None, max_length=64)
    device: str | None = Field(default=None, max_length=64)


class Decision(_In):
    approved: bool


def conversation_routes(auth: params.Depends) -> APIRouter:
    """The ``/conversations`` endpoints, behind the gateway's auth dependency."""
    router = APIRouter(prefix="/conversations", dependencies=[auth])

    @router.post("", status_code=201)
    async def create_conversation(body: NewConversation, s: ServiceDep) -> ConversationOut:
        conversation_id = await s.store.create_conversation(body.kind, body.title)
        return ConversationOut(id=conversation_id, kind=body.kind, title=body.title, brain=None)

    @router.get("/{conversation_id}")
    async def get_conversation(conversation_id: uuid.UUID, s: ServiceDep) -> ConversationOut:
        info = await _require(s, conversation_id)
        return ConversationOut(id=info.id, kind=info.kind, title=info.title, brain=info.brain)

    @router.put("/{conversation_id}/brain", status_code=204)
    async def choose_brain(conversation_id: uuid.UUID, body: BrainChoice, s: ServiceDep) -> None:
        if body.brain is not None and body.brain not in (*s.router.config.brains, *TIERS):
            raise HTTPException(422, f"unknown model or tier {body.brain!r}")
        await _require(s, conversation_id)
        await s.store.set_brain(conversation_id, body.brain)

    @router.get("/{conversation_id}/messages")
    async def messages(conversation_id: uuid.UUID, s: ServiceDep) -> list[dict[str, Any]]:
        await _require(s, conversation_id)
        return [m.model_dump(mode="json") for m in await s.store.history(conversation_id)]

    async def start_turn(
        conversation_id: uuid.UUID, body: TurnIn, s: ServiceDep
    ) -> AsyncIterator[ServiceEvent]:
        meta = TurnMeta(timezone=body.timezone, device=body.device)
        return await _guard(s.send(conversation_id, body.message, meta))

    async def start_resume(
        conversation_id: uuid.UUID, s: ServiceDep
    ) -> AsyncIterator[ServiceEvent]:
        return await _guard(s.resume(conversation_id))

    @router.post(
        "/{conversation_id}/turns",
        response_class=EventSourceResponse,
    )
    async def turn(
        events: Annotated[AsyncIterator[ServiceEvent], Depends(start_turn)],
    ) -> AsyncIterator[ServerSentEvent]:
        async for event in events:
            yield _sse(event)

    @router.post(
        "/{conversation_id}/resume",
        response_class=EventSourceResponse,
    )
    async def resume(
        events: Annotated[AsyncIterator[ServiceEvent], Depends(start_resume)],
    ) -> AsyncIterator[ServerSentEvent]:
        async for event in events:
            yield _sse(event)

    @router.post(
        "/{conversation_id}/approvals/{approval_id}",
        status_code=204,
    )
    async def decide(
        conversation_id: uuid.UUID, approval_id: str, body: Decision, s: ServiceDep
    ) -> None:
        await _require(s, conversation_id)
        try:
            await s.store.decide(conversation_id, approval_id, approved=body.approved)
        except ApprovalError as exc:
            raise HTTPException(409, str(exc)) from exc

    return router


async def _require(s: ConversationService, conversation_id: uuid.UUID) -> ConversationInfo:
    try:
        return await s.require(conversation_id)
    except ConversationNotFoundError as exc:
        raise HTTPException(404, "conversation not found") from exc


async def _guard(
    starting: Awaitable[AsyncIterator[ServiceEvent]],
) -> AsyncIterator[ServiceEvent]:
    """Turn start-up failures into HTTP errors before the stream begins."""
    try:
        stream: AsyncIterator[ServiceEvent] = await starting
    except ConversationNotFoundError as exc:
        raise HTTPException(404, "conversation not found") from exc
    except TurnInProgressError as exc:
        raise HTTPException(409, "a turn is already running in this conversation") from exc
    return stream


def _sse(event: ServiceEvent) -> ServerSentEvent:
    """Client-facing events. Plain data only: no provider payloads leave the gateway."""
    match event:
        case TextDelta(text=text):
            return ServerSentEvent(event="text", data={"text": text})
        case ToolStarted(call=call):
            return ServerSentEvent(event="tool_started", data={"id": call.id, "tool": call.name})
        case ToolFinished(outcome=o):
            data = {
                "id": o.call.id,
                "tool": o.call.name,
                "status": o.status,
                "latency_ms": o.latency_ms,
            }
            return ServerSentEvent(event="tool_finished", data=data)
        case ApprovalNeeded(request=r):
            data = {
                "id": r.id,
                "tool": r.tool,
                "arguments": r.arguments,
                "summary": r.summary,
                "detail": r.detail,
            }
            return ServerSentEvent(event="approval_needed", data=data)
        case TurnDone(outcome=o):
            # Calls the user's plan paid for have no per-token cost; the rest are summed.
            costs = [c.cost_usd for c in o.brain_calls if c.billing != "plan"]
            # No call made: nothing was spent (#25). Any unpriced call: unknown, never 0 (§1.3.5).
            cost = None if any(c is None for c in costs) else sum(c or 0 for c in costs)
            answered = next((c for c in reversed(o.brain_calls) if c.outcome == "ok"), None)
            data = {
                "stop": o.stop,
                "text": o.text,
                "steps": o.steps,
                "cost_usd": cost,
                # Which brain replied, shown where the reply appears (spec §1.3.6).
                "brain": answered.brain if answered else None,
                "model": answered.model if answered else None,
                "failure": o.failure,
                # A ChatGPT plan paid for (part of) this reply: apps say so instead of a price.
                "plan": any(c.billing == "plan" for c in o.brain_calls),
            }
            return ServerSentEvent(event="done", data=data)
        case TurnFailed(message=message):
            return ServerSentEvent(event="error", data={"message": message})
