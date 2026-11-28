"""Counting tokens for budgets (spec §1.3.1 ``count_tokens``, §1.3.5, §5.2; ADR 0024).

Three ways to know how big a prompt is, cheapest first:

1. A local estimate, for every brain call: no network, well under a millisecond per 100K
   characters. It over-counts on purpose: ASCII text at a per-provider characters-per-token ratio
   kept below the provider's published average, and every other character as a whole token.
2. Learned from the providers' own usage reports: each reply says exactly how many input tokens
   its prompt had, so the next estimate for that brain is scaled up when the last one was low.
   Never scaled down below the plain estimate.
3. The provider's exact count (a network call: Anthropic's free ``count_tokens`` endpoint, OpenAI's
   ``input_tokens`` endpoint), only when the estimate comes near a limit, where being wrong
   matters. Each one is logged with how long it took. A brain without one keeps the estimate.

Images count as their provider bills them, at the size the brain is sent (``enzo.images.sizing``);
their bytes are never counted as text.
"""

import json
import logging
import math
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from enzo.brain.config import BrainsConfig, VisionConfig
from enzo.brain.errors import BrainError
from enzo.brain.models import Message, NativePart, TextPart, ToolCall, ToolResult, ToolSpec
from enzo.brain.provider import BrainProvider
from enzo.images.sizing import image_tokens

log = logging.getLogger("enzo.brain.tokens")

#: The most cautious ratio: used where nothing better is configured.
DEFAULT_CHARS_PER_TOKEN = 2.0
#: Roles, separators and message boundaries the provider adds around each message and tool.
MESSAGE_OVERHEAD = 6
TOOL_OVERHEAD = 12


def text_tokens(text: str, chars_per_token: float = DEFAULT_CHARS_PER_TOKEN) -> int:
    """Estimated tokens of a text: ASCII at ``chars_per_token``, any other character as one."""
    ascii_chars = len(text.encode("ascii", "ignore"))
    return math.ceil(ascii_chars / chars_per_token) + (len(text) - ascii_chars)


def estimate_tokens(
    messages: Sequence[Message],
    tools: Sequence[ToolSpec] = (),
    *,
    chars_per_token: float = DEFAULT_CHARS_PER_TOKEN,
    vision: VisionConfig | None = None,
) -> int:
    """A conservative local estimate of a prompt's input tokens: tools, messages and replay data.

    A message with provider replay data is counted from that data (what the provider will
    actually receive, reasoning included), never from its canonical parts as well. Each image
    costs what ``vision`` (the brain's profile) says; without one, a cautious maximum.
    """
    total = 0
    for spec in tools:
        total += TOOL_OVERHEAD + text_tokens(spec.model_dump_json(), chars_per_token)
    for message in messages:
        total += MESSAGE_OVERHEAD + text_tokens(_wire_text(message), chars_per_token)
        total += sum(image_tokens(i.width, i.height, vision) for i in message.images)
    return total


def _wire_text(message: Message) -> str:
    native = next((p for p in message.parts if isinstance(p, NativePart)), None)
    if native is not None:
        return json.dumps(native.payload, ensure_ascii=False, separators=(",", ":"))
    pieces: list[str] = []
    for part in message.parts:
        if isinstance(part, TextPart):
            pieces.append(part.text)
        elif isinstance(part, ToolCall):
            pieces.append(f"{part.name} {part.id} {part.arguments_json}")
        elif isinstance(part, ToolResult):
            pieces.append(f"{part.call_id} {part.content}")
    return "\n".join(pieces)


@dataclass(frozen=True, slots=True)
class Count:
    tokens: int
    #: From the provider's own counter; otherwise a (conservative) estimate.
    exact: bool


class TokenCounter:
    """Counts prompts for one Enzo: local first, learned from usage, exact near a limit.

    One per gateway, so what it learns from each brain's usage reports carries across chats.
    """

    def __init__(self, config: BrainsConfig) -> None:
        self._config = config
        #: Per brain: how much its last reported prompt exceeded the plain estimate (>= 1).
        self._scale: dict[str, float] = {}

    def estimate(self, brain: str, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> int:
        return math.ceil(self._raw(brain, messages, tools) * self._scale.get(brain, 1.0))

    def learn(
        self, brain: str, messages: Sequence[Message], tools: Sequence[ToolSpec], reported: int
    ) -> None:
        """A reply's usage report: the exact input tokens of the prompt that was sent."""
        raw = self._raw(brain, messages, tools)
        if raw > 0 and reported > 0:
            self._scale[brain] = max(1.0, reported / raw)

    async def count(
        self,
        brain: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
        *,
        limit: int,
        provider: Callable[[], BrainProvider | None],
        attach: Callable[[Sequence[Message]], Awaitable[list[Message]]] | None = None,
    ) -> Count:
        """How big this prompt is for ``brain``. Asks the provider (built only then) near
        ``limit``, if it can count exactly. ``attach`` fills in images for the exact count."""
        estimate = self.estimate(brain, messages, tools)
        if estimate < limit * self._config.context.exact_above:
            return Count(estimate, exact=False)
        counter = provider()
        exact_counter = counter is not None and counter.capabilities.token_counting == "exact"  # noqa: S105 - a label
        if counter is None or not exact_counter:
            return Count(estimate, exact=False)
        started = time.perf_counter()
        try:
            sent = messages
            if attach is not None and any(m.images for m in messages):
                sent = await attach(messages)
            exact = await counter.count_tokens(sent, tools=tools)
        except BrainError as exc:  # the count failed: the estimate still errs on the high side
            log.warning(
                "context.count failed",
                extra={"brain": brain, "kind": exc.kind, "estimate": estimate},
            )
            return Count(estimate, exact=False)
        # The spec's rule: a network round trip is recorded with what it cost in time.
        log.info(
            "context.count",
            extra={
                "brain": brain,
                "tokens": exact,
                "estimate": estimate,
                "ms": round((time.perf_counter() - started) * 1000),
            },
        )
        self.learn(brain, messages, tools, exact)
        return Count(exact, exact=True)

    def _raw(self, brain: str, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> int:
        cfg = self._config.brains.get(brain)
        ratio = self._config.context.ratio(cfg) if cfg else DEFAULT_CHARS_PER_TOKEN
        return estimate_tokens(
            messages, tools, chars_per_token=ratio, vision=self._config.vision_of(brain)
        )
