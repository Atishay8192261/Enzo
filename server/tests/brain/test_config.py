import tomllib
from importlib import resources
from typing import Any

import pytest
from pydantic import ValidationError

from enzo.brain.config import BrainsConfig, load_config

BRAIN = {"api": "anthropic", "model": "m", "api_key_env": "KEY", "tier": "balanced"}


def config(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"brains": {"a": BRAIN, "b": BRAIN}}
    return {**base, **overrides}


def test_default_config_is_valid_and_fully_priced() -> None:
    cfg = load_config()
    for name, brain in cfg.brains.items():
        assert brain.pricing is not None, name
        assert "read 2026-" in brain.pricing.source, name  # every price says when it was read


def test_default_config_holds_no_secrets() -> None:
    text = resources.files("enzo.brain").joinpath("brains.toml").read_text()
    raw = tomllib.loads(text)
    for brain in raw["brains"].values():
        assert set(brain) & {"api_key", "key", "token", "secret"} == set()
    assert "sk-" not in text


@pytest.mark.parametrize(
    ("data", "error"),
    [
        (config(routing={"default": "nope"}), "routing.default"),
        (config(routing={"tasks": {"hard": "zzz"}}), "routing.tasks.hard"),
        (config(routing={"fallback": {"a": "b"}}), "routing.fallback"),  # old schema: loud
        (config(routing={"tasks": {"bogus": "fast"}}), "tasks.bogus"),
        (config(brains={"Bad Name": BRAIN}), "pattern"),
        (config(brains={"a": {k: v for k, v in BRAIN.items() if k != "tier"}}), "tier"),
        (config(brains={"a": {**BRAIN, "tier": "huge"}}), "tier"),
        (config(brains={"fast": BRAIN}), "brain names cannot be tier names: fast"),
        (config(brains={"a": {**BRAIN, "effort": "turbo"}}), "anthropic effort"),
        (config(brains={"a": {**BRAIN, "api_key_env": "sk-123"}}), "api_key_env"),
        (config(brains={"a": {**BRAIN, "api_key": "sk-123"}}), "api_key"),
    ],
)
def test_invalid_configuration_fails_loudly(data: dict[str, Any], error: str) -> None:
    with pytest.raises(ValidationError, match=error):
        BrainsConfig.model_validate(data)


def test_provider_name_defaults_from_the_api() -> None:
    cfg = load_config()
    assert cfg.brains["claude-opus"].provider_name == "anthropic"
    assert cfg.brains["gpt-sol"].provider_name == "openai"
    assert cfg.brains["grok"].provider_name == "xai"


def test_the_catalog_matches_the_decisions() -> None:
    """Every tier has a recommended model per main provider; defaults are Sonnet 5.5 and Sol."""
    cfg = load_config()
    assert cfg.routing.default == "balanced"
    assert (cfg.tier_of("light"), cfg.tier_of("streaming"), cfg.tier_of("hard")) == (
        "fast",
        "balanced",
        "powerful",
    )
    recommended = {(b.provider_name, b.tier): n for n, b in cfg.brains.items() if b.recommended}
    assert recommended[("anthropic", "balanced")] == "claude-sonnet"
    assert recommended[("openai", "balanced")] == "gpt-sol"
    for provider in ("anthropic", "openai"):
        assert {t for p, t in recommended if p == provider} == {"fast", "balanced", "powerful"}
    # at most one recommended brain per provider and tier
    pairs = [(b.provider_name, b.tier) for b in cfg.brains.values() if b.recommended]
    assert len(pairs) == len(set(pairs))
