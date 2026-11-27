"""``/privacy``: the discretion rules the user stated, and lifting one (spec §12.5, ADR 0029)."""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, params, status
from pydantic import BaseModel

from enzo.gateway.conversations import ServiceDep
from enzo.privacy import Discretion, Rule, RuleRefusedError


class RuleOut(BaseModel):
    id: int
    #: The rule in plain words: "Don't mention my health to anyone".
    text: str
    #: The kinds of private detail it covers, and its own words.
    kinds: list[str]
    words: list[str]
    #: All of it in words: "health, and the words “work”".
    covers: str
    #: "all": every chat; "chat": only the chat it was set in.
    scope: Literal["all", "chat"]
    conversation_id: uuid.UUID | None
    #: That chat's title, when it has one.
    chat: str | None
    #: The user's own words that stated it.
    quote: str
    created_at: datetime


def privacy_routes(auth: params.Depends) -> APIRouter:
    router = APIRouter(prefix="/privacy", dependencies=[auth])

    @router.get("")
    async def list_rules(
        s: ServiceDep, conversation: Annotated[uuid.UUID | None, Query()] = None
    ) -> list[RuleOut]:
        """The rules that hold in a chat (its own and every chat's); without one, every
        chat's rules only."""
        rules = await _discretion(s.discretion).rules.applying(conversation)
        return [_out(r) for r in rules]

    @router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def remove(rule_id: int, s: ServiceDep) -> None:
        try:
            await _discretion(s.discretion).rules.remove(rule_id)
        except RuleRefusedError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None

    return router


def _discretion(discretion: Discretion | None) -> Discretion:
    if discretion is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "privacy rules are not set up")
    return discretion


def _out(r: Rule) -> RuleOut:
    return RuleOut(
        id=r.id,
        text=r.text,
        kinds=list(r.kinds),
        words=list(r.words),
        covers=r.covers(),
        scope="all" if r.conversation_id is None else "chat",
        conversation_id=r.conversation_id,
        chat=r.chat,
        quote=r.quote,
        created_at=r.created_at,
    )
