"""OpenAI Responses adapter tests: the real SDK runs end to end; only HTTP is simulated."""

import json
from collections.abc import Callable
from typing import Any, cast

import httpx2
import openai
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
from enzo.brain.adapters.openai_brain import OpenAIBrain
from tests.brain.schemas import SearchWeb

MODEL = "gpt-test"
SPEC = ToolSpec.from_model("search_web", "Search the web", SearchWeb)
Handler = Callable[[httpx2.Request], httpx2.Response]

REASONING = {"id": "rs_1", "type": "reasoning", "summary": [], "encrypted_content": "enc-xyz"}
TEXT = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "status": "completed",
    "phase": "commentary",
    "content": [{"type": "output_text", "text": "Hel|lo", "annotations": []}],
}
CALL = {
    "id": "fc_1",
    "type": "function_call",
    "call_id": "call_1",
    "name": "search_web",
    "arguments": '{"query": "x"}',
    "status": "completed",
}


def client(handler: Handler) -> openai.AsyncOpenAI:
    return openai.AsyncOpenAI(
        api_key="test-key",
        http_client=openai.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)),
    )


def brain(handler: Handler, **kwargs: Any) -> OpenAIBrain:
    return OpenAIBrain(model=MODEL, client=client(handler), **kwargs)


def response(
    output: list[dict[str, Any]], status: str = "completed", **extra: Any
) -> dict[str, Any]:
    return {
        "id": "resp_1",
        "object": "response",
        "created_at": 0,
        "model": MODEL,
        "status": status,
        "output": output,
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "incomplete_details": None,
        "error": None,
        "usage": {
            "input_tokens": 400,
            "input_tokens_details": {"cached_tokens": 300},
            "output_tokens": 30,
            "output_tokens_details": {"reasoning_tokens": 10},
            "total_tokens": 430,
        },
        **extra,
    }


def finished(item: dict[str, Any]) -> dict[str, Any]:
    if item["type"] != "message" or item["content"][0]["type"] != "output_text":
        return item
    text = item["content"][0]["text"].replace("|", "")
    return {**item, "content": [{**item["content"][0], "text": text}]}


def sse(final: dict[str, Any], *, omit_final_output: bool = False) -> bytes:
    """Encode a response as the documented Responses streaming events."""
    started = {**final, "status": "in_progress", "output": []}
    events: list[dict[str, Any]] = [{"type": "response.created", "response": started}]
    for i, item in enumerate(final["output"]):
        done = finished(item)
        if item["type"] == "message" and item["content"][0]["type"] == "output_text":
            pieces = item["content"][0]["text"].split("|")
            events.append(
                {
                    "type": "response.output_item.added",
                    "output_index": i,
                    "item": {**item, "status": "in_progress", "content": []},
                }
            )
            part = {"type": "output_text", "text": "", "annotations": []}
            events.append(
                {
                    "type": "response.content_part.added",
                    "item_id": item["id"],
                    "output_index": i,
                    "content_index": 0,
                    "part": part,
                }
            )
            events += [
                {
                    "type": "response.output_text.delta",
                    "item_id": item["id"],
                    "output_index": i,
                    "content_index": 0,
                    "delta": p,
                    "logprobs": [],
                }
                for p in pieces
            ]
            events.append(
                {
                    "type": "response.output_text.done",
                    "item_id": item["id"],
                    "output_index": i,
                    "content_index": 0,
                    "text": "".join(pieces),
                    "logprobs": [],
                }
            )
            events.append(
                {
                    "type": "response.content_part.done",
                    "item_id": item["id"],
                    "output_index": i,
                    "content_index": 0,
                    "part": done["content"][0],
                }
            )
        elif item["type"] == "function_call":
            events.append(
                {
                    "type": "response.output_item.added",
                    "output_index": i,
                    "item": {**item, "arguments": "", "status": "in_progress"},
                }
            )
            events.append(
                {
                    "type": "response.function_call_arguments.delta",
                    "item_id": item["id"],
                    "output_index": i,
                    "delta": item["arguments"],
                }
            )
            events.append(
                {
                    "type": "response.function_call_arguments.done",
                    "item_id": item["id"],
                    "output_index": i,
                    "arguments": item["arguments"],
                    "name": item["name"],
                }
            )
        else:
            events.append({"type": "response.output_item.added", "output_index": i, "item": item})
        events.append({"type": "response.output_item.done", "output_index": i, "item": done})
    terminal = {
        "completed": "response.completed",
        "incomplete": "response.incomplete",
        "failed": "response.failed",
    }[final["status"]]
    last_output = [] if omit_final_output else [finished(o) for o in final["output"]]
    events.append({"type": terminal, "response": {**final, "output": last_output}})
    return "".join(
        f"event: {e['type']}\ndata: {json.dumps({**e, 'sequence_number': n})}\n\n"
        for n, e in enumerate(events)
    ).encode()


def streaming(final: dict[str, Any], seen: list[dict[str, Any]] | None = None) -> Handler:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if seen is not None:
            seen.append(json.loads(request.content))
        return httpx2.Response(
            200, headers={"content-type": "text/event-stream"}, content=sse(final)
        )

    return handler


def test_satisfies_the_provider_protocol_with_retries_disabled() -> None:
    b = brain(streaming(response([])))
    assert isinstance(b, BrainProvider)
    assert b._client.max_retries == 0


def test_request_translation() -> None:
    b = brain(streaming(response([])), reasoning_effort="high")
    request = b.build_request(
        [
            Message.system("You are Enzo."),
            Message.user("find x"),
            Message.assistant(
                "checking",
                (ToolCall(id="call_1", name="search_web", arguments_json='{"query": "x"}'),),
            ),
            Message.tool(
                ToolResult(call_id="call_1", name="search_web", content="r1"),
                ToolResult(call_id="call_2", name="search_web", content="bad", is_error=True),
            ),
            Message.system("Be brief."),
        ],
        [SPEC],
        "auto",
        1000,
    )
    assert request["input"] == [
        {"role": "developer", "content": "You are Enzo."},
        {"role": "user", "content": [{"type": "input_text", "text": "find x"}]},
        {"role": "assistant", "content": [{"type": "output_text", "text": "checking"}]},
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "search_web",
            "arguments": '{"query": "x"}',
        },
        {"type": "function_call_output", "call_id": "call_1", "output": "r1"},
        {"type": "function_call_output", "call_id": "call_2", "output": "Error: bad"},
        {"role": "developer", "content": "Be brief."},
    ]
    assert request["tools"] == [
        {
            "type": "function",
            "name": "search_web",
            "description": "Search the web",
            "parameters": SPEC.parameters,
            "strict": False,
        }
    ]
    assert (request["tool_choice"], request["store"], request["max_output_tokens"]) == (
        "auto",
        False,
        1000,
    )
    assert request["reasoning"] == {"effort": "high"}


def test_forced_tool_choice_respects_configuration() -> None:
    msgs = [Message.user("x")]
    assert (
        brain(streaming(response([]))).build_request(msgs, [SPEC], "required", 10)["tool_choice"]
        == "required"
    )
    with pytest.raises(UnsupportedCapabilityError):
        brain(streaming(response([])), forced_tool_choice=False).build_request(
            msgs, [SPEC], "required", 10
        )
    with pytest.raises(ValueError, match="at least one tool"):
        brain(streaming(response([]))).build_request(msgs, [], "required", 10)


async def test_stream_yields_text_then_a_canonical_response() -> None:
    final = response([REASONING, TEXT, CALL])
    events = [
        e async for e in brain(streaming(final)).stream([Message.user("hi")], max_output_tokens=100)
    ]
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["Hel", "lo"]
    done = events[-1]
    assert isinstance(done, StreamDone)
    r = done.response
    assert r.text == "Hello"
    assert r.tool_calls == (
        ToolCall(id="call_1", name="search_web", arguments_json='{"query": "x"}'),
    )
    assert (r.stop_reason, r.provider, r.model) == ("tool_use", "openai", MODEL)
    assert r.usage.model_dump() == {
        "input_tokens": 400,
        "output_tokens": 30,
        "cached_input_tokens": 300,
        "cache_write_input_tokens": 0,
        "cache_write_breakdown": {},
    }


async def test_replay_matches_the_sdk_sending_response_output() -> None:
    """Encrypted reasoning items and phase go back exactly, in order (OpenAI tool-loop rule)."""
    final = response([REASONING, TEXT, CALL])
    seen: list[dict[str, Any]] = []
    b = brain(streaming(final, seen))
    first = await b.chat([Message.user("hi")], max_output_tokens=100)
    result = Message.tool(ToolResult(call_id="call_1", name="search_web", content="r"))
    await b.chat([Message.user("hi"), first.message, result], max_output_tokens=100)
    replayed = seen[1]["input"][1:4]

    # Reference: the docs' pattern of sending response.output back, via the SDK's own serializer.
    sdk_seen: list[dict[str, Any]] = []

    def plain(request: httpx2.Request) -> httpx2.Response:
        sdk_seen.append(json.loads(request.content))
        body = {**final, "output": [finished(o) for o in final["output"]]}
        return httpx2.Response(200, json=body)

    raw = client(plain)
    out = (await raw.responses.create(model=MODEL, input="hi")).output
    history = [{"role": "user", "content": "hi"}, *out]
    await raw.responses.create(model=MODEL, input=cast("Any", history))
    assert replayed == sdk_seen[1]["input"][1:]
    assert all("parsed" not in str(item) for item in replayed)  # no helper-only fields
    assert replayed[0]["encrypted_content"] == "enc-xyz"
    assert replayed[1]["phase"] == "commentary"
    assert seen[1]["input"][4] == {
        "type": "function_call_output",
        "call_id": "call_1",
        "output": "r",
    }


async def test_max_token_stop_is_a_normal_result_not_a_crash() -> None:
    final = response([TEXT], "incomplete", incomplete_details={"reason": "max_output_tokens"})
    # The SDK's stream helper cannot handle this case (verified); the adapter reads raw events.
    async with client(streaming(final)).responses.stream(model=MODEL, input="hi") as s:
        with pytest.raises(RuntimeError, match=r"response\.completed"):
            await s.get_final_response()
    r = await brain(streaming(final)).chat([Message.user("hi")], max_output_tokens=5)
    assert (r.stop_reason, r.text) == ("max_tokens", "Hello")


async def test_refusals_are_reported_as_refusals() -> None:
    refusal = {**TEXT, "content": [{"type": "refusal", "refusal": "I can't help with that."}]}
    r = await brain(streaming(response([refusal]))).chat([Message.user("x")], max_output_tokens=5)
    assert (r.stop_reason, r.text) == ("refusal", "I can't help with that.")
    filtered = response([TEXT], "incomplete", incomplete_details={"reason": "content_filter"})
    r2 = await brain(streaming(filtered)).chat([Message.user("x")], max_output_tokens=5)
    assert r2.stop_reason == "refusal"


async def test_failed_response_raises_with_the_provider_reason() -> None:
    failed = response([], "failed", error={"code": "server_error", "message": "boom"})
    with pytest.raises(ProviderServerError, match="server_error: boom"):
        await brain(streaming(failed)).chat([Message.user("x")], max_output_tokens=5)


async def test_native_payload_from_another_provider_is_ignored() -> None:
    foreign = Message(
        role="assistant",
        parts=(
            NativePart(provider="anthropic", payload={"content": []}),
            ToolCall(id="c", name="search_web", arguments_json="{}"),
        ),
    )
    request = brain(streaming(response([]))).build_request(
        [Message.user("x"), foreign], [SPEC], "auto", 10
    )
    assert request["input"][1] == {
        "type": "function_call",
        "call_id": "c",
        "name": "search_web",
        "arguments": "{}",
    }


def error(status: int, code: str) -> Handler:
    calls: list[int] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(1)
        handler.calls = len(calls)  # type: ignore[attr-defined]
        return httpx2.Response(
            status, json={"error": {"message": "boom", "type": code, "code": code}}
        )

    return handler


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (429, RateLimitError),
        (500, ProviderServerError),
        (503, ProviderServerError),
        (401, AuthError),
        (403, AuthError),
        (402, AuthError),
        (400, BadRequestError),
        (404, BadRequestError),
    ],
)
async def test_http_errors_map_to_canonical_errors_with_one_attempt(
    status: int, expected: type[BrainError]
) -> None:
    handler = error(status, "some_error")
    with pytest.raises(expected) as exc:
        await brain(handler).chat([Message.user("x")], max_output_tokens=10)
    assert (exc.value.provider, exc.value.model) == ("openai", MODEL)
    assert handler.calls == 1  # type: ignore[attr-defined]


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


async def test_count_tokens_uses_the_input_token_endpoint() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"object": "response.input_tokens", "input_tokens": 42})

    assert await brain(handler).count_tokens([Message.user("hi")], tools=[SPEC]) == 42
    assert seen[0].url.path == "/v1/responses/input_tokens"
    assert json.loads(seen[0].content)["tools"][0]["name"] == "search_web"


async def test_embeddings_come_back_in_input_order() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert json.loads(request.content)["model"] == "embed-test"
        return httpx2.Response(
            200,
            json={
                "object": "list",
                "model": "embed-test",
                "data": [
                    {"object": "embedding", "index": 1, "embedding": [0.0, 1.0]},
                    {"object": "embedding", "index": 0, "embedding": [1.0, 0.0]},
                ],
                "usage": {"prompt_tokens": 2, "total_tokens": 2},
            },
        )

    b = brain(handler, embedding_model="embed-test")
    assert b.capabilities.embeddings is True
    assert await b.embed(["a", "b"]) == [[1.0, 0.0], [0.0, 1.0]]


async def test_embeddings_without_a_configured_model_are_refused() -> None:
    b = brain(streaming(response([])))
    assert b.capabilities.embeddings is False
    with pytest.raises(UnsupportedCapabilityError):
        await b.embed(["a"])


async def test_items_are_recovered_when_the_final_event_omits_output() -> None:
    final = response([REASONING, TEXT, CALL])

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = sse(final, omit_final_output=True)
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    r = await brain(handler).chat([Message.user("hi")], max_output_tokens=10)
    assert r.text == "Hello"
    assert [c.id for c in r.tool_calls] == ["call_1"]
    native = r.message.native("openai")
    assert native is not None
    assert [i["id"] for i in native.payload["output"]] == ["rs_1", "msg_1", "fc_1"]


async def test_cache_writes_are_counted_with_their_retention() -> None:
    """Regression: cache writes are billed separately and were once dropped as 0."""
    final = response([TEXT])
    final["usage"]["input_tokens_details"] = {
        "cached_tokens": 300,
        "cache_write_tokens": 50,
        "cache_write_tokens_details": {"ttl_30m_tokens": 40, "ttl_12h_tokens": 10},
    }
    r = await brain(streaming(final)).chat([Message.user("x")], max_output_tokens=10)
    assert r.usage.cache_write_input_tokens == 50
    assert r.usage.cache_write_breakdown == {"30m": 40, "12h": 10}


# ---------- Signed in with ChatGPT: the plan route's rules (ADR 0013, SIWC limits) ----------


def plan_brain(handler: Handler, tokens: list[str] | None = None) -> OpenAIBrain:
    """A plan-mode brain whose token source hands out the next token on each request."""
    issued = iter(tokens or ["tok-1", "tok-2", "tok-3"])

    async def access_token() -> str:
        return next(issued)

    http = openai.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler))
    return OpenAIBrain(
        model=MODEL,
        client=openai.AsyncOpenAI(api_key=access_token, http_client=http),
        plan=True,
    )


def test_plan_requests_follow_the_plan_route_rules() -> None:
    foreign = Message(
        role="assistant",
        parts=(ToolCall(id="c", name="search_web", arguments_json="{}"),),
    )
    request = plan_brain(streaming(response([]))).build_request(
        [Message.system("be brief"), Message.user("x"), foreign], [SPEC], "auto", 10
    )
    assert "max_output_tokens" not in request  # rejected on this route
    assert request["store"] is False
    assert request["input"][0] == {"role": "developer", "content": "be brief"}  # never "system"
    (namespace,) = request["tools"]  # function tools only inside a namespace
    assert (namespace["type"], namespace["name"]) == ("namespace", "enzo")
    assert [t["name"] for t in namespace["tools"]] == ["search_web"]
    assert request["input"][2]["namespace"] == "enzo"  # replayed calls name their namespace
    # An API key keeps the plain request.
    plain = brain(streaming(response([]))).build_request([Message.user("x")], [SPEC], "auto", 10)
    assert plain["max_output_tokens"] == 10
    assert plain["tools"][0]["type"] == "function"


async def test_each_plan_request_asks_for_a_fresh_token_and_is_paid_by_the_plan() -> None:
    seen: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.headers["authorization"])
        body = json.loads(request.content)
        assert body["stream"] is True
        assert body["store"] is False
        return streaming(response([TEXT]))(request)

    b = plan_brain(handler)
    assert b._client.max_retries == 0
    first = await b.chat([Message.user("x")], max_output_tokens=10)
    await b.chat([Message.user("y")], max_output_tokens=10)
    assert seen == ["Bearer tok-1", "Bearer tok-2"]  # a renewed token is used at once
    assert first.billing == "plan"
    assert (
        await brain(streaming(response([TEXT]))).chat([Message.user("x")], max_output_tokens=5)
    ).billing == "usage"
    # Known before any reply: a call that fails is still the plan's (#43).
    assert (b.billing, brain(streaming(response([]))).billing) == ("plan", "usage")


@pytest.mark.parametrize(
    ("status", "code", "expected", "words"),
    [
        (
            429,
            "subscription_sharing_usage_limit_exceeded",
            RateLimitError,
            "chatgpt.com/settings/usage",
        ),
        (403, "subscription_sharing_user_not_eligible", AuthError, "/login openai"),
        (401, "subscription_sharing_invalid_user", AuthError, "/login chatgpt"),
        (503, "subscription_sharing_usage_unavailable", ProviderServerError, "Try again shortly"),
    ],
)
async def test_plan_errors_say_what_to_do(
    status: int, code: str, expected: type[BrainError], words: str
) -> None:
    with pytest.raises(expected) as exc:
        await plan_brain(error(status, code)).chat([Message.user("x")], max_output_tokens=10)
    assert words in str(exc.value)


async def test_a_plan_admission_refusal_without_a_code_is_explained() -> None:
    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(401, json={"detail": "signed identity not accepted"})

    with pytest.raises(AuthError, match="Sign in again: /login chatgpt"):
        await plan_brain(handler).chat([Message.user("x")], max_output_tokens=10)


async def test_a_plan_limit_reached_mid_stream_is_a_rate_limit() -> None:
    failed = response(
        [], "failed", error={"code": "subscription_sharing_usage_limit_exceeded", "message": "x"}
    )
    with pytest.raises(RateLimitError, match="usage limit for Enzo is reached"):
        await plan_brain(streaming(failed)).chat([Message.user("x")], max_output_tokens=5)


async def test_a_plan_has_no_token_counting() -> None:
    b = plan_brain(streaming(response([])))
    assert b.capabilities.token_counting == "unsupported"
    with pytest.raises(UnsupportedCapabilityError):
        await b.count_tokens([Message.user("x")])


async def test_a_failing_token_source_stops_the_request() -> None:
    async def ended() -> str:
        raise AuthError("Your ChatGPT sign-in has ended.", provider="openai", model="")

    calls: list[int] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(1)
        return streaming(response([TEXT]))(request)

    http = openai.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler))
    b = OpenAIBrain(
        model=MODEL, client=openai.AsyncOpenAI(api_key=ended, http_client=http), plan=True
    )
    with pytest.raises(AuthError, match="sign-in has ended"):
        await b.chat([Message.user("x")], max_output_tokens=5)
    assert calls == []  # nothing was sent without a token
