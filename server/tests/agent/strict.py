"""Test brains that behave like the real providers where #39 and #46 bit."""

import asyncio
from collections.abc import AsyncIterator, Sequence

from enzo.brain import BadRequestError, Message, TextDelta, ToolResult, ToolSpec
from enzo.brain.fake import ScriptedBrain
from enzo.brain.models import StreamEvent, ToolChoice


def check_history(messages: Sequence[Message]) -> None:
    """What OpenAI and Anthropic enforce: every tool call is answered in the very next message."""
    for i, message in enumerate(messages):
        if message.role != "assistant" or not message.tool_calls or i == len(messages) - 1:
            continue
        following = messages[i + 1]
        answered = (
            {p.call_id for p in following.parts if isinstance(p, ToolResult)}
            if following.role == "tool"
            else set()
        )
        for call in message.tool_calls:
            if call.id not in answered:
                msg = f"No tool output found for function call {call.id}."
                raise BadRequestError(msg, provider="fake", model="strict")


class StrictBrain(ScriptedBrain):
    """A scripted brain that refuses an invalid history, as the providers do."""

    async def stream(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSpec] = (),
        tool_choice: ToolChoice = "auto",
        max_output_tokens: int,
    ) -> AsyncIterator[StreamEvent]:
        check_history(messages)
        async for event in super().stream(
            messages, tools=tools, tool_choice=tool_choice, max_output_tokens=max_output_tokens
        ):
            yield event


class Stalls(ScriptedBrain):
    """Streams a few words, then waits forever: a reply whose connection silently died."""

    def __init__(self, words: Sequence[str] = ("Once", " upon", " a time")) -> None:
        super().__init__([])
        self.words = list(words)
        self.started = asyncio.Event()
        self.cancelled = False

    async def stream(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSpec] = (),
        tool_choice: ToolChoice = "auto",
        max_output_tokens: int,
    ) -> AsyncIterator[StreamEvent]:
        for word in self.words:
            yield TextDelta(text=word)
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        yield TextDelta(text="never")  # pragma: no cover - unreachable
