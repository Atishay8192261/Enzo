"""The agent gateway: auth, routing to conversations, streaming (spec §2.3, §3)."""

import secrets
import uuid
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel, ConfigDict, Field

from enzo import __version__
from enzo.agent.loop import ApprovalNeeded, ToolFinished, ToolStarted, TurnDone
from enzo.agent.service import (
    ConversationNotFoundError,
    ConversationService,
    ServiceEvent,
    TurnFailed,
    TurnInProgressError,
    TurnMeta,
)
from enzo.brain.config import load_config
from enzo.brain.models import TextDelta
from enzo.brain.router import Router
from enzo.config import Settings
from enzo.store.repository import ApprovalError, ConversationInfo, ConversationKind, Store
from enzo.tools.registry import ToolRegistry


def get_service(request: Request) -> ConversationService:
    service: ConversationService = request.app.state.service
    return service


ServiceDep = Annotated[ConversationService, Depends(get_service)]


class Health(BaseModel):
    """Liveness payload. Clients use it to confirm they reached a compatible gateway."""

    status: Literal["ok"]
    version: str
    env: str


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


class BrainOut(BaseModel):
    name: str
    provider: str
    model: str
    priced: bool


class TurnIn(_In):
    message: str = Field(min_length=1, max_length=100_000)
    timezone: str | None = Field(default=None, max_length=64)
    device: str | None = Field(default=None, max_length=64)


class Decision(_In):
    approved: bool


def create_app(
    settings: Settings | None = None, *, service: ConversationService | None = None
) -> FastAPI:
    """Build the gateway. Tests inject a service; otherwise the real one is built at startup."""
    cfg = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if service is not None:
            app.state.service = service
            yield
            return
        store = Store.from_url(cfg.database_url)
        app.state.service = ConversationService(
            store, Router(load_config(cfg.brains_config)), ToolRegistry()
        )
        try:
            yield
        finally:
            await store.close()

    app = FastAPI(title="Enzo Gateway", version=__version__, lifespan=lifespan)
    if service is not None:
        app.state.service = service  # available even when the ASGI server skips lifespan

    def authorized(authorization: Annotated[str | None, Header()] = None) -> None:
        if cfg.api_token is None:
            return
        expected = f"Bearer {cfg.api_token.get_secret_value()}"
        if authorization is None or not secrets.compare_digest(authorization, expected):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing or wrong bearer token")

    auth = Depends(authorized)

    @app.get("/healthz")
    def healthz() -> Health:
        return Health(status="ok", version=__version__, env=cfg.env)

    @app.get("/brains", dependencies=[auth])
    def brains(s: ServiceDep) -> list[BrainOut]:
        config = s.router.config
        return [
            BrainOut(name=n, provider=b.provider_name, model=b.model, priced=b.pricing is not None)
            for n, b in config.brains.items()
        ]

    @app.post("/conversations", status_code=201, dependencies=[auth])
    async def create_conversation(body: NewConversation, s: ServiceDep) -> ConversationOut:
        conversation_id = await s.store.create_conversation(body.kind, body.title)
        return ConversationOut(id=conversation_id, kind=body.kind, title=body.title, brain=None)

    @app.get("/conversations/{conversation_id}", dependencies=[auth])
    async def get_conversation(conversation_id: uuid.UUID, s: ServiceDep) -> ConversationOut:
        info = await _require(s, conversation_id)
        return ConversationOut(id=info.id, kind=info.kind, title=info.title, brain=info.brain)

    @app.put("/conversations/{conversation_id}/brain", status_code=204, dependencies=[auth])
    async def choose_brain(conversation_id: uuid.UUID, body: BrainChoice, s: ServiceDep) -> None:
        if body.brain is not None and body.brain not in s.router.config.brains:
            raise HTTPException(422, f"unknown brain {body.brain!r}")
        await _require(s, conversation_id)
        await s.store.set_brain(conversation_id, body.brain)

    @app.get("/conversations/{conversation_id}/messages", dependencies=[auth])
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

    @app.post(
        "/conversations/{conversation_id}/turns",
        response_class=EventSourceResponse,
        dependencies=[auth],
    )
    async def turn(
        events: Annotated[AsyncIterator[ServiceEvent], Depends(start_turn)],
    ) -> AsyncIterator[ServerSentEvent]:
        async for event in events:
            yield _sse(event)

    @app.post(
        "/conversations/{conversation_id}/resume",
        response_class=EventSourceResponse,
        dependencies=[auth],
    )
    async def resume(
        events: Annotated[AsyncIterator[ServiceEvent], Depends(start_resume)],
    ) -> AsyncIterator[ServerSentEvent]:
        async for event in events:
            yield _sse(event)

    @app.post(
        "/conversations/{conversation_id}/approvals/{approval_id}",
        status_code=204,
        dependencies=[auth],
    )
    async def decide(
        conversation_id: uuid.UUID, approval_id: str, body: Decision, s: ServiceDep
    ) -> None:
        await _require(s, conversation_id)
        try:
            await s.store.decide(conversation_id, approval_id, approved=body.approved)
        except ApprovalError as exc:
            raise HTTPException(409, str(exc)) from exc

    return app


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
            data = {"id": r.id, "tool": r.tool, "arguments": r.arguments}
            return ServerSentEvent(event="approval_needed", data=data)
        case TurnDone(outcome=o):
            costs = [c.cost_usd for c in o.brain_calls]
            cost = (
                None if not costs or any(c is None for c in costs) else sum(c or 0 for c in costs)
            )
            data = {"stop": o.stop, "text": o.text, "steps": o.steps, "cost_usd": cost}
            return ServerSentEvent(event="done", data=data)
        case TurnFailed(message=message):
            return ServerSentEvent(event="error", data={"message": message})
