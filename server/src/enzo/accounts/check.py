"""Live credential checks: is this key accepted by the provider? (spec §8.2 status check)."""

import anthropic
import httpx2
import openai

from enzo.brain.adapters import anthropic_brain, openai_brain
from enzo.brain.config import BrainConfig
from enzo.brain.errors import (
    AuthError,
    ProviderConnectionError,
    ProviderServerError,
    ProviderTimeoutError,
    RateLimitError,
)


async def check_api_key(
    cfg: BrainConfig, api_key: str, *, http_client: httpx2.AsyncClient | None = None
) -> None:
    """List models with the key: cheap, read-only, and fails exactly like a real call would.

    Raises the canonical errors (AuthError for a rejected key, connection/server errors when the
    provider cannot be reached). One attempt, no retries (ADR 0002).
    """
    if cfg.api == "anthropic":
        a_client = anthropic.AsyncAnthropic(
            api_key=api_key,
            base_url=cfg.base_url,
            max_retries=0,
            http_client=http_client,
        )
        with anthropic_brain.mapped_errors(cfg.model):
            await a_client.models.list(limit=1)
        return
    o_client = openai.AsyncOpenAI(
        api_key=api_key,
        base_url=cfg.base_url,
        max_retries=0,
        http_client=http_client,
    )
    with openai_brain.mapped_errors(cfg.provider_name, cfg.model):
        await o_client.models.list()


TAVILY_USAGE = "https://api.tavily.com/usage"


async def check_tavily_key(api_key: str, *, http_client: httpx2.AsyncClient | None = None) -> None:
    """Ask Tavily for the key's usage: read-only, costs no credits (docs.tavily.com, 2026-11-18).

    Fails like a brain check would: AuthError for a rejected key, connection or server errors when
    Tavily cannot be reached, so the gateway reports both the same way.
    """
    ctx = {"provider": "tavily", "model": "-"}
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        if http_client is None:
            async with httpx2.AsyncClient(timeout=15) as http:
                response = await http.get(TAVILY_USAGE, headers=headers)
        else:
            response = await http_client.get(TAVILY_USAGE, headers=headers)
    except httpx2.TimeoutException as exc:
        raise ProviderTimeoutError("Tavily did not answer in time", **ctx) from exc
    except httpx2.HTTPError as exc:
        raise ProviderConnectionError(
            f"could not reach Tavily ({type(exc).__name__})", **ctx
        ) from exc
    if response.status_code in (401, 403):
        raise AuthError("Tavily rejected this key", **ctx)
    if response.status_code == 429:
        raise RateLimitError(
            "Tavily is rate limiting key checks; try again in a few minutes", **ctx
        )
    if response.status_code != 200:
        raise ProviderServerError(f"Tavily answered HTTP {response.status_code}", **ctx)
