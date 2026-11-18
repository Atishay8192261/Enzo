"""GPT adapter on the official ``openai`` SDK, using the Responses API (spec §1.3.1, §1.3.2).

Stateless by design: ``store=False``, Enzo owns the history and replays every output item, including
encrypted reasoning, in its original order (OpenAI's guidance for tool loops with reasoning models).
"""

import time
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import contextmanager
from typing import Any

import openai
from openai.types.responses import (
    Response as WireResponse,
)
from openai.types.responses import (
    ResponseCompletedEvent,
    ResponseErrorEvent,
    ResponseFailedEvent,
    ResponseIncompleteEvent,
    ResponseOutputItem,
    ResponseOutputItemDoneEvent,
    ResponseTextDeltaEvent,
)

from enzo.brain.errors import (
    AuthError,
    BadRequestError,
    BrainError,
    ProviderConnectionError,
    ProviderServerError,
    ProviderTimeoutError,
    RateLimitError,
    UnsupportedCapabilityError,
)
from enzo.brain.models import (
    BrainResponse,
    Capabilities,
    Message,
    NativePart,
    Part,
    StopReason,
    StreamDone,
    StreamEvent,
    TextDelta,
    TextPart,
    ToolCall,
    ToolChoice,
    ToolSpec,
    Usage,
)

PROVIDER = "openai"
#: A ChatGPT plan sign-in takes function tools only inside a namespace (OpenAI's SIWC docs).
NAMESPACE = "enzo"
USAGE_URL = "https://chatgpt.com/settings/usage"
#: ChatGPT plan errors, in words a user can act on (developers.openai.com/siwc, read 2026-11-18).
PLAN_LIMIT = (
    "Your ChatGPT plan's usage limit for Enzo is reached. Review or raise it at " + USAGE_URL
)
_PLAN_ERRORS: dict[str, tuple[type[BrainError], str]] = {
    "subscription_sharing_usage_limit_exceeded": (RateLimitError, PLAN_LIMIT),
    "subscription_sharing_usage_unavailable": (
        ProviderServerError,
        "ChatGPT could not check your plan's usage right now. Try again shortly.",
    ),
    "subscription_sharing_user_unavailable": (
        ProviderServerError,
        "ChatGPT could not look up your account right now. Try again shortly.",
    ),
    "subscription_sharing_user_not_eligible": (
        AuthError,
        "This ChatGPT account or workspace can't use its plan in Enzo. "
        "Use an API key instead: /login openai",
    ),
    "subscription_sharing_invalid_user": (
        AuthError,
        "ChatGPT did not accept your sign-in. Sign in again: /login chatgpt",
    ),
}


class OpenAIBrain:
    """A GPT model behind Enzo's ``BrainProvider`` interface."""

    def __init__(
        self,
        *,
        model: str,
        client: openai.AsyncOpenAI | None = None,
        reasoning_effort: str | None = None,
        embedding_model: str | None = None,
        forced_tool_choice: bool = True,
        provider: str = PROVIDER,
        plan: bool = False,
    ) -> None:
        # The SDK retries 429/5xx by default; Enzo never retries around a failure (ADR 0002).
        self._client = (client or openai.AsyncOpenAI()).with_options(max_retries=0)
        self._model = model
        self._reasoning_effort = reasoning_effort
        self._embedding_model = embedding_model
        self._forced_tool_choice = forced_tool_choice
        self._provider = provider
        #: Signed in with ChatGPT: the plan pays, and OpenAI's plan-route rules apply (ADR 0013).
        self._plan = plan

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def model(self) -> str:
        return self._model

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(
            tool_calling="native",
            forced_tool_choice=self._forced_tool_choice,
            streaming=True,
            token_counting="unsupported" if self._plan else "exact",
            embeddings=self._embedding_model is not None,
        )

    async def chat(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSpec] = (),
        tool_choice: ToolChoice = "auto",
        max_output_tokens: int,
    ) -> BrainResponse:
        done: StreamDone | None = None
        async for event in self.stream(
            messages, tools=tools, tool_choice=tool_choice, max_output_tokens=max_output_tokens
        ):
            if isinstance(event, StreamDone):
                done = event
        if done is None:
            msg = "stream ended without a final response"
            raise ProviderServerError(msg, provider=self._provider, model=self._model)
        return done.response

    async def stream(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSpec] = (),
        tool_choice: ToolChoice = "auto",
        max_output_tokens: int,
    ) -> AsyncIterator[StreamEvent]:
        request = self.build_request(messages, tools, tool_choice, max_output_tokens)
        started = time.perf_counter()
        final: WireResponse | None = None
        done_items: dict[int, ResponseOutputItem] = {}
        # Raw events, not the ``responses.stream()`` helper: the helper wraps items in "parsed"
        # types that add fields the API never sent (breaking exact replay), and its
        # get_final_response() raises on a normal max-token stop ("incomplete"). Both verified.
        with mapped_errors(self._provider, self._model, plan=self._plan):
            wire = await self._client.responses.create(**request, stream=True)
            async with wire:
                async for event in wire:
                    if isinstance(event, ResponseTextDeltaEvent):
                        yield TextDelta(text=event.delta)
                    elif isinstance(event, ResponseOutputItemDoneEvent):
                        done_items[event.output_index] = event.item
                    elif isinstance(
                        event,
                        ResponseCompletedEvent | ResponseIncompleteEvent | ResponseFailedEvent,
                    ):
                        final = event.response
                    elif isinstance(event, ResponseErrorEvent):
                        msg = f"stream error {event.code}: {event.message}"
                        raise ProviderServerError(msg, provider=self._provider, model=self._model)
        if final is None:
            msg = "stream ended without a terminal response event"
            raise ProviderServerError(msg, provider=self._provider, model=self._model)
        if not final.output and done_items:
            # The terminal event may omit output; recover the finished items, as the SDK does.
            final = final.model_copy(update={"output": [done_items[i] for i in sorted(done_items)]})
        if final.status == "failed":
            error = final.error
            known = _PLAN_ERRORS.get(str(error.code)) if self._plan and error else None
            if known is not None:  # e.g. the plan's limit, reached after streaming began
                kind, message = known
                raise kind(message, provider=self._provider, model=self._model)
            detail = f"{error.code}: {error.message}" if error else "no error details"
            msg = f"response failed ({detail})"
            raise ProviderServerError(msg, provider=self._provider, model=self._model)
        latency_ms = (time.perf_counter() - started) * 1000
        response = from_wire(final, latency_ms, self._provider)
        if self._plan:
            response = response.model_copy(update={"billing": "plan"})
        yield StreamDone(response=response)

    async def count_tokens(
        self, messages: Sequence[Message], *, tools: Sequence[ToolSpec] = ()
    ) -> int:
        if self._plan:
            msg = "a ChatGPT plan sign-in has no token-counting endpoint"
            raise UnsupportedCapabilityError(msg, provider=self._provider, model=self._model)
        kwargs: dict[str, Any] = {
            "model": self._model,
            "input": to_wire_input(messages, self._provider),
        }
        if tools:
            kwargs["tools"] = [to_wire_tool(t) for t in tools]
        with mapped_errors(self._provider, self._model):
            result = await self._client.responses.input_tokens.count(**kwargs)
        return result.input_tokens

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if self._embedding_model is None:
            msg = "no embedding model configured for this brain"
            raise UnsupportedCapabilityError(msg, provider=self._provider, model=self._model)
        with mapped_errors(self._provider, self._embedding_model):
            result = await self._client.embeddings.create(
                model=self._embedding_model, input=list(texts)
            )
        return [item.embedding for item in sorted(result.data, key=lambda d: d.index)]

    def build_request(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
        tool_choice: ToolChoice,
        max_output_tokens: int,
    ) -> dict[str, Any]:
        """Canonical request → Responses API parameters. Pure, so it is tested without a network."""
        request: dict[str, Any] = {
            "model": self._model,
            "input": to_wire_input(
                messages, self._provider, namespace=NAMESPACE if self._plan else None
            ),
            "store": False,
        }
        if not self._plan:  # the plan route rejects max_output_tokens (SIWC preview limits)
            request["max_output_tokens"] = max_output_tokens
        if tools:
            wire_tools = [to_wire_tool(t) for t in tools]
            request["tools"] = (
                [
                    {
                        "type": "namespace",
                        "name": NAMESPACE,
                        "description": "Enzo's tools",
                        "tools": wire_tools,
                    }
                ]
                if self._plan
                else wire_tools
            )
            if tool_choice == "required" and not self._forced_tool_choice:
                msg = f"{self._model} is not configured to accept a forced tool call"
                raise UnsupportedCapabilityError(msg, provider=self._provider, model=self._model)
            request["tool_choice"] = tool_choice
        elif tool_choice == "required":
            msg = "tool_choice='required' needs at least one tool"
            raise ValueError(msg)
        if self._reasoning_effort:
            request["reasoning"] = {"effort": self._reasoning_effort}
        return request


def to_wire_input(
    messages: Sequence[Message], provider: str, *, namespace: str | None = None
) -> list[dict[str, Any]]:
    """``namespace``: the tool namespace calls belong to, when tools are grouped in one."""
    items: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "system":
            # Developer messages keep their position, so mid-conversation instructions append
            # instead of rewriting the prefix (which would drop reasoning on newer models).
            items.append({"role": "developer", "content": message.text})
        elif message.role == "user":
            items.append(
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": p.text}
                        for p in message.parts
                        if isinstance(p, TextPart)
                    ],
                }
            )
        elif message.role == "assistant":
            items.extend(_assistant_items(message, provider, namespace))
        else:
            items.extend(
                {
                    "type": "function_call_output",
                    "call_id": r.call_id,
                    # The Responses API has no error flag on outputs, so say it in the text.
                    "output": f"Error: {r.content}" if r.is_error else r.content,
                }
                for r in message.parts
                if r.type == "tool_result"
            )
    return items


def _assistant_items(
    message: Message, provider: str, namespace: str | None
) -> list[dict[str, Any]]:
    native = message.native(provider)
    if native is not None:
        # Every output item, in order: reasoning (encrypted), messages with their phase, calls.
        output: list[dict[str, Any]] = native.payload["output"]
        return output
    items: list[dict[str, Any]] = []
    for part in message.parts:
        if isinstance(part, TextPart) and part.text:
            items.append(
                {"role": "assistant", "content": [{"type": "output_text", "text": part.text}]}
            )
        elif isinstance(part, ToolCall):
            call = {
                "type": "function_call",
                "call_id": part.id,
                "name": part.name,
                "arguments": part.arguments_json,
            }
            items.append(call if namespace is None else {**call, "namespace": namespace})
    return items


def to_wire_tool(spec: ToolSpec) -> dict[str, Any]:
    # strict=False: Pydantic schemas with optional fields are not strict-mode compatible, and
    # Enzo validates every call itself before running it (spec §4.2).
    return {
        "type": "function",
        "name": spec.name,
        "description": spec.description,
        "parameters": spec.parameters,
        "strict": False,
    }


def from_wire(response: WireResponse, latency_ms: float, provider: str = PROVIDER) -> BrainResponse:
    raw = [item.to_dict(mode="json") for item in response.output]
    parts: list[Part] = [NativePart(provider=provider, payload={"output": raw})]
    refused = False
    for item in response.output:
        if item.type == "message":
            for content in item.content:
                if content.type == "output_text":
                    parts.append(TextPart(text=content.text))
                elif content.type == "refusal":
                    refused = True
                    parts.append(TextPart(text=content.refusal))
        elif item.type == "function_call":
            parts.append(ToolCall(id=item.call_id, name=item.name, arguments_json=item.arguments))
    usage = _usage(response)
    return BrainResponse(
        provider=provider,
        model=response.model,
        message=Message(role="assistant", parts=tuple(parts)),
        stop_reason=_stop_reason(
            response, refused=refused, called=any(isinstance(p, ToolCall) for p in parts)
        ),
        usage=usage,
        latency_ms=latency_ms,
    )


def _usage(response: WireResponse) -> Usage:
    """OpenAI-compatible providers may omit detail fields, so every one is optional here."""
    usage = response.usage
    if usage is None:
        return Usage(input_tokens=0, output_tokens=0)
    details = getattr(usage, "input_tokens_details", None)
    cached = getattr(details, "cached_tokens", None) or 0
    writes = getattr(details, "cache_write_tokens", None) or 0
    by_ttl = getattr(details, "cache_write_tokens_details", None)
    breakdown = (
        {
            ttl: n
            for ttl, n in (
                ("30m", getattr(by_ttl, "ttl_30m_tokens", 0)),
                ("12h", getattr(by_ttl, "ttl_12h_tokens", 0)),
            )
            if n
        }
        if by_ttl
        else {}
    )
    return Usage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cached_input_tokens=cached,
        cache_write_input_tokens=writes,
        cache_write_breakdown=breakdown,
    )


def _stop_reason(response: WireResponse, *, refused: bool, called: bool) -> StopReason:
    if response.status == "incomplete":
        reason = response.incomplete_details.reason if response.incomplete_details else None
        if reason == "max_output_tokens":
            return "max_tokens"
        return "refusal" if reason == "content_filter" else "other"
    if refused:
        return "refusal"
    if called:
        return "tool_use"
    return "end_turn" if response.status == "completed" else "other"


@contextmanager
def mapped_errors(provider: str, model: str, *, plan: bool = False) -> Iterator[None]:
    """Map every SDK failure onto one canonical error. Most specific first."""
    ctx = {"provider": provider, "model": model}
    try:
        yield
    except openai.APITimeoutError as exc:  # subclass of APIConnectionError: must come first
        raise ProviderTimeoutError(str(exc), **ctx) from exc
    except openai.APIConnectionError as exc:
        raise ProviderConnectionError(str(exc), **ctx) from exc
    except openai.APIStatusError as exc:
        raise status_error(exc, ctx, plan=plan) from exc
    except openai.APIError as exc:
        raise BrainError(str(exc), **ctx) from exc


def status_error(exc: openai.APIStatusError, ctx: dict[str, str], *, plan: bool) -> BrainError:
    """An HTTP refusal as a canonical error. On a ChatGPT plan, OpenAI's plan error codes and its
    admission refusals (401/403 with only a ``detail``) get words a user can act on."""
    if plan and (worded := _plan_error(exc, ctx)) is not None:
        return worded
    if isinstance(exc, openai.RateLimitError):
        return RateLimitError(str(exc), **ctx)
    if isinstance(exc, openai.AuthenticationError | openai.PermissionDeniedError):
        return AuthError(str(exc), **ctx)
    if exc.status_code >= 500:
        return ProviderServerError(str(exc), **ctx)
    if exc.status_code == 402:  # billing problem: the account cannot use it
        return AuthError(str(exc), **ctx)
    return BadRequestError(str(exc), **ctx)


def _plan_error(exc: openai.APIStatusError, ctx: dict[str, str]) -> BrainError | None:
    known = _PLAN_ERRORS.get(str(exc.code))
    if known is not None:
        kind, message = known
        return kind(message, **ctx)
    if exc.status_code == 401:
        return AuthError(_PLAN_ERRORS["subscription_sharing_invalid_user"][1], **ctx)
    if exc.status_code == 403:
        msg = f"ChatGPT refused this request for your plan (policy or region): {exc.message}"
        return AuthError(msg, **ctx)
    return None
