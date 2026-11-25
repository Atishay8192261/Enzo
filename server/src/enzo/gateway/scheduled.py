"""``/scheduled`` (list, cancel) for reminders and tasks, each chat's scheduled notices, and
``/events`` for open apps."""

import uuid
from collections.abc import AsyncIterator
from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, params, status
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel

from enzo.schedule import Background, Job, ScheduleRefusedError
from enzo.schedule.recurrence import zone


class ScheduledOut(BaseModel):
    id: int
    kind: str
    #: A reminder's words, or what a task does on each run.
    text: str
    #: The next time, in full and in the user's words ("Fri 21 Nov 09:00").
    next_at: datetime | None
    next: str | None
    #: "every weekday at 09:00, until 1 Dec 2026".
    schedule: str
    timezone: str
    repeat: str
    ends_on: date | None
    runs: int
    #: A task: when it is done (its runs stay quiet until then).
    stop_when: str | None
    conversation_id: uuid.UUID | None
    #: A task's own chat, where its runs happen.
    chat_id: uuid.UUID | None


class ScheduledListOut(BaseModel):
    scheduled: list[ScheduledOut]


class NoticeOut(BaseModel):
    kind: str
    #: A task: what it does.
    task: str | None
    #: A reminder's words, or the run's report.
    text: str
    due_at: datetime
    delivered_at: datetime
    late: bool
    #: Shown after this many of the chat's messages.
    after: int
    #: delivered (a reminder); reported, quiet, finished, needs_approval or failed (a task run).
    outcome: str
    #: When it was due, in the user's zone: "Fri 09:00".
    due: str
    chat_id: uuid.UUID | None


def background(request: Request) -> Background:
    found: Background | None = getattr(request.app.state, "background", None)
    if found is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "scheduling is not running")
    return found


BackgroundDep = Annotated[Background, Depends(background)]


def scheduled_routes(auth: params.Depends) -> APIRouter:
    router = APIRouter(dependencies=[auth])

    @router.get("/scheduled")
    async def list_scheduled(bg: BackgroundDep) -> ScheduledListOut:
        return ScheduledListOut(scheduled=[_out(j) for j in await bg.schedule.active()])

    @router.delete("/scheduled/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def cancel(job_id: int, bg: BackgroundDep) -> None:
        try:
            await bg.schedule.cancel(job_id)
        except ScheduleRefusedError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None

    @router.get("/conversations/{conversation_id}/scheduled")
    async def notices(conversation_id: uuid.UUID, bg: BackgroundDep) -> list[NoticeOut]:
        return [
            NoticeOut(
                kind=n.kind,
                task=n.task,
                text=n.text,
                due_at=n.due_at,
                delivered_at=n.delivered_at,
                late=n.late,
                after=n.after,
                outcome=n.outcome,
                due=f"{n.due_at.astimezone(zone(n.timezone)):%a %H:%M}",
                chat_id=n.chat_id,
            )
            for n in await bg.schedule.delivered(conversation_id)
        ]

    @router.get("/events", response_class=EventSourceResponse)
    async def events(bg: BackgroundDep) -> AsyncIterator[ServerSentEvent]:
        """Live events for open apps: a reminder or a task report as it arrives."""
        async for event in bg.bus.subscribe():
            yield ServerSentEvent(event=event.kind, data=event.data)

    return router


def _out(j: Job) -> ScheduledOut:
    return ScheduledOut(
        id=j.id,
        kind=j.kind,
        text=j.text,
        next_at=j.next_run_at,
        next=j.next_words,
        schedule=j.schedule,
        timezone=j.timezone,
        repeat=j.repeat,
        ends_on=j.ends_on,
        runs=j.runs,
        stop_when=j.stop_when,
        conversation_id=j.conversation_id,
        chat_id=j.chat_id,
    )
