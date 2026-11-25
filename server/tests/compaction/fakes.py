"""A rule-based brain for context tests: answers like a model would, and records every request.

- Asked for a summary (the record_summary tool is offered): writes notes from the transcript.
  Lines of the user that mention "decide" become decisions; it marks no corrections on its own
  unless ``marks_corrections`` (the code must keep them anyway).
- A message asking about a document: looks it up with read_stored_output (find), then answers
  with what it read.
- Otherwise: a short reply that quotes nothing.
Usage reports a prompt size from the same estimate Enzo uses, scaled by ``true_ratio``.
"""

import json
import re
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from enzo.brain.config import BrainsConfig
from enzo.brain.errors import BrainError
from enzo.brain.models import (
    BrainResponse,
    Capabilities,
    Message,
    NativePart,
    StreamDone,
    StreamEvent,
    TextDelta,
    TextPart,
    ToolCall,
    ToolChoice,
    ToolResult,
    ToolSpec,
    Usage,
)
from enzo.brain.tokens import estimate_tokens

CORRECTION_TURN = re.compile(r"Turn (\d+), the user[^\n]*:\n(?:Actually|No,)", re.MULTILINE)


@dataclass
class Request:
    messages: tuple[Message, ...]
    tools: tuple[ToolSpec, ...]
    max_output_tokens: int

    @property
    def text(self) -> str:
        """Everything the provider would read, as one string (for "did it reach the prompt")."""
        out: list[str] = []
        for m in self.messages:
            for p in m.parts:
                if isinstance(p, TextPart):
                    out.append(p.text)
                elif isinstance(p, ToolResult):
                    out.append(p.content)
                elif isinstance(p, ToolCall):
                    out.append(p.arguments_json)
        return "\n".join(out)

    @property
    def summarizing(self) -> bool:
        return any(t.name == "record_summary" for t in self.tools)


@dataclass
class RuleBrain:
    name: str
    provider: str = "anthropic"
    model: str = "rule-1"
    exact: bool = False
    marks_corrections: bool = False
    #: Real tokens per estimated token, for the usage report.
    true_ratio: float = 1.0
    #: Fail the summary request with this error (once each).
    summary_errors: list[BrainError] = field(default_factory=list)
    #: Answer the summary request without calling the tool.
    refuses_summary: bool = False
    #: Each reply carries replay data, like Claude's thinking.
    replay: bool = True
    #: Replies to give first (before the rules), in order.
    script: list[Message] = field(default_factory=list)
    requests: list[Request] = field(default_factory=list)
    counted: int = 0

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(
            tool_calling="native",
            forced_tool_choice=False,
            streaming=True,
            token_counting="exact" if self.exact else "estimate",
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
        request = Request(tuple(messages), tuple(tools), max_output_tokens)
        self.requests.append(request)
        if request.summarizing:
            if self.summary_errors:
                raise self.summary_errors.pop(0)
            reply = (
                Message.assistant("Here is a summary.")
                if self.refuses_summary
                else (self._summary(request))
            )
        else:
            reply = self.script.pop(0) if self.script else self._answer(messages)
        if self.replay:
            native = NativePart(
                provider=self.provider, payload={"thinking": f"t{len(self.requests)}"}
            )
            reply = Message(role="assistant", parts=(native, *reply.parts))
        return BrainResponse(
            provider=self.provider,
            model=self.model,
            message=reply,
            stop_reason="tool_use" if reply.tool_calls else "end_turn",
            usage=Usage(
                input_tokens=int(
                    estimate_tokens(messages, tools, chars_per_token=4.0) * self.true_ratio
                ),
                output_tokens=10,
            ),
            latency_ms=1.0,
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
        if response.text:
            yield TextDelta(text=response.text)
        yield StreamDone(response=response)

    async def count_tokens(
        self, messages: Sequence[Message], *, tools: Sequence[ToolSpec] = ()
    ) -> int:
        self.counted += 1
        return int(estimate_tokens(messages, tools, chars_per_token=4.0) * self.true_ratio)

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        raise NotImplementedError

    def _summary(self, request: Request) -> Message:
        transcript = request.messages[-1].text
        users = re.findall(r"Turn (\d+), the user[^\n]*:\n([^\n]+)", transcript)
        decisions = [f"Turn {n}: {words}" for n, words in users if "decide" in words.lower()]
        args: dict[str, Any] = {
            "story": f"The user and Enzo talked through {len(users)} messages.",
            "decisions": decisions,
            "facts": [],
            "open_tasks": [],
        }
        if self.marks_corrections:
            args["corrections"] = [int(n) for n in CORRECTION_TURN.findall(transcript)]
        call = ToolCall(
            id=f"sum{len(self.requests)}", name="record_summary", arguments_json=json.dumps(args)
        )
        return Message.assistant(tool_calls=(call,))

    def _answer(self, messages: Sequence[Message]) -> Message:
        last = messages[-1]
        if last.role == "tool":
            result = next(p for p in last.parts if isinstance(p, ToolResult))
            if result.name == "read_stored_output":
                found = re.search(r"CLAUSE-\d+: [^\\\n]+", result.content)
                return Message.assistant(f"From the document: {found.group(0) if found else '?'}")
            return Message.assistant("Done.")
        words = next((m.text for m in reversed(messages) if m.role == "user"), "")
        stored = re.search(r"stored text (s_[0-9a-f]{16})", words)
        asked = re.search(r"what does (CLAUSE-\d+) say", words)
        if stored and asked:
            args = {"id": stored.group(1), "find": asked.group(1)}
            call = ToolCall(id="read1", name="read_stored_output", arguments_json=json.dumps(args))
            return Message.assistant(tool_calls=(call,))
        return Message.assistant("Okay.")


def config(**context: Any) -> BrainsConfig:
    """Brains with small windows, so a test chat outgrows them quickly. ``other`` is a second
    provider that must never be used for a summary."""
    small = {"limits_source": "test", "context_window": 3_000, "max_output_tokens": 1_000}
    return BrainsConfig.model_validate(
        {
            "brains": {
                "main": {
                    "api": "anthropic",
                    "model": "main-1",
                    "api_key_env": "K",
                    "tier": "balanced",
                    "recommended": True,
                    **small,
                },
                "quick": {
                    "api": "anthropic",
                    "model": "quick-1",
                    "api_key_env": "K",
                    "tier": "fast",
                    "recommended": True,
                    **small,
                },
                "other": {
                    "api": "openai-responses",
                    "model": "other-1",
                    "api_key_env": "O",
                    "tier": "balanced",
                    **small,
                },
            },
            "context": {
                "margin": 0.0,
                "reply_tokens": 200,
                "task_share": 0.25,
                "compact_at": 1_000,
                "keep_recent": 400,
                "summary_tokens": 300,
                "chars_per_token": {"default": 4.0},
                "tool_inline_chars": 1_000,
                "paste_inline_chars": 3_000,
                "excerpt_chars": 300,
                **context,
            },
        }
    )
