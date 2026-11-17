import pytest

from enzo.brain import AuthError
from enzo.brain.adapters.anthropic_brain import AnthropicBrain
from enzo.brain.adapters.openai_brain import OpenAIBrain
from enzo.brain.config import load_config
from enzo.brain.factory import build_brain

CFG = load_config()


def test_missing_key_names_the_variable_to_set() -> None:
    with pytest.raises(AuthError, match="set ANTHROPIC_API_KEY"):
        build_brain(CFG.brains["claude-opus"], env={})


def test_builds_a_claude_brain_with_retries_off() -> None:
    brain = build_brain(CFG.brains["claude-opus"], env={"ANTHROPIC_API_KEY": "k"})
    assert isinstance(brain, AnthropicBrain)
    assert (brain.model, brain._client.max_retries, brain._effort) == (
        "claude-opus-5-5",
        0,
        "medium",
    )


def test_openai_compatible_providers_keep_their_base_url_and_name() -> None:
    brain = build_brain(CFG.brains["grok"], env={"XAI_API_KEY": "k"})
    assert isinstance(brain, OpenAIBrain)
    assert (brain.provider, brain.model) == ("xai", "grok-4.7")
    assert str(brain._client.base_url).rstrip("/") == "https://api.x.ai/v1"
    assert brain._client.max_retries == 0


def test_a_connected_account_wins_over_the_environment() -> None:
    keys = {"anthropic": "sk-from-vault"}
    brain = build_brain(
        CFG.brains["claude-opus"], env={"ANTHROPIC_API_KEY": "sk-from-env"}, keys=keys.get
    )
    assert isinstance(brain, AnthropicBrain)
    assert brain._client.api_key == "sk-from-vault"


def test_the_environment_is_the_fallback_when_not_connected() -> None:
    brain = build_brain(CFG.brains["grok"], env={"XAI_API_KEY": "xai-env"}, keys=lambda _p: None)
    assert isinstance(brain, OpenAIBrain)
    assert brain._client.api_key == "xai-env"


def test_missing_key_says_how_to_connect() -> None:
    with pytest.raises(
        AuthError, match=r"xai is not connected: run /login xai \(or set XAI_API_KEY\)"
    ):
        build_brain(CFG.brains["grok"], env={}, keys=lambda _p: None)
