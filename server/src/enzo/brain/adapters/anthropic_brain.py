"""Claude adapter on the official ``anthropic`` SDK (spec §1.3.1, §1.3.2)."""

import json
import time
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import contextmanager
from typing import Any, Literal

import anthropic
from anthropic.types import Message as WireMessage

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
    ImagePart,
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

PROVIDER = "anthropic"

type Effort = Literal["low", "medium", "high", "xhigh", "max"]

_STOP_REASONS: dict[str, StopReason] = {
    "end_turn": "end_turn",
    "stop_sequence": "end_turn",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "refusal": "refusal",
}


class AnthropicBrain:
    """A Claude model behind Enzo's ``BrainProvider`` interface."""

    def __init__(
        self,
        *,
        model: str,
        client: anthropic.AsyncAnthropic | None = None,
        effort: Effort | None = None,
        forced_tool_choice: bool = False,
        prompt_caching: bool = True,
    ) -> None:
        # The SDK retries 429/5xx twice by default. Enzo never retries around a failure; the router
        # decides on one recorded fallback instead (spec §1.3.4, §8.3).
        self._client = (client or anthropic.AsyncAnthropic()).with_options(max_retries=0)
        self._model = model
        self._effort = effort
        self._forced_tool_choice = forced_tool_choice
        self._prompt_caching = prompt_caching

    @property
    def provider(self) -> str:
        return PROVIDER

    @property
    def model(self) -> str:
        return self._model

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(
            tool_calling="native",
            forced_tool_choice=self._forced_tool_choice,
            streaming=True,
            token_counting="exact",  # noqa: S106 - a capability label, not a secret
            embeddings=False,
        )

    async def chat(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSpec] = (),
        tool_choice: ToolChoice = "auto",
        max_output_tokens: int,
    ) -> BrainResponse:
        # Streaming under the hood avoids HTTP timeouts on long generations.
        done: StreamDone | None = None
        async for event in self.stream(
            messages, tools=tools, tool_choice=tool_choice, max_output_tokens=max_output_tokens
        ):
            if isinstance(event, StreamDone):
                done = event
        if done is None:
            msg = "stream ended without a final message"
            raise ProviderServerError(msg, provider=PROVIDER, model=self._model)
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
        with mapped_errors(self._model):
            async with self._client.messages.stream(**request) as wire:
                async for event in wire:
                    if event.type == "text":
                        yield TextDelta(text=event.text)
                final = await wire.get_final_message()
        latency_ms = (time.perf_counter() - started) * 1000
        yield StreamDone(response=from_wire(final, latency_ms))

    async def count_tokens(
        self, messages: Sequence[Message], *, tools: Sequence[ToolSpec] = ()
    ) -> int:
        system, wire_messages = to_wire_messages(messages)
        kwargs: dict[str, Any] = {"model": self._model, "messages": wire_messages}
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = [to_wire_tool(t) for t in tools]
        with mapped_errors(self._model):
            result = await self._client.messages.count_tokens(**kwargs)
        return result.input_tokens

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        msg = "Anthropic has no embeddings API; configure a separate embedding provider"
        raise UnsupportedCapabilityError(msg, provider=PROVIDER, model=self._model)

    def build_request(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
        tool_choice: ToolChoice,
        max_output_tokens: int,
    ) -> dict[str, Any]:
        """Canonical request → Messages API parameters. Pure, so it is tested without a network."""
        system, wire_messages = to_wire_messages(messages)
        request: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_output_tokens,
            "messages": wire_messages,
        }
        if system:
            request["system"] = system
        if tools:
            request["tools"] = [to_wire_tool(t) for t in tools]
        if wire_choice := self._wire_tool_choice(tool_choice, has_tools=bool(tools)):
            request["tool_choice"] = wire_choice
        if self._effort:
            request["output_config"] = {"effort": self._effort}
        if self._prompt_caching:
            request["cache_control"] = {"type": "ephemeral"}
        return request

    def _wire_tool_choice(self, choice: ToolChoice, *, has_tools: bool) -> dict[str, str] | None:
        if not has_tools:
            if choice == "required":
                msg = "tool_choice='required' needs at least one tool"
                raise ValueError(msg)
            return None
        if choice == "none":
            return {"type": "none"}
        if choice == "required":
            if not self._forced_tool_choice:
                msg = f"{self._model} is not configured to accept a forced tool call"
                raise UnsupportedCapabilityError(msg, provider=PROVIDER, model=self._model)
            return {"type": "any"}
        return None  # "auto" is the API default


def to_wire_messages(messages: Sequence[Message]) -> tuple[str, list[dict[str, Any]]]:
    """Leading system messages become the top-level ``system``; later ones stay in place."""
    system: list[str] = []
    wire: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "system":
            if wire:
                wire.append({"role": "system", "content": message.text})
            else:
                system.append(message.text)
        elif message.role == "user":
            wire.append({"role": "user", "content": _user_blocks(message)})
        elif message.role == "assistant":
            wire.append({"role": "assistant", "content": _assistant_blocks(message)})
        else:
            # All results of one turn go back in a single user message, which keeps parallel
            # tool calling working.
            wire.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": r.call_id,
                            # A result that shows an image (a screenshot) is a list of blocks.
                            "content": (
                                [{"type": "text", "text": r.content}, *map(_image_block, r.images)]
                                if r.images
                                else r.content
                            ),
                            "is_error": r.is_error,
                        }
                        for r in message.parts
                        if r.type == "tool_result"
                    ],
                }
            )
    return "\n\n".join(system), wire


def _user_blocks(message: Message) -> list[dict[str, Any]]:
    """Text and images, in their order: the user's words, their images, then the context."""
    blocks: list[dict[str, Any]] = []
    for part in message.parts:
        if isinstance(part, TextPart):
            blocks.append({"type": "text", "text": part.text})
        elif isinstance(part, ImagePart):
            blocks.append(_image_block(part))
    return blocks


def _image_block(image: ImagePart) -> dict[str, Any]:
    if image.data is None:  # the router fills in each brain's copy just before the call
        msg = f"image {image.id} was not attached before the call"
        raise ValueError(msg)
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": image.media_type, "data": image.data},
    }


def _assistant_blocks(message: Message) -> list[dict[str, Any]]:
    native = message.native(PROVIDER)
    if native is not None:
        # Replay exactly what Claude sent, thinking blocks included, so history is unedited.
        blocks: list[dict[str, Any]] = native.payload["content"]
        return blocks
    out: list[dict[str, Any]] = []
    for part in message.parts:
        if isinstance(part, TextPart) and part.text:
            out.append({"type": "text", "text": part.text})
        elif isinstance(part, ToolCall):
            out.append(
                {"type": "tool_use", "id": part.id, "name": part.name, "input": _input(part)}
            )
    return out


def _input(call: ToolCall) -> dict[str, Any]:
    """Tool input must be an object. Malformed JSON from another brain is kept visible, not lost."""
    try:
        parsed = json.loads(call.arguments_json)
    except json.JSONDecodeError:
        return {"invalid_arguments_json": call.arguments_json}
    return parsed if isinstance(parsed, dict) else {"invalid_arguments_json": call.arguments_json}


def to_wire_tool(spec: ToolSpec) -> dict[str, Any]:
    return {"name": spec.name, "description": spec.description, "input_schema": spec.parameters}


def from_wire(message: WireMessage, latency_ms: float) -> BrainResponse:
    """Messages API response → canonical response. ``model`` is the model that actually served."""
    # exclude_unset reproduces exactly what the SDK sends when given response.content (verified).
    raw = [block.model_dump(mode="json", exclude_unset=True) for block in message.content]
    parts: list[Part] = [NativePart(provider=PROVIDER, payload={"content": raw})]
    for block in message.content:
        if block.type == "text":
            parts.append(TextPart(text=block.text))
        elif block.type == "tool_use":
            parts.append(
                ToolCall(id=block.id, name=block.name, arguments_json=json.dumps(block.input))
            )
    usage = message.usage
    cache_read = usage.cache_read_input_tokens or 0
    cache_write = usage.cache_creation_input_tokens or 0
    by_ttl = usage.cache_creation
    breakdown = (
        {
            ttl: n
            for ttl, n in (
                ("5m", by_ttl.ephemeral_5m_input_tokens),
                ("1h", by_ttl.ephemeral_1h_input_tokens),
            )
            if n
        }
        if by_ttl
        else {}
    )
    return BrainResponse(
        provider=PROVIDER,
        model=message.model,
        message=Message(role="assistant", parts=tuple(parts)),
        stop_reason=_STOP_REASONS.get(message.stop_reason or "", "other"),
        usage=Usage(
            # Anthropic reports uncached input only; canonical input_tokens is the whole prompt.
            input_tokens=usage.input_tokens + cache_read + cache_write,
            output_tokens=usage.output_tokens,
            cached_input_tokens=cache_read,
            cache_write_input_tokens=cache_write,
            cache_write_breakdown=breakdown,
        ),
        latency_ms=latency_ms,
    )


@contextmanager
def mapped_errors(model: str) -> Iterator[None]:
    """Map every SDK failure onto one canonical error. Most specific first."""
    ctx = {"provider": PROVIDER, "model": model}
    try:
        yield
    except anthropic.APITimeoutError as exc:  # subclass of APIConnectionError: must come first
        raise ProviderTimeoutError(str(exc), **ctx) from exc
    except anthropic.APIConnectionError as exc:
        raise ProviderConnectionError(str(exc), **ctx) from exc
    except anthropic.RateLimitError as exc:
        raise RateLimitError(str(exc), **ctx) from exc
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
        raise AuthError(str(exc), **ctx) from exc
    except anthropic.APIStatusError as exc:
        if exc.status_code >= 500:
            raise ProviderServerError(str(exc), **ctx) from exc
        if exc.status_code == 402:  # billing problem: the account cannot use it
            raise AuthError(str(exc), **ctx) from exc
        raise BadRequestError(str(exc), **ctx) from exc
    except anthropic.APIError as exc:
        raise BrainError(str(exc), **ctx) from exc
