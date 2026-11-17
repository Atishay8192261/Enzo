import math

import pytest

from enzo.brain import (
    AuthError,
    BadRequestError,
    BrainError,
    BrainProvider,
    Capabilities,
    Message,
    ProviderServerError,
    ProviderTimeoutError,
    RateLimitError,
    StreamDone,
    TextDelta,
    ToolCall,
    ToolCallRejection,
    ToolSpec,
    UnsupportedCapabilityError,
    ValidatedCall,
    tool_call,
)
from enzo.brain.fake import ScriptedBrain, ScriptExhaustedError
from tests.brain.schemas import SearchWeb

SPEC = ToolSpec.from_model("search_web", "Search the web", SearchWeb)
GOOD = ToolCall(id="c1", name="search_web", arguments_json='{"query": "x"}')
BAD = ToolCall(id="c2", name="search_web", arguments_json='{"query": 5}')


def test_scripted_brain_satisfies_the_provider_protocol() -> None:
    assert isinstance(ScriptedBrain([]), BrainProvider)


async def test_replays_script_in_order_and_records_calls() -> None:
    brain = ScriptedBrain([Message.assistant("one"), Message.assistant("two")])
    first = await brain.chat([Message.user("a")], max_output_tokens=10)
    second = await brain.chat([Message.user("b")], tools=[SPEC], max_output_tokens=20)
    assert (first.text, second.text) == ("one", "two")
    assert [c.messages[0].text for c in brain.calls] == ["a", "b"]
    assert brain.calls[1].tools == (SPEC,)
    assert first.stop_reason == "end_turn"


async def test_exhausted_script_fails_loudly() -> None:
    brain = ScriptedBrain([])
    with pytest.raises(ScriptExhaustedError):
        await brain.chat([Message.user("a")], max_output_tokens=10)


async def test_scripted_errors_are_raised_with_provider_and_model() -> None:
    brain = ScriptedBrain([RateLimitError("slow down", provider="fake", model="m")])
    with pytest.raises(RateLimitError) as exc:
        await brain.chat([Message.user("a")], max_output_tokens=10)
    assert (exc.value.provider, exc.value.model, exc.value.kind) == ("fake", "m", "rate_limit")


@pytest.mark.parametrize(
    ("error", "eligible"),
    [
        (RateLimitError, True),
        (ProviderServerError, True),
        (ProviderTimeoutError, True),
        (AuthError, False),
        (BadRequestError, False),
        (UnsupportedCapabilityError, False),
    ],
)
def test_only_transient_failures_may_trigger_fallback(
    error: type[BrainError], eligible: bool
) -> None:
    assert error.fallback_eligible is eligible


async def test_stream_yields_text_then_exactly_one_done_matching_chat() -> None:
    brain = ScriptedBrain([Message.assistant("hello there world")])
    events = [e async for e in brain.stream([Message.user("hi")], max_output_tokens=10)]
    deltas = [e for e in events if isinstance(e, TextDelta)]
    done = [e for e in events if isinstance(e, StreamDone)]
    assert len(done) == 1
    assert events[-1] is done[0]
    assert " ".join(d.text for d in deltas) == done[0].response.text == "hello there world"


async def test_tool_call_forces_a_tool_and_validates_every_call() -> None:
    brain = ScriptedBrain([Message.assistant(tool_calls=(GOOD, BAD))])
    results = await tool_call(
        brain, [Message.user("find x")], {"search_web": (SPEC, SearchWeb)}, max_output_tokens=50
    )
    assert brain.calls[0].tool_choice == "required"
    assert brain.calls[0].tools == (SPEC,)
    assert isinstance(results[0], ValidatedCall)
    assert isinstance(results[1], ToolCallRejection)


async def test_tool_call_refuses_a_brain_without_tool_calling() -> None:
    class NoTools(ScriptedBrain):
        @property
        def capabilities(self) -> Capabilities:
            return Capabilities(
                tool_calling="unsupported",
                streaming=False,
                token_counting="estimate",  # noqa: S106
                embeddings=False,
            )

    brain = NoTools([])
    with pytest.raises(UnsupportedCapabilityError):
        await tool_call(brain, [Message.user("x")], {}, max_output_tokens=10)
    assert brain.calls == []


async def test_fake_embeddings_are_deterministic_unit_vectors() -> None:
    brain = ScriptedBrain([])
    a1, a2, b = await brain.embed(["alpha", "alpha", "beta"])
    assert a1 == a2
    assert a1 != b
    assert math.isclose(sum(x * x for x in a1), 1.0)
