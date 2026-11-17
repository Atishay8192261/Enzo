import asyncio
import json
import time
from typing import Any

from enzo.agent.approvals import approval_request
from enzo.agent.loop import (
    AgentEvent,
    ApprovalNeeded,
    ToolFinished,
    ToolStarted,
    TurnDone,
    TurnOutcome,
    run_turn,
)
from enzo.brain import (
    BrainResponse,
    Message,
    RateLimitError,
    TextDelta,
    ToolCall,
    ToolResult,
    Usage,
)
from enzo.brain.config import BrainConfig, BrainsConfig
from enzo.brain.fake import ScriptedBrain
from enzo.brain.provider import BrainProvider
from enzo.brain.router import Router
from enzo.tools import ToolRegistry
from tests.agent import tools as t
from tests.agent.tools import Args

USER = [Message.user("do the thing")]


def make_router(brain: ScriptedBrain) -> Router:
    config = BrainsConfig.model_validate(
        {
            "brains": {"main": {"api": "anthropic", "model": "m", "api_key_env": "KEY"}},
            "routing": {"default": "main"},
        }
    )

    def build(_: BrainConfig) -> BrainProvider:
        return brain

    return Router(config, build=build)


def call(name: str, value: Any, cid: str = "c1") -> ToolCall:
    return ToolCall(id=cid, name=name, arguments_json=json.dumps({"value": value}))


def calls(*items: ToolCall) -> Message:
    return Message.assistant(tool_calls=items)


def results(messages: tuple[Message, ...] | list[Message]) -> list[ToolResult]:
    return [p for m in messages if m.role == "tool" for p in m.parts if isinstance(p, ToolResult)]


async def run(
    brain: ScriptedBrain, registry: ToolRegistry, history: list[Message] | None = None, **kw: Any
) -> tuple[list[AgentEvent], TurnOutcome]:
    events = [e async for e in run_turn(make_router(brain), registry, history or USER, **kw)]
    done = events[-1]
    assert isinstance(done, TurnDone)
    return events, done.outcome


async def test_plain_answer_streams_and_finishes() -> None:
    events, out = await run(ScriptedBrain([Message.assistant("all done here")]), t.registry())
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["all", "done", "here"]
    assert (out.stop, out.text, out.steps) == ("final_answer", "all done here", 1)
    assert len(out.brain_calls) == 1


async def test_three_tools_in_sequence_complete_in_order() -> None:
    """Spec §4.3: a task that needs 3 tools in sequence completes in order."""
    brain = ScriptedBrain(
        [
            calls(call("step", "a")),
            calls(call("step", "b", "c2")),
            calls(call("step", "c", "c3")),
            Message.assistant("did a, b and c"),
        ]
    )
    registry = t.registry()
    _, out = await run(brain, registry)
    assert t.ran == [("step", "a"), ("step", "b"), ("step", "c")]
    assert (out.stop, out.text, out.steps) == ("final_answer", "did a, b and c", 4)
    last_seen = brain.calls[-1].messages
    assert [json.loads(r.content) for r in results(last_seen)] == [
        {"value": "did a"},
        {"value": "did b"},
        {"value": "did c"},
    ]


async def test_invalid_call_is_rejected_before_running_then_retried() -> None:
    """Spec §4.3: invalid argument rejected before execution, then retried correctly."""
    brain = ScriptedBrain(
        [calls(call("step", 42)), calls(call("step", "fixed", "c2")), Message.assistant("ok")]
    )
    registry = t.registry()
    _, out = await run(brain, registry)
    assert t.ran == [("step", "fixed")]
    (first_result,) = results(brain.calls[1].messages)
    assert first_result.is_error
    assert "value: Input should be a valid string" in first_result.content
    assert out.stop == "final_answer"


async def test_a_looping_task_stops_at_max_steps_with_a_report() -> None:
    """Spec §4.3: a task that would loop stops at the limit and says what was tried."""
    brain = ScriptedBrain([calls(call("step", str(i), f"c{i}")) for i in range(10)])
    _, out = await run(brain, t.registry(), max_steps=3)
    assert (out.stop, out.steps, len(brain.calls)) == ("max_steps", 3, 3)
    assert "Stopped after 3 steps" in out.text
    assert "step (ok) x3" in out.text
    assert "Still open: do the thing" in out.text


async def test_parallel_calls_run_together_and_answer_in_one_message() -> None:
    brain = ScriptedBrain(
        [calls(call("step", "x", "c1"), call("step", "y", "c2")), Message.assistant("done")]
    )
    started = time.perf_counter()
    events, out = await run(brain, t.registry())
    assert time.perf_counter() - started < 0.19  # two 0.1s tools, concurrently
    tool_messages = [m for m in out.messages if m.role == "tool"]
    assert len(tool_messages) == 1
    assert [r.call_id for r in results(tool_messages)] == ["c1", "c2"]
    kinds = [type(e).__name__ for e in events if isinstance(e, ToolStarted | ToolFinished)]
    assert kinds == ["ToolStarted", "ToolStarted", "ToolFinished", "ToolFinished"]


async def test_a_failing_tool_never_crashes_the_turn() -> None:
    brain = ScriptedBrain([calls(call("boom", "x")), Message.assistant("that tool failed, sorry")])
    _, out = await run(brain, t.registry())
    assert out.stop == "final_answer"
    assert out.tool_outcomes[0].status == "failed"


async def test_irreversible_tools_wait_for_approval_of_the_exact_call() -> None:
    email = call("send_email", "hello bob")
    registry = t.registry()
    brain = ScriptedBrain([calls(email), Message.assistant("sent it")])
    events, paused = await run(brain, registry)
    assert paused.stop == "approval_pending"
    assert t.ran == []  # nothing sent without approval
    (request,) = paused.pending
    assert [e.request for e in events if isinstance(e, ApprovalNeeded)] == [request]
    assert request.arguments == {"value": "hello bob"}

    # Resume with the user's yes for this exact call.
    _, done = await run(brain, registry, list(paused.messages), decisions={request.id: True})
    assert t.ran == [("send_email", "hello bob")]
    assert (done.stop, done.text) == ("final_answer", "sent it")


def test_changed_content_needs_a_new_approval() -> None:
    a = approval_request(call("send_email", "hello bob"), Args(value="hello bob"))
    b = approval_request(call("send_email", "hello BOB"), Args(value="hello BOB"))
    same = approval_request(call("send_email", "hello bob", "other-id"), Args(value="hello bob"))
    assert a.id != b.id
    assert a.id == same.id  # the same action and content is the same approval


async def test_an_approval_for_other_content_does_not_unlock_a_call() -> None:
    old = approval_request(call("send_email", "draft 1"), Args(value="draft 1"))
    brain = ScriptedBrain([calls(call("send_email", "draft 2")), Message.assistant("x")])
    _, out = await run(brain, t.registry(), decisions={old.id: True})
    assert out.stop == "approval_pending"
    assert t.ran == []


async def test_a_declined_action_is_reported_to_the_brain_not_run() -> None:
    email = call("send_email", "hi")
    request = approval_request(email, Args(value="hi"))
    brain = ScriptedBrain([calls(email), Message.assistant("ok, not sending")])
    _, out = await run(brain, t.registry(), decisions={request.id: False})
    assert t.ran == []
    assert out.tool_outcomes[0].status == "declined"
    assert out.text == "ok, not sending"


def reply(message: Message, stop: str) -> BrainResponse:
    return BrainResponse(
        provider="fake",
        model="m",
        message=message,
        stop_reason=stop,
        usage=Usage(input_tokens=1, output_tokens=1),
        latency_ms=0,
    )


async def test_refusal_ends_the_turn() -> None:
    _, out = await run(
        ScriptedBrain([reply(Message.assistant("I can't help with that."), "refusal")]),
        t.registry(),
    )
    assert (out.stop, out.text) == ("refusal", "I can't help with that.")


async def test_cut_off_tool_calls_are_never_run_and_history_stays_consistent() -> None:
    registry = t.registry()
    brain = ScriptedBrain([reply(calls(call("step", "partial")), "max_tokens")])
    _, out = await run(brain, registry)
    assert out.stop == "truncated"
    assert t.ran == []
    assert out.messages[-1].role == "tool"  # answered, so a resume will not run it either
    # Resuming the stored history asks the brain again instead of running the cut-off call.
    _, resumed = await run(
        ScriptedBrain([Message.assistant("retrying differently")]), registry, list(out.messages)
    )
    assert t.ran == []
    assert resumed.stop == "final_answer"


async def test_brain_failure_is_reported_honestly() -> None:
    brain = ScriptedBrain([RateLimitError("slow down", provider="fake", model="m")])
    _, out = await run(brain, t.registry())
    assert out.stop == "brain_failure"
    assert "slow down" in out.text
    # Spec §1.3.4 step 1: the failed attempt is recorded, not lost with the error.
    assert [(c.brain, c.outcome) for c in out.brain_calls] == [("main", "rate_limit")]


async def test_over_budget_stops_before_calling_the_brain() -> None:
    brain = ScriptedBrain([Message.assistant("never")])
    _, out = await run(brain, t.registry(), budget_tokens=10, max_output_tokens=50)
    assert out.stop == "over_budget"
    assert brain.calls == []


async def test_stop_request_ends_the_turn_before_the_next_brain_call() -> None:
    stop = asyncio.Event()
    stop.set()
    brain = ScriptedBrain([Message.assistant("never")])
    _, out = await run(brain, t.registry(), stop=stop)
    assert out.stop == "cancelled"
    assert brain.calls == []


async def test_an_interrupted_turn_resumes_without_asking_again() -> None:
    """Spec §6.1: interrupt mid-task, resume from where it stopped."""
    history = [*USER, calls(call("step", "pending"))]  # stored right after the brain asked
    brain = ScriptedBrain([Message.assistant("finished")])
    _, out = await run(brain, t.registry(), history)
    assert t.ran == [("step", "pending")]
    assert out.stop == "final_answer"
    assert len(brain.calls) == 1  # the earlier plan was not re-requested
