"""Build a live adapter from config. Keys come from the vault or the environment, never files."""

import os
from collections.abc import Callable, Mapping
from typing import cast

import anthropic
import openai

from enzo.brain.adapters.anthropic_brain import AnthropicBrain, Effort
from enzo.brain.adapters.openai_brain import OpenAIBrain
from enzo.brain.config import BrainConfig
from enzo.brain.errors import AuthError
from enzo.brain.provider import BrainProvider

type KeyLookup = Callable[[str], str | None]


def build_brain(
    cfg: BrainConfig, env: Mapping[str, str] | None = None, *, keys: KeyLookup | None = None
) -> BrainProvider:
    """The user's connected account comes first; an environment variable is the fallback."""
    source = os.environ if env is None else env
    key = (keys(cfg.provider_name) if keys else None) or source.get(cfg.api_key_env)
    if not key:
        msg = (
            f"{cfg.provider_name} is not connected: run /login {cfg.provider_name} "
            f"(or set {cfg.api_key_env})"
        )
        raise AuthError(msg, provider=cfg.provider_name, model=cfg.model)
    if cfg.api == "anthropic":
        return AnthropicBrain(
            model=cfg.model,
            client=anthropic.AsyncAnthropic(api_key=key, base_url=cfg.base_url),
            effort=cast("Effort | None", cfg.effort),
            forced_tool_choice=bool(cfg.forced_tool_choice),
        )
    return OpenAIBrain(
        model=cfg.model,
        client=openai.AsyncOpenAI(api_key=key, base_url=cfg.base_url),
        reasoning_effort=cfg.effort,
        embedding_model=cfg.embedding_model,
        forced_tool_choice=cfg.forced_tool_choice is not False,
        provider=cfg.provider_name,
    )
