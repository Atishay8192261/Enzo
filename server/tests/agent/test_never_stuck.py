"""A chat never gets stuck: cut-off replies are kept, Stop works mid-reply (#46), and an approval
left open never breaks the history (#39)."""

import asyncio
from collections.abc import AsyncIterator, Sequence

import pytest

from enzo.agent.history import MOVED_ON, NEVER_ANSWERED, close_open_calls, open_calls, repair
from enzo.agent.loop import TurnDone, run_turn
from enzo.brain import Message, ProviderConnectionError, TextDelta, ToolCall, ToolResult, ToolSpec
from enzo.brain.fake import ScriptedBrain
from enzo.brain.models import StreamEvent, ToolChoice
from enzo.brain.router import InMemoryLedger
from tests.agent import tools as t
from tests.agent.strict import Stalls, check_history
from tests.agent.test_loop import USER, make_router


class CutOff(ScriptedBrain):
    """Streams half a reply, then the connection drops."""

    async def stream(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSpec] = (),
        tool_choice: ToolChoice = "auto",
        max_output_tokens: int,
    ) -> AsyncIterator[StreamEvent]:
        yield TextDelta(text="The robot picked up ")
        yield TextDelta(text="a whisk")
        raise ProviderConnectionError("Connection error.", provider="fake", model="m")


async def test_a_reply_cut_off_mid_stream_keeps_what_the_user_saw() -> None:
    events = [e async for e in run_turn(make_router(CutOff([])), t.registry(), USER)]
    done = events[-1]
    assert isinstance(done, TurnDone)
    out = done.outcome
    assert (out.stop, out.failure) == ("brain_failure", "connection")
    assert out.text == (
        "The reply was cut off (lost the connection to the model). "
        "The part above is kept; send again to continue."
    )
    assert out.messages[-1] == Message.assistant("The robot picked up a whisk")  # saved
    check_history(out.messages)


async def test_stop_ends_a_reply_mid_stream_and_keeps_its_words() -> None:
    brain = Stalls()
    router = make_router(brain)
    stop = asyncio.Event()

    async def press_stop() -> None:
        await brain.started.wait()
        stop.set()

    async def turn() -> list[object]:
        return [e async for e in run_turn(router, t.registry(), USER, stop=stop)]

    events, _ = await asyncio.wait_for(asyncio.gather(turn(), press_stop()), timeout=5)
    done = events[-1]
    assert isinstance(done, TurnDone)
    assert (done.outcome.stop, done.outcome.text) == ("cancelled", "Stopped at your request.")
    assert done.outcome.messages[-1] == Message.assistant("Once upon a time")
    assert brain.cancelled  # the model call itself was cancelled, not left running
    ledger = router.ledger
    assert isinstance(ledger, InMemoryLedger)
    assert [r.outcome for r in ledger.records] == ["cancelled"]  # still on record (§1.3.5)


async def test_stop_before_anything_streamed_keeps_nothing() -> None:
    brain = Stalls(words=())
    stop = asyncio.Event()
    turn = run_turn(make_router(brain), t.registry(), USER, stop=stop)

    async def collect() -> list[object]:
        return [e async for e in turn]

    task = asyncio.create_task(collect())
    await asyncio.wait_for(brain.started.wait(), 5)
    stop.set()
    events = await asyncio.wait_for(task, 5)
    done = events[-1]
    assert isinstance(done, TurnDone)
    assert done.outcome.stop == "cancelled"
    assert done.outcome.messages == tuple(USER)


CALL = ToolCall(id="c1", name="delete_file", arguments_json='{"path": "plan.md"}')
OTHER = ToolCall(id="c2", name="read_file", arguments_json='{"path": "x"}')


def test_open_calls_at_the_end_are_closed_as_not_run() -> None:
    history = [Message.user("delete plan.md"), Message.assistant(tool_calls=(CALL,))]
    assert open_calls(history) == (CALL,)
    closing = close_open_calls(history)
    assert closing == Message.tool(
        ToolResult(call_id="c1", name="delete_file", content=MOVED_ON, is_error=True)
    )
    assert close_open_calls([*history, closing]) is None
    check_history([*history, closing, Message.user("never mind")])


def test_a_chat_broken_before_the_fix_is_repaired_when_sent() -> None:
    broken = [
        Message.user("delete plan.md"),
        Message.assistant(tool_calls=(CALL, OTHER)),
        Message.user("hello?"),  # the bug: a new message right after an open call
        Message.assistant("hi"),
    ]
    with pytest.raises(Exception, match="No tool output found for function call c1"):
        check_history(broken)
    fixed = repair(broken)
    check_history(fixed)
    assert fixed[2] == Message.tool(
        ToolResult(call_id="c1", name="delete_file", content=NEVER_ANSWERED, is_error=True),
        ToolResult(call_id="c2", name="read_file", content=NEVER_ANSWERED, is_error=True),
    )
    assert fixed[3:] == broken[2:]


def test_repair_completes_a_partly_answered_reply_in_one_message() -> None:
    answered = ToolResult(call_id="c2", name="read_file", content="text")
    history = [
        Message.user("x"),
        Message.assistant(tool_calls=(CALL, OTHER)),
        Message.tool(answered),
        Message.user("y"),
    ]
    fixed = repair(history)
    check_history(fixed)
    assert len(fixed) == len(history)
    assert fixed[2].parts == (
        answered,
        ToolResult(call_id="c1", name="delete_file", content=NEVER_ANSWERED, is_error=True),
    )


def test_repair_leaves_a_paused_turn_and_valid_history_alone() -> None:
    paused = [Message.user("delete plan.md"), Message.assistant(tool_calls=(CALL,))]
    assert repair(paused) == paused  # resuming runs these calls
    done = [*paused, Message.tool(ToolResult(call_id="c1", name="delete_file", content="ok"))]
    assert repair(done) == done
