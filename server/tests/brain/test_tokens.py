"""Counting tokens for budgets: local first, learned from usage, exact only near a limit."""

import time

from enzo.brain.config import load_config
from enzo.brain.errors import RateLimitError
from enzo.brain.models import Message, NativePart, TextPart, ToolCall, ToolResult, ToolSpec
from enzo.brain.provider import BrainProvider
from enzo.brain.tokens import (
    MESSAGE_OVERHEAD,
    TokenCounter,
    estimate_tokens,
    text_tokens,
)
from tests.brain.schemas import SearchWeb
from tests.compaction.fakes import RuleBrain

SPEC = ToolSpec.from_model("search_web", "Search.", SearchWeb)


def test_ascii_counts_at_the_ratio_and_any_other_character_as_a_whole_token() -> None:
    assert text_tokens("a" * 100, 2.0) == 50
    assert text_tokens("a" * 101, 2.0) == 51  # rounds up: never under
    assert text_tokens("日本語のテキスト", 4.0) == 8  # each CJK character a token
    assert text_tokens("café", 4.0) == 2  # "caf" → 1, "é" → 1


def test_the_estimate_errs_high_against_published_averages() -> None:
    """Claude's tokenizer averages ~2.5 characters per token (models overview, read
    2026-11-25); OpenAI's ~4. The configured ratios must count more tokens than those."""
    cfg = load_config()
    english = "The quick brown fox jumps over the lazy dog. " * 200
    for brain, published in (("claude-sonnet", 2.5), ("gpt-sol", 4.0)):
        ratio = cfg.context.ratio(cfg.brains[brain])
        assert text_tokens(english, ratio) > len(english) / published


def test_replay_data_is_counted_once_in_place_of_the_canonical_parts() -> None:
    text = "x" * 400
    plain = Message.assistant(text)
    native = Message(
        role="assistant",
        parts=(
            NativePart(provider="p", payload={"content": [{"text": text}]}),
            TextPart(text=text),
        ),
    )
    assert estimate_tokens([native], chars_per_token=4.0) < 2 * estimate_tokens(
        [plain], chars_per_token=4.0
    )
    assert estimate_tokens([native], chars_per_token=4.0) > estimate_tokens(
        [plain], chars_per_token=4.0
    )


def test_tools_calls_and_results_all_count() -> None:
    call = Message.assistant(tool_calls=(ToolCall(id="c1", name="t", arguments_json="{}" * 50),))
    result = Message.tool(ToolResult(call_id="c1", name="t", content="r" * 400))
    bare = estimate_tokens([], chars_per_token=4.0)
    assert bare == 0
    assert estimate_tokens([call], chars_per_token=4.0) > MESSAGE_OVERHEAD
    assert estimate_tokens([result], chars_per_token=4.0) >= 100
    assert estimate_tokens([], [SPEC], chars_per_token=4.0) > 20


def test_a_long_prompt_is_estimated_in_well_under_a_millisecond_per_100k_characters() -> None:
    """The estimate runs before every brain call: it must cost nothing next to the call."""
    prompt = [Message.user("word " * 20_000) for _ in range(10)]  # 1M characters
    started = time.perf_counter()
    estimate_tokens(prompt, chars_per_token=2.0)
    assert time.perf_counter() - started < 0.1


async def test_the_provider_is_asked_only_near_the_limit_and_built_only_then() -> None:
    cfg = load_config()
    counter = TokenCounter(cfg)
    brain = RuleBrain("claude-sonnet", exact=True, true_ratio=0.5)
    built: list[BrainProvider] = []

    def provider() -> BrainProvider:
        built.append(brain)
        return brain

    words = [Message.user("w" * 1_000)]
    far = await counter.count("claude-sonnet", words, [], limit=10_000, provider=provider)
    assert (far.exact, built, brain.counted) == (False, [], 0)
    near = await counter.count("claude-sonnet", words, [], limit=600, provider=provider)
    assert near.exact
    assert brain.counted == 1
    assert near.tokens == int(estimate_tokens(words, chars_per_token=4.0) * 0.5)


async def test_a_brain_without_an_exact_counter_keeps_the_estimate() -> None:
    cfg = load_config()
    counter = TokenCounter(cfg)
    brain = RuleBrain("gpt-sol", exact=False)
    words = [Message.user("w" * 1_000)]
    count = await counter.count("gpt-sol", words, [], limit=10, provider=lambda: brain)
    assert not count.exact
    assert brain.counted == 0
    assert count.tokens == counter.estimate("gpt-sol", words, [])


async def test_a_failed_exact_count_keeps_the_estimate_and_never_raises() -> None:
    cfg = load_config()
    counter = TokenCounter(cfg)
    brain = RuleBrain("claude-sonnet", exact=True)

    async def refuse(*_a: object, **_k: object) -> int:
        raise RateLimitError("slow down", provider="anthropic", model="m")

    brain.count_tokens = refuse  # type: ignore[method-assign]
    words = [Message.user("w" * 1_000)]
    count = await counter.count("claude-sonnet", words, [], limit=10, provider=lambda: brain)
    assert count == type(count)(counter.estimate("claude-sonnet", words, []), exact=False)


def test_usage_reports_scale_the_estimate_up_never_down() -> None:
    cfg = load_config()
    counter = TokenCounter(cfg)
    words = [Message.user("w" * 1_000)]
    plain = counter.estimate("gpt-sol", words, [])
    counter.learn("gpt-sol", words, [], reported=plain * 3)
    assert counter.estimate("gpt-sol", words, []) == plain * 3
    assert counter.estimate("claude-sonnet", words, []) != plain * 3  # per brain
    counter.learn("gpt-sol", words, [], reported=plain // 10)
    assert counter.estimate("gpt-sol", words, []) == plain  # back to the plain estimate, no lower
    counter.learn("gpt-sol", words, [], reported=0)  # no usage reported: nothing learned
    assert counter.estimate("gpt-sol", words, []) == plain
