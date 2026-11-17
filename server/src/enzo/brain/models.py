"""Canonical, provider-neutral types for every brain (spec §1.3.1, §1.3.2, §7.2.1)."""

import json
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Strictest rule shared by the providers Enzo supports (OpenAI documents this exact set).
TOOL_NAME_PATTERN = r"^[a-zA-Z0-9_-]{1,64}$"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TextPart(_Frozen):
    type: Literal["text"] = "text"
    text: str


class ToolCall(_Frozen):
    """A tool call exactly as the brain emitted it.

    Arguments stay raw JSON: a malformed payload must reach validation intact, so the exact error
    can be fed back to the brain (spec §4.2 "error as data") instead of being lost in a parse.
    """

    type: Literal["tool_call"] = "tool_call"
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    arguments_json: str


class ToolResult(_Frozen):
    type: Literal["tool_result"] = "tool_result"
    call_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    content: str
    is_error: bool = False


type Part = Annotated[TextPart | ToolCall | ToolResult, Field(discriminator="type")]
type Role = Literal["system", "user", "assistant", "tool"]

_ALLOWED_PARTS: dict[str, tuple[type[_Frozen], ...]] = {
    "system": (TextPart,),
    "user": (TextPart,),
    "assistant": (TextPart, ToolCall),
    "tool": (ToolResult,),
}


class Message(_Frozen):
    role: Role
    parts: tuple[Part, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _parts_match_role(self) -> Self:
        allowed = _ALLOWED_PARTS[self.role]
        for part in self.parts:
            if not isinstance(part, allowed):
                msg = f"a {self.role} message cannot contain a {part.type} part"
                raise ValueError(msg)
        return self

    @classmethod
    def system(cls, text: str) -> Self:
        return cls(role="system", parts=(TextPart(text=text),))

    @classmethod
    def user(cls, text: str) -> Self:
        return cls(role="user", parts=(TextPart(text=text),))

    @classmethod
    def assistant(cls, text: str | None = None, tool_calls: tuple[ToolCall, ...] = ()) -> Self:
        parts: tuple[Part, ...] = ((TextPart(text=text),) if text else ()) + tool_calls
        return cls(role="assistant", parts=parts)

    @classmethod
    def tool(cls, *results: ToolResult) -> Self:
        return cls(role="tool", parts=results)

    @property
    def text(self) -> str:
        return "".join(p.text for p in self.parts if isinstance(p, TextPart))

    @property
    def tool_calls(self) -> tuple[ToolCall, ...]:
        return tuple(p for p in self.parts if isinstance(p, ToolCall))


class ToolSpec(_Frozen):
    """What the brain sees of a tool. Adapters translate this into each provider's format."""

    name: str = Field(pattern=TOOL_NAME_PATTERN)
    description: str = Field(min_length=1)
    parameters: dict[str, Any]

    @classmethod
    def from_model(cls, name: str, description: str, params: type[BaseModel]) -> Self:
        """Derive the JSON Schema from a Pydantic model so schema and validation never drift."""
        if params.model_config.get("extra") != "forbid":
            msg = f"{params.__name__} must set extra='forbid' so unknown arguments are rejected"
            raise TypeError(msg)
        schema = params.model_json_schema()
        json.dumps(schema)  # fail now, not at the provider, if the schema is not JSON-serialisable
        return cls(name=name, description=description, parameters=schema)


class Usage(_Frozen):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)


type StopReason = Literal["end_turn", "tool_use", "max_tokens", "refusal", "other"]
type ToolChoice = Literal["auto", "required", "none"]


class BrainResponse(_Frozen):
    provider: str
    model: str
    message: Message
    stop_reason: StopReason
    usage: Usage
    latency_ms: float = Field(ge=0)

    @model_validator(mode="after")
    def _is_assistant(self) -> Self:
        if self.message.role != "assistant":
            msg = "a brain response must be an assistant message"
            raise ValueError(msg)
        return self

    @property
    def text(self) -> str:
        return self.message.text

    @property
    def tool_calls(self) -> tuple[ToolCall, ...]:
        return self.message.tool_calls


class TextDelta(_Frozen):
    type: Literal["text_delta"] = "text_delta"
    text: str


class StreamDone(_Frozen):
    """Last event of every stream. Tool calls arrive here, complete, never as partial JSON."""

    type: Literal["done"] = "done"
    response: BrainResponse


type StreamEvent = Annotated[TextDelta | StreamDone, Field(discriminator="type")]


class Capabilities(_Frozen):
    """What a brain can really do. Enzo must not claim a capability a brain lacks (spec §1.3.2)."""

    tool_calling: Literal["native", "emulated", "unsupported"]
    streaming: bool
    token_counting: Literal["exact", "estimate"]
    embeddings: bool
