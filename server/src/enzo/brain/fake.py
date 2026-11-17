"""``ScriptedBrain``: deterministic brain; the orchestrator is testable with no live LLM (§2.3)."""

import hashlib
import math
from collections import deque
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass

from enzo.brain.errors import BrainError
from enzo.brain.models import (
    BrainResponse,
    Capabilities,
    Message,
    StreamDone,
    StreamEvent,
    TextDelta,
    ToolChoice,
    ToolSpec,
    Usage,
)

EMBED_DIM = 16


class ScriptExhaustedError(AssertionError):
    """The code under test called the brain more often than the test scripted."""


@dataclass(frozen=True, slots=True)
class RecordedCall:
    messages: tuple[Message, ...]
    tools: tuple[ToolSpec, ...]
    tool_choice: ToolChoice
    max_output_tokens: int


def estimate_tokens(text: str) -> int:
    """Rough 4-characters-per-token estimate. Labelled ``estimate`` in capabilities, never exact."""
    return math.ceil(len(text) / 4)


class ScriptedBrain:
    """Replays scripted assistant messages (or raises scripted errors) in order; records calls."""

    def __init__(
        self,
        script: Iterable[Message | BrainError],
        *,
        provider: str = "fake",
        model: str = "scripted-1",
    ) -> None:
        self._script = deque(script)
        self._provider = provider
        self._model = model
        self.calls: list[RecordedCall] = []

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
            streaming=True,
            token_counting="estimate",  # noqa: S106 - a capability label, not a secret
            embeddings=True,
        )

    async def chat(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSpec] = (),
        tool_choice: ToolChoice = "auto",
        max_output_tokens: int,
    ) -> BrainResponse:
        self.calls.append(
            RecordedCall(tuple(messages), tuple(tools), tool_choice, max_output_tokens)
        )
        if not self._script:
            msg = f"ScriptedBrain got call #{len(self.calls)} but the script is empty"
            raise ScriptExhaustedError(msg)
        step = self._script.popleft()
        if isinstance(step, BrainError):
            raise step
        if step.role != "assistant":
            msg = "scripted steps must be assistant messages"
            raise ValueError(msg)
        return BrainResponse(
            provider=self._provider,
            model=self._model,
            message=step,
            stop_reason="tool_use" if step.tool_calls else "end_turn",
            usage=Usage(
                input_tokens=await self.count_tokens(messages, tools=tools),
                output_tokens=estimate_tokens(
                    step.text + "".join(c.arguments_json for c in step.tool_calls)
                ),
            ),
            latency_ms=0.0,
        )

    async def stream(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSpec] = (),
        tool_choice: ToolChoice = "auto",
        max_output_tokens: int,
    ) -> AsyncIterator[StreamEvent]:
        response = await self.chat(
            messages, tools=tools, tool_choice=tool_choice, max_output_tokens=max_output_tokens
        )
        for word in response.text.split(" ") if response.text else ():
            yield TextDelta(text=word)
        yield StreamDone(response=response)

    async def count_tokens(
        self, messages: Sequence[Message], *, tools: Sequence[ToolSpec] = ()
    ) -> int:
        text = "".join(m.model_dump_json() for m in messages) + "".join(
            t.model_dump_json() for t in tools
        )
        return estimate_tokens(text)

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Deterministic unit vectors from a hash: same text, same vector; no semantics."""
        vectors = []
        for text in texts:
            digest = hashlib.sha256(text.encode()).digest()
            raw = [b - 127.5 for b in digest[:EMBED_DIM]]
            norm = math.sqrt(sum(x * x for x in raw))
            vectors.append([x / norm for x in raw])
        return vectors
