"""The brain's helper tools (spec §9.5): hand work to a helper, stop one.

There is deliberately no tool to check on a helper: its report comes back by itself (§9.5 "the
parent must not poll in a loop"), and every new message says which helpers are still at work.
Handing work over needs no approval: the helper acts under the same rules, so anything it would
send, buy or delete still waits for the user, and the number working at once is capped.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from enzo.context import current_conversation
from enzo.subagents.manager import HelperRefusedError, Subagents
from enzo.tools.registry import Tool, plain_words, tool


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DelegateParams(_Params):
    title: str = Field(
        min_length=3,
        max_length=60,
        description="A short name the user sees for this work, like “Compare 5 laptops”.",
    )
    task: str = Field(
        min_length=10,
        max_length=4000,
        description=(
            "What the helper should do, as a complete instruction: it sees only this brief, "
            "not this chat."
        ),
    )
    limits: str = Field(
        min_length=2,
        max_length=1000,
        description=(
            "The user's limits for this work, in their words: budget, sites or sources, what "
            "not to do, a deadline. Write “none given” if they gave none."
        ),
    )
    context: str | None = Field(
        default=None,
        max_length=4000,
        description="What it needs to know from this chat: facts, preferences, what was tried.",
    )


class Delegated(BaseModel):
    helper: int
    status: Literal["working"]
    note: str


class StopParams(_Params):
    helper: int = Field(ge=1, description="The number of the helper to stop.")


class StoppedOut(BaseModel):
    helper: int
    status: str


HANDED = (
    "It works in the background in its own chat; its report comes back to this chat by itself, "
    "and you will review it then. Tell the user in one line that a helper is on it, then carry "
    "on. Never wait for it or check on it."
)


def subagent_tools(helpers: Subagents) -> list[Tool[Any, Any]]:
    def handing(p: DelegateParams) -> str:
        return f"Handing to a helper: {plain_words(p.title, 60)}"

    @tool(
        name="delegate",
        description=(
            "Hand long work to a helper, which does it in the background in its own chat while "
            "you stay free to talk: research and compare several options, read many pages, a "
            "task of many steps. Its report comes back to this chat by itself, and you review it "
            "before telling the user. For work with several parts, hand all of it to one helper, "
            "which splits it among helpers of its own."
        ),
        activity=handing,
    )
    async def delegate(p: DelegateParams) -> Delegated:
        chat = current_conversation()
        if chat is None:
            msg = "Only a chat can hand work to a helper."
            raise ValueError(msg)
        try:
            made = await helpers.start(
                chat, title=p.title, task=p.task, limits=p.limits, context=p.context
            )
        except HelperRefusedError as exc:
            raise ValueError(str(exc)) from None
        return Delegated(helper=made.id, status="working", note=HANDED)

    def stopping(p: StopParams) -> str:
        return f"Stopping helper #{p.helper}"

    @tool(
        name="stop_helper",
        description=(
            "Stop a helper this chat handed work to, when the user no longer wants it. Its work "
            "stops at once, and so does the work of any helpers it handed parts to."
        ),
        activity=stopping,
    )
    async def stop_helper(p: StopParams) -> StoppedOut:
        try:
            done = await helpers.stop_helper(p.helper, by="parent", asking=current_conversation())
        except HelperRefusedError as exc:
            raise ValueError(str(exc)) from None
        return StoppedOut(helper=done.id, status=done.status)

    return [delegate, stop_helper]
