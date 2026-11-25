"""Tools that ask only sometimes (ADR 0027): the prepared call says whether this one needs a yes.

A browser step is approved once per task, but a click that would place an order must still ask.
The tool's ``prepare`` looks at the live target, pins the call to it, and says whether to ask.
"""

import json

from pydantic import BaseModel, ConfigDict

from enzo.agent.approvals import approval_request
from enzo.agent.loop import ApprovalNeeded, ToolStarted
from enzo.brain import Message, ToolCall, ToolResult
from enzo.brain.fake import ScriptedBrain
from enzo.tools import Prepared, ToolRegistry, ToolStoppedError, tool
from tests.agent.test_loop import run


class Press(BaseModel):
    model_config = ConfigDict(extra="forbid")
    button: str
    #: Pinned by prepare: what the button really is right now.
    seen: str | None = None


class Pressed(BaseModel):
    pressed: str


#: What each button "is" on the live page; tests change it to simulate the page changing.
page: dict[str, str] = {}
pressed: list[Press] = []


async def look(args: Press) -> Prepared[Press]:
    what = page[args.button]
    pinned = args.model_copy(update={"seen": what})
    if what.startswith("buys"):
        return Prepared(pinned, summary=f"Press “{args.button}”: this buys it", ask=True)
    return Prepared(pinned)


@tool(
    name="press",
    description="Press a button.",
    prepare=look,
    activity=lambda a: f"Pressing {a.button} ({a.seen})",
)
async def press(args: Press) -> Pressed:
    pressed.append(args)
    return Pressed(pressed=args.button)


@tool(name="always_ask", description="Always asks.", requires_approval=True, prepare=look)
async def always_ask(args: Press) -> Pressed:
    pressed.append(args)
    return Pressed(pressed=args.button)


@tool(name="halt", description="Stopped by the user while running.")
async def halt(args: Press) -> Pressed:
    msg = f"Stopped: the user stopped it ({args.button})"
    raise ToolStoppedError(msg)


def registry() -> ToolRegistry:
    pressed.clear()
    return ToolRegistry([press, always_ask, halt])


def press_call(button: str, cid: str = "p1", name: str = "press") -> ToolCall:
    return ToolCall(id=cid, name=name, arguments_json=json.dumps({"button": button}))


def reply(*calls: ToolCall) -> Message:
    return Message.assistant(tool_calls=calls)


async def test_a_harmless_step_runs_as_prepared_without_asking() -> None:
    page.update({"next": "goes to the next page"})
    brain = ScriptedBrain([reply(press_call("next")), Message.assistant("done")])
    events, out = await run(brain, registry())
    assert out.stop == "final_answer"
    assert not [e for e in events if isinstance(e, ApprovalNeeded)]
    # It ran pinned to what prepare saw, and the step says so in plain words.
    assert pressed == [Press(button="next", seen="goes to the next page")]
    started = [e for e in events if isinstance(e, ToolStarted)]
    assert [e.label for e in started] == ["Pressing next (goes to the next page)"]


async def test_an_irreversible_step_asks_with_its_own_words_then_runs_once_approved() -> None:
    page.update({"buy": "buys"})
    brain = ScriptedBrain([reply(press_call("buy")), Message.assistant("bought")])
    registry_ = registry()
    events, paused = await run(brain, registry_)
    assert paused.stop == "approval_pending"
    assert pressed == []  # nothing bought before the yes
    (request,) = paused.pending
    assert request.summary == "Press “buy”: this buys it"
    assert request.arguments == {"button": "buy", "seen": "buys"}
    assert [e.request for e in events if isinstance(e, ApprovalNeeded)] == [request]
    _, done = await run(brain, registry_, list(paused.messages), decisions={request.id: True})
    assert done.stop == "final_answer"
    assert pressed == [Press(button="buy", seen="buys")]


async def test_a_step_whose_target_changed_while_waiting_asks_again() -> None:
    """§12.3: if the content changes after approval, ask again (ADR 0014)."""
    page.update({"buy": "buys"})
    brain = ScriptedBrain([reply(press_call("buy")), Message.assistant("bought")])
    registry_ = registry()
    _, paused = await run(brain, registry_)
    (request,) = paused.pending
    # While the card waited, the button changed to buy something else.
    page["buy"] = "buys 3 of them"
    history = list(paused.messages)
    _, again = await run(brain, registry_, history, decisions={request.id: True})
    assert again.stop == "approval_pending"  # the old yes does not cover the new target
    (new_request,) = again.pending
    assert new_request.id != request.id
    assert new_request.arguments["seen"] == "buys 3 of them"
    assert (
        new_request.id
        == approval_request(request.call, Press(button="buy", seen="buys 3 of them")).id
    )
    assert pressed == []


async def test_a_declined_step_does_not_run() -> None:
    page.update({"buy": "buys"})
    brain = ScriptedBrain([reply(press_call("buy")), Message.assistant("ok, not buying")])
    _, paused = await run(brain, registry())
    (request,) = paused.pending
    _, done = await run(brain, registry(), list(paused.messages), decisions={request.id: False})
    assert done.stop == "final_answer"
    assert pressed == []


async def test_a_gated_tool_always_asks_even_if_prepare_says_it_need_not() -> None:
    page.update({"next": "goes to the next page"})
    brain = ScriptedBrain([reply(press_call("next", name="always_ask"))])
    _, out = await run(brain, registry())
    assert out.stop == "approval_pending"
    assert pressed == []


async def test_a_step_the_user_stopped_says_stopped_not_failed() -> None:
    brain = ScriptedBrain([reply(press_call("x", name="halt")), Message.assistant("stopped")])
    _, out = await run(brain, registry())
    (outcome,) = out.tool_outcomes
    assert outcome.status == "stopped"
    (result,) = [p for m in out.messages if m.role == "tool" for p in m.parts]
    assert isinstance(result, ToolResult)
    assert json.loads(result.content) == {
        "error": "stopped",
        "message": "Stopped: the user stopped it (x)",
    }
