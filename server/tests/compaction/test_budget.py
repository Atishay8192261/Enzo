"""Each brain's budget comes from its sourced window and the configured split (spec §5.2)."""

from typing import Any

import pytest
from pydantic import ValidationError

from enzo.brain.config import BrainsConfig, load_config
from enzo.compaction import Budget

BRAIN = {"api": "anthropic", "model": "m", "api_key_env": "KEY", "tier": "balanced"}


def test_every_confirmed_limit_says_where_it_was_read() -> None:
    cfg = load_config()
    confirmed = {n for n, b in cfg.brains.items() if b.context_window}
    assert confirmed == {
        "claude-opus",
        "claude-sonnet",
        "claude-haiku",
        "gpt-astra",
        "gpt-sol",
        "gpt-luna",
    }
    for name in confirmed:
        source = cfg.brains[name].limits_source or ""
        assert "read 2026-" in source, name
        assert source.startswith("https://"), name


@pytest.mark.parametrize(
    ("brain", "error"),
    [
        ({**BRAIN, "context_window": 1000}, "limits_source"),
        ({**BRAIN, "max_output_tokens": 1000}, "limits_source"),
        ({**BRAIN, "context_window": 0, "limits_source": "x"}, "greater than 0"),
    ],
)
def test_unsourced_or_impossible_limits_fail_loudly(brain: dict[str, Any], error: str) -> None:
    with pytest.raises(ValidationError, match=error):
        BrainsConfig.model_validate({"brains": {"a": brain}})


@pytest.mark.parametrize(
    ("context", "error"),
    [
        ({"chars_per_token": {"anthropic": 2.0}}, "default"),
        ({"chars_per_token": {"default": 0}}, "positive"),
        ({"excerpt_chars": 30_000}, "excerpt_chars"),
        ({"margin": 0.6}, "margin"),
        ({"bogus": 1}, "bogus"),
    ],
)
def test_an_inconsistent_split_fails_loudly(context: dict[str, Any], error: str) -> None:
    with pytest.raises(ValidationError, match=error):
        BrainsConfig.model_validate({"brains": {"a": BRAIN}, "context": context})


def budget(name: str) -> Budget:
    cfg = load_config()
    return Budget.of(cfg.brains[name], cfg.context, reply=cfg.context.reply_tokens)


def test_the_default_split_for_the_default_brains() -> None:
    """The numbers ADR 0024 records, derived from configuration, not guessed per call."""
    sonnet = budget("claude-sonnet")
    assert sonnet == Budget(
        window=1_000_000,
        confirmed=True,
        reply=16_000,
        limit=884_000,  # 1M less a 10% margin and the reply
        compact_at=150_000,  # Anthropic's own server-side compaction default
        keep=30_000,
    )
    # GPT-6.2 doubles its input price past 272K tokens: compaction comes before (150K anyway).
    assert budget("gpt-sol").compact_at == 150_000
    # Haiku's price rises past 100K input tokens: compacted before it gets there.
    assert budget("claude-haiku").compact_at == 100_000
    # A window nobody confirmed is treated as small, never as large.
    grok = budget("grok")
    assert (grok.window, grok.confirmed, grok.limit) == (128_000, False, 99_200)
    assert grok.compact_at == int(99_200 * 0.75)
    assert grok.keep == 24_800


def test_the_task_always_has_room_at_a_turns_start() -> None:
    for name in load_config().brains:
        b = budget(name)
        assert b.compact_at <= b.limit * 0.75, name
        assert b.keep <= b.compact_at / 3, name


def test_a_fallback_must_fit_the_same_prompt() -> None:
    both = budget("claude-sonnet").tighter(budget("grok"))
    assert both.limit == budget("grok").limit
    assert not both.confirmed


def test_a_reply_never_reserves_more_than_the_model_can_write() -> None:
    cfg = load_config()
    small = cfg.brains["claude-sonnet"].model_copy(update={"max_output_tokens": 8_000})
    assert Budget.of(small, cfg.context, reply=16_000).reply == 8_000


def test_a_window_too_small_for_its_reply_is_refused() -> None:
    cfg = load_config()
    tiny = cfg.brains["claude-sonnet"].model_copy(update={"context_window": 10_000})
    with pytest.raises(ValueError, match="no room"):
        Budget.of(tiny, cfg.context, reply=16_000)
