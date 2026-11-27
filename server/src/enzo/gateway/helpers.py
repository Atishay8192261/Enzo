"""``/helpers`` (list, stop) and each chat's helper notices (spec §9.5, ADR 0028)."""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, params, status
from pydantic import BaseModel

from enzo.gateway.usage import usage_of
from enzo.subagents import Helper, HelperRefusedError, Subagents


class HelperOut(BaseModel):
    id: int
    title: str
    #: working, waiting (for the user's approval in its chat), done, failed, stopped, interrupted.
    status: str
    #: 1: the user's chat handed it the work; 2: another helper did.
    depth: int
    #: Its own chat, where it works (open it to watch, answer its approval, or stop it).
    chat_id: uuid.UUID | None
    #: The chat that handed it the work, and the user's own chat at the top.
    parent_id: uuid.UUID | None
    conversation_id: uuid.UUID | None
    coordinator_id: int | None
    created_at: datetime
    finished_at: datetime | None
    #: Steps it took (tool calls), and what its model calls used: it counts against the
    #: user's own accounts.
    steps: int
    calls: int
    cost_usd: float | None
    plan: bool
    #: While it waits: the action it asks the user to approve, in plain words.
    waiting: str | None = None


class HelperListOut(BaseModel):
    helpers: list[HelperOut]


class StoppedOut(BaseModel):
    #: False: it was not working any more, so there was nothing to stop.
    stopped: bool
    helper: HelperOut


class HelperNoticeOut(BaseModel):
    helper: int
    kind: str
    text: str
    #: Shown after this many of the chat's messages.
    after: int
    at: datetime
    #: The helper's own chat.
    chat_id: uuid.UUID | None


def helpers_of(request: Request) -> Subagents:
    found: Subagents | None = getattr(request.app.state, "helpers", None)
    if found is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "helpers are not running")
    return found


HelpersDep = Annotated[Subagents, Depends(helpers_of)]


def helper_routes(auth: params.Depends) -> APIRouter:
    router = APIRouter(dependencies=[auth])

    @router.get("/helpers")
    async def list_helpers(
        h: HelpersDep, limit: Annotated[int, Query(ge=1, le=100)] = 30
    ) -> HelperListOut:
        """Helpers at work first, then the latest that finished."""
        return HelperListOut(helpers=[await _out(h, x) for x in await h.store.recent(limit)])

    @router.post("/helpers/{helper_id}/stop")
    async def stop(helper_id: int, h: HelpersDep) -> StoppedOut:
        """Stop a helper (and the helpers it handed parts to): its work stops at once."""
        found = await h.store.get(helper_id)
        if found is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"no helper {helper_id}")
        try:
            stopped = await h.stop_helper(helper_id, by="user")
        except HelperRefusedError:  # not working any more
            return StoppedOut(stopped=False, helper=await _out(h, found))
        return StoppedOut(stopped=True, helper=await _out(h, stopped))

    @router.get("/conversations/{conversation_id}/helpers")
    async def notices(conversation_id: uuid.UUID, h: HelpersDep) -> list[HelperNoticeOut]:
        """The lines about helpers in a chat's timeline, in order."""
        return [
            HelperNoticeOut(
                helper=n.helper_id,
                kind=n.kind,
                text=n.text,
                after=n.after,
                at=n.created_at,
                chat_id=n.chat_id,
            )
            for n in await h.store.notices(conversation_id)
        ]

    return router


async def _out(h: Subagents, x: Helper) -> HelperOut:
    store = h.service.store
    calls = await store.brain_calls(x.chat_id) if x.chat_id else []
    steps = len(await store.tool_calls(x.chat_id)) if x.chat_id else 0
    used = usage_of(calls)
    waiting = None
    if x.status == "waiting" and x.chat_id is not None:
        cards = await h.service.pending_approvals(x.chat_id)
        waiting = next((c.summary for c in cards if c.summary), "an action") if cards else None
    return HelperOut(
        id=x.id,
        title=x.title,
        status=x.status,
        depth=x.depth,
        chat_id=x.chat_id,
        parent_id=x.parent_id,
        conversation_id=x.root_id,
        coordinator_id=x.coordinator_id,
        created_at=x.created_at,
        finished_at=x.finished_at,
        steps=steps,
        calls=used.calls,
        cost_usd=used.cost_usd,
        plan=used.plan,
        waiting=waiting,
    )
