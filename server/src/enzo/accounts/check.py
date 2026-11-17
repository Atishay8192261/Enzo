"""Live credential checks: is this key accepted by the provider? (spec §8.2 status check)."""

import anthropic
import httpx2
import openai

from enzo.brain.adapters import anthropic_brain, openai_brain
from enzo.brain.config import BrainConfig


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
