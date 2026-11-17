import pytest
from pydantic import BaseModel, ValidationError

from enzo.brain import (
    BrainResponse,
    Message,
    NativePart,
    TextPart,
    ToolCall,
    ToolResult,
    ToolSpec,
    Usage,
)
from tests.brain.schemas import SearchWeb

CALL = ToolCall(id="c1", name="search_web", arguments_json='{"query": "x"}')


@pytest.mark.parametrize(
    ("role", "part"),
    [
        ("user", CALL),
        ("system", CALL),
        ("tool", TextPart(text="hi")),
        ("assistant", ToolResult(call_id="c1", name="search_web", content="r")),
    ],
)
def test_message_rejects_parts_its_role_cannot_hold(role: str, part: object) -> None:
    with pytest.raises(ValidationError, match="cannot contain"):
        Message(role=role, parts=(part,))


def test_empty_assistant_message_is_invalid() -> None:
    with pytest.raises(ValidationError):
        Message.assistant()


def test_messages_are_immutable() -> None:
    msg = Message.user("hi")
    with pytest.raises(ValidationError):
        msg.role = "assistant"  # type: ignore[misc]


def test_message_json_round_trip_keeps_part_types() -> None:
    msg = Message.assistant("let me check", (CALL,))
    assert Message.model_validate_json(msg.model_dump_json()) == msg
    assert msg.text == "let me check"
    assert msg.tool_calls == (CALL,)


def test_tool_spec_schema_comes_from_the_model() -> None:
    spec = ToolSpec.from_model("search_web", "Search the web", SearchWeb)
    assert spec.parameters["additionalProperties"] is False
    assert spec.parameters["required"] == ["query"]
    assert spec.parameters["properties"]["max_results"]["maximum"] == 20


def test_tool_spec_refuses_models_that_accept_unknown_fields() -> None:
    class Loose(BaseModel):
        q: str

    with pytest.raises(TypeError, match="extra='forbid'"):
        ToolSpec.from_model("loose", "d", Loose)


@pytest.mark.parametrize("name", ["search web", "a" * 65, "", "tool.call"])
def test_tool_spec_names_follow_the_strictest_provider_rule(name: str) -> None:
    with pytest.raises(ValidationError):
        ToolSpec(name=name, description="d", parameters={})


def test_brain_response_must_come_from_the_assistant() -> None:
    with pytest.raises(ValidationError, match="assistant"):
        BrainResponse(
            provider="p",
            model="m",
            message=Message.user("hi"),
            stop_reason="end_turn",
            usage=Usage(input_tokens=1, output_tokens=1),
            latency_ms=1,
        )


def test_native_part_is_replay_data_not_content() -> None:
    native = NativePart(provider="anthropic", payload={"content": [{"type": "thinking"}]})
    msg = Message(role="assistant", parts=(native, TextPart(text="hi"), CALL))
    assert msg.native("anthropic") == native
    assert msg.native("openai") is None
    assert (msg.text, msg.tool_calls) == ("hi", (CALL,))
    assert Message.model_validate_json(msg.model_dump_json()) == msg


def test_only_assistant_messages_carry_native_parts() -> None:
    with pytest.raises(ValidationError, match="cannot contain"):
        Message(role="user", parts=(NativePart(provider="x", payload={}),))


def test_cache_tokens_cannot_exceed_the_prompt() -> None:
    Usage(input_tokens=10, output_tokens=1, cached_input_tokens=6, cache_write_input_tokens=4)
    with pytest.raises(ValidationError, match="subsets"):
        Usage(input_tokens=10, output_tokens=1, cached_input_tokens=8, cache_write_input_tokens=4)
