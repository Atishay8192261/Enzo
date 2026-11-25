"""Conversation endpoints: create, read, choose a brain, run turns over SSE, decide approvals."""

import uuid
from collections.abc import AsyncIterator, Awaitable
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, params
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
from enzo.brain.models import Message, TextDelta, ToolCall
from enzo.gateway.usage import ConversationUsageOut, per_turn, usage_of
from enzo.store.repository import ApprovalError, ConversationInfo, ConversationKind
from enzo.tools.registry import ToolRegistry


def get_service(request: Request) -> ConversationService:
    service: ConversationService = request.app.state.service
    return service


ServiceDep = Annotated[ConversationService, Depends(get_service)]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NewConversation(_In):
    kind: ConversationKind = "main"
    title: str | None = Field(default=None, max_length=200)
    #: A model or tier chosen before the first message (the chat is created with it).
    brain: str | None = None


class Rename(_In):
    title: str = Field(min_length=1, max_length=200)


class ChatOut(BaseModel):
    """One past chat, for the list (#38)."""

    id: uuid.UUID
    title: str | None
    brain: str | None
    created_at: datetime
    last_active: datetime
    #: How many messages the user sent in it.
    messages: int


class ApprovalOut(BaseModel):
    """An approval card, as the live ``approval_needed`` event shows it."""

    id: str
    tool: str
    arguments: dict[str, Any]
    summary: str | None
    detail: str | None


class ConversationOut(BaseModel):
    id: uuid.UUID
    kind: str
    title: str | None
    brain: str | None
    #: Cards still waiting for the user, so a reopened app shows them (#39).
    pending_approvals: list[ApprovalOut] = Field(default_factory=list)
    #: A reply is being written right now (another window, or one that lost its stream).
    running: bool = False


class StopOut(BaseModel):
    #: False: no reply was running, so there was nothing to stop.
    stopping: bool


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
        _known_brain(s, body.brain)
        conversation_id = await s.store.create_conversation(body.kind, body.title, body.brain)
        return ConversationOut(
            id=conversation_id, kind=body.kind, title=body.title, brain=body.brain
        )

    @router.get("")
    async def list_conversations(
        s: ServiceDep, limit: Annotated[int, Query(ge=1, le=100)] = 30
    ) -> list[ChatOut]:
        """Past chats with at least one message, most recently active first (#38)."""
        return [
            ChatOut(
                id=c.id,
                title=c.title,
                brain=c.brain,
                created_at=c.created_at,
                last_active=c.last_active,
                messages=c.messages,
            )
            for c in await s.store.chats(limit)
        ]

    @router.patch("/{conversation_id}", status_code=204)
    async def rename(conversation_id: uuid.UUID, body: Rename, s: ServiceDep) -> None:
        await _require(s, conversation_id)
        await s.store.rename(conversation_id, " ".join(body.title.split()))

    @router.delete("/{conversation_id}", status_code=204)
    async def move_to_trash(conversation_id: uuid.UUID, s: ServiceDep) -> None:
        """Out of the list, never destroyed: it can still be opened by id."""
        await _require(s, conversation_id)
        await s.store.move_to_trash(conversation_id)

    @router.get("/{conversation_id}")
    async def get_conversation(conversation_id: uuid.UUID, s: ServiceDep) -> ConversationOut:
        info = await _require(s, conversation_id)
        pending = await s.pending_approvals(conversation_id)
        return ConversationOut(
            id=info.id,
            kind=info.kind,
            title=info.title,
            brain=info.brain,
            pending_approvals=[
                ApprovalOut(
                    id=r.id, tool=r.tool, arguments=r.arguments, summary=r.summary, detail=r.detail
                )
                for r in pending
            ],
            running=s.running(conversation_id),
        )

    @router.post("/{conversation_id}/stop")
    async def stop(conversation_id: uuid.UUID, s: ServiceDep) -> StopOut:
        """Stop the reply being written (#46). What already streamed is kept."""
        await _require(s, conversation_id)
        return StopOut(stopping=s.stop(conversation_id))

    @router.put("/{conversation_id}/brain", status_code=204)
    async def choose_brain(conversation_id: uuid.UUID, body: BrainChoice, s: ServiceDep) -> None:
        _known_brain(s, body.brain)
        await _require(s, conversation_id)
        await s.store.set_brain(conversation_id, body.brain)

    @router.get("/{conversation_id}/messages")
    async def messages(conversation_id: uuid.UUID, s: ServiceDep) -> list[dict[str, Any]]:
        await _require(s, conversation_id)
        took = {t.call_id: t.latency_ms for t in await s.store.tool_calls(conversation_id)}
        runs = await s.store.scheduled(conversation_id)
        shown = [_shown(m, s.registry, took) for m in await s.store.history(conversation_id)]
        for i in runs:
            shown[i]["origin"] = "schedule"  # a scheduled task's run, not the user typing
        return shown

    @router.get("/{conversation_id}/usage")
    async def usage(conversation_id: uuid.UUID, s: ServiceDep) -> ConversationUsageOut:
        """What this chat used, in total and per user message (#49)."""
        await _require(s, conversation_id)
        calls = await s.store.brain_calls(conversation_id)
        turns = per_turn(await s.store.saves(conversation_id), calls)
        return ConversationUsageOut(total=usage_of(calls), turns=[usage_of(t) for t in turns])

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
        raise HTTPException(
            409, "Enzo is still finishing the last reply in this chat. Stop it, or wait."
        ) from exc
    return stream


def _known_brain(s: ConversationService, brain: str | None) -> None:
    if brain is not None and brain not in (*s.router.config.brains, *TIERS):
        raise HTTPException(422, f"unknown model or tier {brain!r}")


def _shown(message: Message, registry: ToolRegistry, took: dict[str, float]) -> dict[str, Any]:
    """A stored message for the chat: each tool call carries its step in plain words (#40)
    and how long it took (#49)."""
    out = message.model_dump(mode="json")
    for part, raw in zip(message.parts, out["parts"], strict=True):
        if isinstance(part, ToolCall):
            raw["label"] = registry.activity(part)
            if part.id in took:
                raw["latency_ms"] = took[part.id]
    return out


def _sse(event: ServiceEvent) -> ServerSentEvent:
    """Client-facing events. Plain data only: no provider payloads leave the gateway."""
    match event:
        case TextDelta(text=text):
            return ServerSentEvent(event="text", data={"text": text})
        case ToolStarted(call=call, label=label):
            return ServerSentEvent(
                event="tool_started", data={"id": call.id, "tool": call.name, "label": label}
            )
        case ToolFinished(outcome=o, label=label):
            data = {
                "id": o.call.id,
                "tool": o.call.name,
                "label": label,
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
            # Who answered; when none did, the last one tried, so a failure names its model (#43).
            answered = next((c for c in reversed(o.brain_calls) if c.outcome == "ok"), None) or (
                o.brain_calls[-1] if o.brain_calls else None
            )
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
                # Tokens, models and time behind the reply (#49).
                "usage": usage_of(o.brain_calls).model_dump(mode="json"),
            }
            return ServerSentEvent(event="done", data=data)
        case TurnFailed(message=message):
            return ServerSentEvent(event="error", data={"message": message})
