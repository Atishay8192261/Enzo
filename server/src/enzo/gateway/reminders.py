"""``/reminders`` (list, cancel), each chat's delivered reminders, and ``/events`` for apps."""

import uuid
from collections.abc import AsyncIterator
from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, params, status
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel

from enzo.schedule import Background, Reminder, ReminderRefusedError
from enzo.schedule.recurrence import zone


class ReminderOut(BaseModel):
    id: int
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
    conversation_id: uuid.UUID | None


class RemindersOut(BaseModel):
    reminders: list[ReminderOut]


class DeliveredOut(BaseModel):
    text: str
    due_at: datetime
    delivered_at: datetime
    late: bool
    #: Shown after this many of the chat's messages.
    after: int
    #: When it was due, in the user's zone: "Fri 09:00".
    due: str


def background(request: Request) -> Background:
    found: Background | None = getattr(request.app.state, "background", None)
    if found is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "reminders are not running")
    return found


BackgroundDep = Annotated[Background, Depends(background)]


def reminder_routes(auth: params.Depends) -> APIRouter:
    router = APIRouter(dependencies=[auth])

    @router.get("/reminders")
    async def list_reminders(bg: BackgroundDep) -> RemindersOut:
        return RemindersOut(reminders=[_out(r) for r in await bg.schedule.active()])

    @router.delete("/reminders/{reminder_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def cancel(reminder_id: int, bg: BackgroundDep) -> None:
        try:
            await bg.schedule.cancel(reminder_id)
        except ReminderRefusedError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None

    @router.get("/conversations/{conversation_id}/reminders")
    async def delivered(conversation_id: uuid.UUID, bg: BackgroundDep) -> list[DeliveredOut]:
        runs = await bg.schedule.delivered(conversation_id)
        tz = {r.id: r.timezone for r in await bg.schedule.active()}
        return [
            DeliveredOut(
                text=r.text,
                due_at=r.due_at,
                delivered_at=r.delivered_at,
                late=r.late,
                after=r.after,
                due=f"{r.due_at.astimezone(zone(tz.get(r.job_id))):%a %H:%M}",
            )
            for r in runs
        ]

    @router.get("/events", response_class=EventSourceResponse)
    async def events(bg: BackgroundDep) -> AsyncIterator[ServerSentEvent]:
        """Live events for open apps: a reminder as it arrives."""
        async for event in bg.bus.subscribe():
            yield ServerSentEvent(event=event.kind, data=event.data)

    return router


def _out(r: Reminder) -> ReminderOut:
    return ReminderOut(
        id=r.id,
        text=r.text,
        next_at=r.next_run_at,
        next=r.next_words,
        schedule=r.schedule,
        timezone=r.timezone,
        repeat=r.repeat,
        ends_on=r.ends_on,
        runs=r.runs,
        conversation_id=r.conversation_id,
    )
