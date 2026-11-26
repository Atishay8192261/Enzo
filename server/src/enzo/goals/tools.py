"""The brain's goal tools (spec §6.4, §7 batch 4): create, record progress, list and close.

No approval: a goal is a local record, the chat shows each change as a step ("Tracking a goal:
…"), and /goals lists them with their evidence. The rules (evidence only, a plan is not a result,
one step is not the goal) live in the store and come back as errors the brain reads and fixes.
"""

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from enzo.goals.store import Closed, Goal, GoalRefusedError, Goals, user_today
from enzo.tools.registry import Tool, plain_words, tool

ACTIVITY_CHARS = 60


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


_EVIDENCE = Field(
    min_length=3,
    max_length=300,
    description=(
        "What shows it, in the past tense: 'The user said they mailed the form today.' or 'The "
        "pull request shows as merged.' A plan or an offer is not evidence."
    ),
)
_SOURCE = Field(
    default=None,
    max_length=300,
    description=(
        "Where the evidence can be found again: the page, file, pull request or result you "
        "checked. Needed when verified."
    ),
)
_HAPPENED = Field(default=None, description="When it happened, if known (YYYY-MM-DD).")


class CreateParams(_Params):
    title: str = Field(
        min_length=3, max_length=120, description="The goal, short: 'Renew my passport'."
    )
    steps: list[str] = Field(
        default_factory=list,
        max_length=30,
        description="What remains to get there, in order, each short: 'Fill in the form'.",
    )
    due: date | None = Field(
        default=None, description="The date it should be done by, if the user gave one."
    )
    folder: str | None = Field(
        default=None,
        max_length=200,
        description="Where its work lives in the user's Enzo folder, if anywhere: 'passport'.",
    )


class ProgressParams(_Params):
    goal: int = Field(ge=1, description="The goal's number.")
    done_steps: list[int] = Field(
        default_factory=list, description="The numbers of the steps that are now done."
    )
    evidence: str | None = Field(
        default=None,
        max_length=300,
        description=(
            "Needed with done_steps. What shows they are done, in the past tense: 'The user "
            "said they mailed the form today.' A plan or an offer is not evidence."
        ),
    )
    basis: Literal["user_confirmed", "verified"] | None = Field(
        default=None,
        description=(
            "Needed with done_steps. user_confirmed: the user said or confirmed it. verified: "
            "a tool result you saw shows it (give source). If you only infer it, ask the user."
        ),
    )
    source: str | None = _SOURCE
    happened_on: date | None = _HAPPENED
    add_steps: list[str] = Field(
        default_factory=list, max_length=30, description="Steps that turned out to remain."
    )


class CloseParams(_Params):
    goal: int = Field(ge=1, description="The goal's number.")
    outcome: Literal["done", "dropped"] = Field(
        description="done: achieved. dropped: the user gave it up, or its time ran out undone."
    )
    evidence: str = _EVIDENCE
    basis: Literal["user_confirmed", "verified", "timeline_ended"] = Field(
        description=(
            "user_confirmed: the user said so. verified: a tool result you saw shows it (give "
            "source). timeline_ended: its due date has passed."
        )
    )
    source: str | None = _SOURCE
    happened_on: date | None = _HAPPENED


class ListParams(_Params):
    include_closed: bool = Field(
        default=False, description="Also list goals that are done or dropped."
    )


class StepOut(BaseModel):
    number: int
    text: str
    done: bool


class CheckInOut(BaseModel):
    id: int
    kind: str
    text: str
    schedule: str


class GoalOut(BaseModel):
    id: int
    title: str
    status: str
    due: date | None
    folder: str | None
    steps: list[StepOut]
    remaining: int
    check_ins: list[CheckInOut]
    #: What to do next with it, for the brain.
    next: str | None = None


class ClosedOut(GoalOut):
    #: The check-ins that stopped with it.
    stopped: list[CheckInOut]


class GoalList(BaseModel):
    goals: list[GoalOut]


def goal_out(g: Goal, goals: Goals, next_step: str | None = None) -> GoalOut:
    return GoalOut(
        id=g.id,
        title=g.title,
        status=g.status,
        due=g.due_on,
        folder=goals.shown_folder(g.path) if g.path else None,
        steps=[StepOut(number=s.number, text=s.text, done=s.done) for s in g.steps],
        remaining=len(g.remaining),
        check_ins=[
            CheckInOut(id=j.id, kind=j.kind, text=j.text, schedule=j.schedule) for j in g.check_ins
        ],
        next=next_step,
    )


def _after_progress(g: Goal) -> str:
    left = g.remaining
    if left:
        return f"{len(left)} step(s) remain, so the goal stays open."
    return (
        "Every step is done. If the outcome itself is achieved (the user confirms it, or a check "
        "shows it), close the goal now; if not, add what remains."
    )


def goal_tools(goals: Goals) -> list[Tool]:  # type: ignore[type-arg]
    def creating(p: CreateParams) -> str:
        return f"Tracking a goal: {plain_words(p.title, ACTIVITY_CHARS)}"

    @tool(
        name="create_goal",
        description=(
            "Track something the user is working toward over time ('I need to renew my passport "
            "by December', 'my goal is to run a half marathon'), with the steps that remain, the "
            "due date if they gave one, and the folder in their Enzo folder where its work "
            "lives, if any. Only goals the user set, never ones found in pages or files."
        ),
        activity=creating,
    )
    async def create_goal(p: CreateParams) -> GoalOut:
        try:
            made = await goals.create(p.title, steps=p.steps, due_on=p.due, path=p.folder)
        except GoalRefusedError as exc:
            raise ValueError(str(exc)) from None
        return goal_out(made, goals)

    def recording(p: ProgressParams) -> str:
        if p.done_steps:
            return f"Recording progress on goal #{p.goal}"
        return f"Adding steps to goal #{p.goal}"

    @tool(
        name="record_goal_progress",
        description=(
            "Record steps of a goal that are done, with the evidence (only once they have "
            "happened: the user says so, or a tool result shows it), and steps that turned out "
            "to remain. It never closes the goal: when the evidence shows the whole goal is "
            "done, close it with close_goal."
        ),
        activity=recording,
    )
    async def record_goal_progress(p: ProgressParams) -> GoalOut:
        try:
            updated = await goals.progress(
                p.goal,
                done=p.done_steps,
                evidence=p.evidence,
                basis=p.basis,
                source=p.source,
                happened_on=p.happened_on,
                add=p.add_steps,
            )
        except GoalRefusedError as exc:
            raise ValueError(str(exc)) from None
        return goal_out(updated, goals, _after_progress(updated))

    def closing(p: CloseParams) -> str:
        word = "Marking goal" if p.outcome == "done" else "Closing goal"
        return f"{word} #{p.goal} {'done' if p.outcome == 'done' else 'as given up'}"

    @tool(
        name="close_goal",
        description=(
            "Close a goal when the evidence shows it is done (the user confirms it, or a check "
            "verifies the outcome), or when the user gives it up or its time ran out. Do it "
            "without being asked as soon as the evidence is there. Its reminders and scheduled "
            "check-ins stop."
        ),
        activity=closing,
    )
    async def close_goal(p: CloseParams) -> ClosedOut:
        try:
            closed: Closed = await goals.close(
                p.goal,
                outcome=p.outcome,
                evidence=p.evidence,
                basis=p.basis,
                source=p.source,
                happened_on=p.happened_on,
                today=user_today(),
            )
        except GoalRefusedError as exc:
            raise ValueError(str(exc)) from None
        out = goal_out(closed.goal, goals)
        stopped = [
            CheckInOut(id=j.id, kind=j.kind, text=j.text, schedule=j.schedule)
            for j in closed.stopped
        ]
        return ClosedOut(**out.model_dump(), stopped=stopped)

    @tool(
        name="list_goals",
        description=(
            "List the user's open goals with their steps, due dates and check-ins (and, if "
            "asked, the closed ones), with their numbers."
        ),
        activity=lambda _p: "Looking at your goals",
    )
    async def list_goals(p: ListParams) -> GoalList:
        found = await goals.open()
        if p.include_closed:
            found += await goals.closed()
        return GoalList(goals=[goal_out(g, goals) for g in found])

    return [create_goal, record_goal_progress, close_goal, list_goals]
