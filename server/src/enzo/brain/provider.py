"""The one interface every brain adapter implements (spec §1.3.1)."""

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from enzo.brain.errors import UnsupportedCapabilityError
from enzo.brain.models import (
    BrainResponse,
    Capabilities,
    Message,
    StreamEvent,
    ToolChoice,
    ToolSpec,
)
from enzo.brain.toolcall import ToolCallRejection, ValidatedCall, validate_call


@runtime_checkable
class BrainProvider(Protocol):
    @property
    def provider(self) -> str: ...

    @property
    def model(self) -> str: ...

    @property
    def capabilities(self) -> Capabilities: ...

    async def chat(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSpec] = (),
        tool_choice: ToolChoice = "auto",
        max_output_tokens: int,
    ) -> BrainResponse:
        """Send a composed prompt plus tools; get text and/or tool calls back."""
        ...

    def stream(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSpec] = (),
        tool_choice: ToolChoice = "auto",
        max_output_tokens: int,
    ) -> AsyncIterator[StreamEvent]:
        """Yield text as generated, then exactly one ``StreamDone`` with the full response."""
        ...

    async def count_tokens(
        self, messages: Sequence[Message], *, tools: Sequence[ToolSpec] = ()
    ) -> int:
        """Token count for budgeting; ``capabilities.token_counting`` says if it is exact."""
        ...

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """One vector per text, or ``UnsupportedCapabilityError``."""
        ...


async def tool_call(
    brain: BrainProvider,
    messages: Sequence[Message],
    tools: Mapping[str, tuple[ToolSpec, type[BaseModel]]],
    *,
    max_output_tokens: int,
) -> list[ValidatedCall | ToolCallRejection]:
    """Force the brain to call a tool and return every call validated (spec §1.3.1 ``tool_call()``).

    Implemented once on top of ``chat`` so every brain gets identical validation.
    """
    if brain.capabilities.tool_calling == "unsupported":
        msg = "this brain cannot call tools"
        raise UnsupportedCapabilityError(msg, provider=brain.provider, model=brain.model)
    response = await brain.chat(
        messages,
        tools=[spec for spec, _ in tools.values()],
        tool_choice="required",
        max_output_tokens=max_output_tokens,
    )
    params = {name: model for name, (_, model) in tools.items()}
    return [validate_call(call, params) for call in response.tool_calls]
