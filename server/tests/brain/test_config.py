import tomllib
from importlib import resources
from typing import Any

import pytest
from pydantic import ValidationError

from enzo.brain.config import BrainsConfig, load_config

BRAIN = {"api": "anthropic", "model": "m", "api_key_env": "KEY"}


def config(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"brains": {"a": BRAIN, "b": BRAIN}, "routing": {"default": "a"}}
    return {**base, **overrides}


def test_default_config_is_valid_and_fully_priced() -> None:
    cfg = load_config()
    assert cfg.routing.default in cfg.brains
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
        (config(routing={"default": "nope"}), "unknown brains: nope"),
        (config(routing={"default": "a", "tasks": {"hard": "zzz"}}), "unknown brains: zzz"),
        (config(routing={"default": "a", "fallback": {"a": "a"}}), "fall back to itself"),
        (config(routing={"default": "a", "tasks": {"bogus": "a"}}), "tasks.bogus"),
        (config(brains={"Bad Name": BRAIN}, routing={"default": "Bad Name"}), "pattern"),
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
