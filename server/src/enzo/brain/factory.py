"""Build a live adapter from config. Keys come from the vault or the environment, never files."""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import cast

import anthropic
import openai

from enzo.brain.adapters.anthropic_brain import AnthropicBrain, Effort
from enzo.brain.adapters.openai_brain import OpenAIBrain
from enzo.brain.config import BrainConfig
from enzo.brain.errors import AuthError
from enzo.brain.provider import BrainProvider


@dataclass(frozen=True, slots=True)
class PlanLogin:
    """Signed in with ChatGPT: the user's plan pays, and each request gets a fresh token."""

    access_token: Callable[[], Awaitable[str]]


#: How a provider is paid for: an API key, or a plan sign-in.
type Auth = str | PlanLogin
type KeyLookup = Callable[[str], Auth | None]


def build_brain(
    cfg: BrainConfig, env: Mapping[str, str] | None = None, *, keys: KeyLookup | None = None
) -> BrainProvider:
    """The user's connected account is the source of keys. ``env`` (opt-in, ``ENZO_USE_ENV_KEYS``)
    adds keys from environment variables; it is never read implicitly (issue #24)."""
    key = _key(cfg, env, keys)
    if not key:
        hint = f" (or set {cfg.api_key_env} with ENZO_USE_ENV_KEYS=1)" if env is not None else ""
        msg = f"{cfg.provider_name} is not connected: run /login {cfg.provider_name}{hint}"
        raise AuthError(msg, provider=cfg.provider_name, model=cfg.model)
    if isinstance(key, PlanLogin):
        if cfg.api != "openai-responses" or cfg.provider_name != "openai" or cfg.base_url:
            msg = f"{cfg.provider_name} cannot use a ChatGPT sign-in"
            raise AuthError(msg, provider=cfg.provider_name, model=cfg.model)
        return OpenAIBrain(
            model=cfg.model,
            # The SDK asks for the token before every request, so a renewed one is always used.
            client=openai.AsyncOpenAI(api_key=key.access_token),
            reasoning_effort=cfg.effort,
            forced_tool_choice=cfg.forced_tool_choice is not False,
            provider=cfg.provider_name,
            plan=True,
        )
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


def has_key(
    cfg: BrainConfig, env: Mapping[str, str] | None = None, *, keys: KeyLookup | None = None
) -> bool:
    """Whether ``build_brain`` would find a key for this brain."""
    return bool(_key(cfg, env, keys))


def _key(cfg: BrainConfig, env: Mapping[str, str] | None, keys: KeyLookup | None) -> Auth | None:
    vault = keys(cfg.provider_name) if keys else None
    return vault or (env.get(cfg.api_key_env) if env is not None else None)
