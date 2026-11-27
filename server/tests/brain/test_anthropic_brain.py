"""Anthropic adapter tests. The real SDK runs end to end; only the HTTP transport is simulated."""

import json
from collections.abc import Callable
from typing import Any

import anthropic
import httpx2
import pytest

from enzo.brain import (
    AuthError,
    BadRequestError,
    BrainError,
    BrainProvider,
    Message,
    NativePart,
    ProviderConnectionError,
    ProviderServerError,
    ProviderTimeoutError,
    RateLimitError,
    StreamDone,
    TextDelta,
    ToolCall,
    ToolResult,
    ToolSpec,
    UnsupportedCapabilityError,
)
from enzo.brain.adapters.anthropic_brain import AnthropicBrain, from_wire
from enzo.brain.models import ImagePart, TextPart
from tests.brain.schemas import SearchWeb

MODEL = "claude-opus-5-5"
SPEC = ToolSpec.from_model("search_web", "Search the web", SearchWeb)
Handler = Callable[[httpx2.Request], httpx2.Response]


def brain(handler: Handler, **kwargs: Any) -> AnthropicBrain:
    transport = httpx2.MockTransport(handler)
    client = anthropic.AsyncAnthropic(
        api_key="test-key", http_client=anthropic.DefaultAsyncHttpxClient(transport=transport)
    )
    return AnthropicBrain(model=MODEL, client=client, **kwargs)


def wire_message(content: list[dict[str, Any]], stop_reason: str = "end_turn") -> dict[str, Any]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": MODEL,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_input_tokens": 300,
            "cache_creation_input_tokens": 50,
        },
    }


def sse(message: dict[str, Any]) -> bytes:
    """Encode a final message as the documented Messages streaming events."""
    events: list[dict[str, Any]] = [
        {
            "type": "message_start",
            "message": {
                **message,
                "content": [],
                "stop_reason": None,
                "usage": {**message["usage"], "output_tokens": 1},
            },
        }
    ]
    for i, block in enumerate(message["content"]):
        start: dict[str, Any]
        deltas: list[dict[str, Any]]
        if block["type"] == "text":
            start, deltas = (
                {"type": "text", "text": ""},
                [{"type": "text_delta", "text": w} for w in block["text"].split("|")],
            )
        elif block["type"] == "tool_use":
            start = {**block, "input": {}}
            deltas = [{"type": "input_json_delta", "partial_json": json.dumps(block["input"])}]
        else:
            start = {"type": "thinking", "thinking": "", "signature": ""}
            deltas = [
                {"type": "thinking_delta", "thinking": block["thinking"]},
                {"type": "signature_delta", "signature": block["signature"]},
            ]
        events.append({"type": "content_block_start", "index": i, "content_block": start})
        events += [{"type": "content_block_delta", "index": i, "delta": d} for d in deltas]
        events.append({"type": "content_block_stop", "index": i})
    events.append(
        {
            "type": "message_delta",
            "delta": {"stop_reason": message["stop_reason"], "stop_sequence": None},
            "usage": {"output_tokens": message["usage"]["output_tokens"]},
        }
    )
    events.append({"type": "message_stop"})
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def streaming(message: dict[str, Any], seen: list[dict[str, Any]] | None = None) -> Handler:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if seen is not None:
            seen.append(json.loads(request.content))
        return httpx2.Response(
            200, headers={"content-type": "text/event-stream"}, content=sse(message)
        )

    return handler


THINKING = {"type": "thinking", "thinking": "plan", "signature": "sig-abc"}
TOOL_USE = {"type": "tool_use", "id": "toolu_1", "name": "search_web", "input": {"query": "x"}}


def test_satisfies_the_provider_protocol() -> None:
    assert isinstance(brain(streaming(wire_message([]))), BrainProvider)


def test_sdk_retries_are_disabled() -> None:
    assert brain(streaming(wire_message([])))._client.max_retries == 0


def test_request_translation() -> None:
    b = brain(streaming(wire_message([])), effort="high")
    request = b.build_request(
        [
            Message.system("You are Enzo."),
            Message.user("find x"),
            Message.assistant(
                "checking", (ToolCall(id="t1", name="search_web", arguments_json='{"query": "x"}'),)
            ),
            Message.tool(
                ToolResult(call_id="t1", name="search_web", content="r1"),
                ToolResult(call_id="t2", name="search_web", content="bad", is_error=True),
            ),
            Message.system("Be brief."),
        ],
        [SPEC],
        "auto",
        1000,
    )
    assert request["system"] == "You are Enzo."
    assert request["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "find x"}]},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "checking"},
                {"type": "tool_use", "id": "t1", "name": "search_web", "input": {"query": "x"}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "r1", "is_error": False},
                {"type": "tool_result", "tool_use_id": "t2", "content": "bad", "is_error": True},
            ],
        },
        {"role": "system", "content": "Be brief."},
    ]
    assert request["tools"] == [
        {"name": "search_web", "description": "Search the web", "input_schema": SPEC.parameters}
    ]
    assert "tool_choice" not in request
    assert request["output_config"] == {"effort": "high"}
    assert request["cache_control"] == {"type": "ephemeral"}
    assert request["max_tokens"] == 1000


def test_malformed_arguments_from_another_brain_stay_visible() -> None:
    bad = ToolCall(id="t1", name="search_web", arguments_json='{"query": ')
    request = brain(streaming(wire_message([]))).build_request(
        [Message.user("x"), Message.assistant(tool_calls=(bad,))], [SPEC], "auto", 10
    )
    assert request["messages"][1]["content"][0]["input"] == {"invalid_arguments_json": '{"query": '}


def test_forced_tool_choice_is_refused_unless_configured() -> None:
    msgs = [Message.user("x")]
    with pytest.raises(UnsupportedCapabilityError):
        brain(streaming(wire_message([]))).build_request(msgs, [SPEC], "required", 10)
    forced = brain(streaming(wire_message([])), forced_tool_choice=True)
    assert forced.build_request(msgs, [SPEC], "required", 10)["tool_choice"] == {"type": "any"}
    assert forced.build_request(msgs, [SPEC], "none", 10)["tool_choice"] == {"type": "none"}


async def test_stream_yields_text_then_a_canonical_response() -> None:
    message = wire_message([THINKING, {"type": "text", "text": "Hel|lo"}, TOOL_USE], "tool_use")
    events = [
        e
        async for e in brain(streaming(message)).stream([Message.user("hi")], max_output_tokens=100)
    ]
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["Hel", "lo"]
    done = events[-1]
    assert isinstance(done, StreamDone)
    response = done.response
    assert response.text == "Hello"
    assert response.tool_calls == (
        ToolCall(id="toolu_1", name="search_web", arguments_json='{"query": "x"}'),
    )
    assert response.stop_reason == "tool_use"
    assert (response.provider, response.model) == ("anthropic", MODEL)
    assert response.usage.model_dump() == {
        "input_tokens": 450,  # 100 uncached + 300 cache read + 50 cache write
        "output_tokens": 20,
        "cached_input_tokens": 300,
        "cache_write_input_tokens": 50,
        "cache_write_breakdown": {},
    }


async def test_replay_is_byte_identical_to_what_the_sdk_sends() -> None:
    """Thinking blocks must go back unchanged. Compare with the SDK sending response.content."""
    message = wire_message([THINKING, {"type": "text", "text": "ok"}, TOOL_USE], "tool_use")
    seen: list[dict[str, Any]] = []
    b = brain(streaming(message, seen))
    first = await b.chat([Message.user("hi")], max_output_tokens=100)
    result = Message.tool(ToolResult(call_id="toolu_1", name="search_web", content="r"))
    await b.chat([Message.user("hi"), first.message, result], max_output_tokens=100)
    replayed = seen[1]["messages"][1]["content"]

    sdk_seen: list[dict[str, Any]] = []
    raw = anthropic.AsyncAnthropic(
        api_key="k",
        http_client=anthropic.DefaultAsyncHttpxClient(
            transport=httpx2.MockTransport(streaming(message, sdk_seen))
        ),
    )
    async with raw.messages.stream(
        model=MODEL, max_tokens=10, messages=[{"role": "user", "content": "hi"}]
    ) as s:
        final = await s.get_final_message()
    async with raw.messages.stream(
        model=MODEL,
        max_tokens=10,
        messages=[
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": final.content},
        ],
    ) as s:
        await s.get_final_message()
    assert replayed == sdk_seen[1]["messages"][1]["content"]
    assert replayed[0] == {"type": "thinking", "thinking": "plan", "signature": "sig-abc"}


async def test_native_payload_is_only_replayed_to_anthropic() -> None:
    foreign = Message(
        role="assistant",
        parts=(
            NativePart(provider="openai", payload={"x": 1}),
            ToolCall(id="c", name="search_web", arguments_json="{}"),
        ),
    )
    request = brain(streaming(wire_message([]))).build_request(
        [Message.user("x"), foreign], [SPEC], "auto", 10
    )
    assert request["messages"][1]["content"] == [
        {"type": "tool_use", "id": "c", "name": "search_web", "input": {}}
    ]


@pytest.mark.parametrize(
    ("stop", "expected"),
    [
        ("end_turn", "end_turn"),
        ("stop_sequence", "end_turn"),
        ("max_tokens", "max_tokens"),
        ("refusal", "refusal"),
        ("pause_turn", "other"),
    ],
)
async def test_stop_reasons_map_to_canonical_values(stop: str, expected: str) -> None:
    response = await brain(streaming(wire_message([{"type": "text", "text": "x"}], stop))).chat(
        [Message.user("x")], max_output_tokens=10
    )
    assert response.stop_reason == expected


def error(status: int, kind: str) -> Handler:
    calls: list[int] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(1)
        handler.calls = len(calls)  # type: ignore[attr-defined]
        return httpx2.Response(
            status, json={"type": "error", "error": {"type": kind, "message": "boom"}}
        )

    return handler


@pytest.mark.parametrize(
    ("status", "kind", "expected"),
    [
        (429, "rate_limit_error", RateLimitError),
        (500, "api_error", ProviderServerError),
        (529, "overloaded_error", ProviderServerError),
        (401, "authentication_error", AuthError),
        (402, "billing_error", AuthError),
        (403, "permission_error", AuthError),
        (400, "invalid_request_error", BadRequestError),
        (404, "not_found_error", BadRequestError),
    ],
)
async def test_http_errors_map_to_canonical_errors_with_one_attempt(
    status: int, kind: str, expected: type[BrainError]
) -> None:
    handler = error(status, kind)
    with pytest.raises(expected) as exc:
        await brain(handler).chat([Message.user("x")], max_output_tokens=10)
    assert (exc.value.provider, exc.value.model) == ("anthropic", MODEL)
    assert handler.calls == 1  # type: ignore[attr-defined]  # never retried behind Enzo's back


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (httpx2.ReadTimeout("slow"), ProviderTimeoutError),
        (httpx2.ConnectError("down"), ProviderConnectionError),
    ],
)
async def test_network_failures_map_to_canonical_errors(
    raised: Exception, expected: type[BrainError]
) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise raised

    with pytest.raises(expected):
        await brain(handler).chat([Message.user("x")], max_output_tokens=10)


async def test_count_tokens_uses_the_counting_endpoint() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"input_tokens": 42})

    count = await brain(handler).count_tokens(
        [Message.system("s"), Message.user("hi")], tools=[SPEC]
    )
    assert count == 42
    assert seen[0].url.path == "/v1/messages/count_tokens"
    body = json.loads(seen[0].content)
    assert (body["system"], body["tools"][0]["name"]) == ("s", "search_web")


async def test_embeddings_are_refused_honestly() -> None:
    b = brain(streaming(wire_message([])))
    assert b.capabilities.embeddings is False
    with pytest.raises(UnsupportedCapabilityError):
        await b.embed(["x"])


async def test_explicit_nulls_survive_replay_exactly_like_the_sdk() -> None:
    """Non-streamed replies carry explicit nulls (citations); replay keeps them, as the SDK does."""
    wire = anthropic.types.Message.model_validate(
        wire_message([{"type": "text", "text": "hi", "citations": None}, TOOL_USE], "tool_use")
    )
    payload = from_wire(wire, latency_ms=1.0).message.native("anthropic")
    assert payload is not None

    sdk_seen: list[dict[str, Any]] = []

    def capture(request: httpx2.Request) -> httpx2.Response:
        sdk_seen.append(json.loads(request.content))
        return httpx2.Response(200, json=wire_message([{"type": "text", "text": "ok"}]))

    raw = anthropic.AsyncAnthropic(
        api_key="k",
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(capture)),
    )
    await raw.messages.create(
        model=MODEL,
        max_tokens=10,
        messages=[{"role": "user", "content": "x"}, {"role": "assistant", "content": wire.content}],
    )
    assert payload.payload["content"] == sdk_seen[0]["messages"][1]["content"]
    assert payload.payload["content"][0] == {"type": "text", "text": "hi", "citations": None}


async def test_cache_writes_are_broken_down_by_retention() -> None:
    message = wire_message([{"type": "text", "text": "x"}])
    message["usage"]["cache_creation"] = {
        "ephemeral_5m_input_tokens": 30,
        "ephemeral_1h_input_tokens": 20,
    }
    r = await brain(streaming(message)).chat([Message.user("x")], max_output_tokens=10)
    assert r.usage.cache_write_breakdown == {"5m": 30, "1h": 20}


def test_images_go_as_base64_blocks_in_user_messages_and_tool_results() -> None:
    pic = ImagePart(id="img_0123456789abcdef", media_type="image/png", width=4, height=3)
    sent = pic.model_copy(update={"data": "QUJD"})
    request = brain(streaming(wire_message([]))).build_request(
        [
            Message(
                role="user", parts=(TextPart(text="what is it?"), sent, TextPart(text="[ctx]"))
            ),
            Message.assistant(None, (ToolCall(id="t1", name="shot", arguments_json="{}"),)),
            Message.tool(ToolResult(call_id="t1", name="shot", content="{}", images=(sent,))),
        ],
        [],
        "auto",
        100,
    )
    block = {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"},
    }
    assert request["messages"][0]["content"] == [
        {"type": "text", "text": "what is it?"},
        block,
        {"type": "text", "text": "[ctx]"},
    ]
    assert request["messages"][2]["content"][0]["content"] == [
        {"type": "text", "text": "{}"},
        block,
    ]
    with pytest.raises(ValueError, match="not attached"):  # never sent without its bytes
        brain(streaming(wire_message([]))).build_request(
            [Message(role="user", parts=(TextPart(text="x"), pic))], [], "auto", 100
        )
