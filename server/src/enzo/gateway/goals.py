"""``/goals``: the user's goals with their steps, evidence and check-ins (spec §6.4, §15.2)."""

import uuid
from datetime import date, datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, params, status
from pydantic import BaseModel

from enzo.gateway.conversations import ServiceDep
from enzo.goals import Evidence, Goal, Goals
from enzo.goals.goals_md import BASIS_WORDS
from enzo.paths import shown


class EvidenceOut(BaseModel):
    evidence: str | None
    #: user_confirmed, verified or timeline_ended.
    basis: str | None
    source: str | None
    happened_on: date | None
    recorded_at: datetime
    #: All of it in words: "you confirmed, 24 Nov 2026, in “Passport”".
    said: str


class StepOut(BaseModel):
    number: int
    text: str
    done: bool
    done_at: datetime | None
    evidence: EvidenceOut | None


class CheckInOut(BaseModel):
    id: int
    kind: str
    text: str
    #: "every Monday at 09:00".
    schedule: str
    #: "Mon 30 Nov 09:00".
    next: str | None


class GoalOut(BaseModel):
    id: int
    title: str
    status: str
    due_on: date | None
    #: The due date in words: "Tue 15 Dec 2026".
    due: str | None
    #: Where its work lives, as the user types it: ~/Enzo/passport.
    folder: str | None
    created_at: datetime
    closed_at: datetime | None
    #: The chat it was set in, when it still exists.
    chat: str | None
    conversation_id: uuid.UUID | None
    steps: list[StepOut]
    check_ins: list[CheckInOut]
    #: Why it was closed, when it is.
    closing: EvidenceOut | None


class GoalListOut(BaseModel):
    goals: list[GoalOut]
    #: Where the read-only copy is, as the user types it: ~/Enzo/Goals.md.
    file: str | None


def goal_routes(auth: params.Depends) -> APIRouter:
    router = APIRouter(prefix="/goals", dependencies=[auth])

    @router.get("")
    async def list_goals(s: ServiceDep, which: Literal["open", "closed"] = "open") -> GoalListOut:
        goals = _goals(s.goals)
        found = await goals.open() if which == "open" else await goals.closed()
        return GoalListOut(
            goals=[_out(g, goals) for g in found], file=shown(goals.file) if goals.file else None
        )

    return router


def _goals(goals: Goals | None) -> Goals:
    if goals is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "goals are not set up")
    return goals


def _evidence(e: Evidence | None) -> EvidenceOut | None:
    if e is None:
        return None
    when = e.happened_on or e.recorded_at.date()
    where = f", in “{e.chat}”" if e.chat and not e.source else ""
    said = f"{BASIS_WORDS.get(e.basis or '', 'recorded')}, {when:%-d %b %Y}{where}"
    return EvidenceOut(
        evidence=e.evidence,
        basis=e.basis,
        source=e.source,
        happened_on=e.happened_on,
        recorded_at=e.recorded_at,
        said=said,
    )


def _out(g: Goal, goals: Goals) -> GoalOut:
    return GoalOut(
        id=g.id,
        title=g.title,
        status=g.status,
        due_on=g.due_on,
        due=f"{g.due_on:%a %-d %b %Y}" if g.due_on else None,
        folder=goals.shown_folder(g.path) if g.path else None,
        created_at=g.created_at,
        closed_at=g.closed_at,
        chat=g.chat,
        conversation_id=g.conversation_id,
        steps=[
            StepOut(
                number=s.number,
                text=s.text,
                done=s.done,
                done_at=s.done_at,
                evidence=_evidence(s.evidence),
            )
            for s in g.steps
        ],
        check_ins=[
            CheckInOut(id=j.id, kind=j.kind, text=j.text, schedule=j.schedule, next=j.next_words)
            for j in g.check_ins
        ],
        closing=_evidence(g.closing),
    )
