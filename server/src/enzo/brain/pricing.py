"""Per-call cost from published prices (spec §1.3.5). Unknown is ``None``, never 0."""

from pydantic import BaseModel, ConfigDict, Field

from enzo.brain.models import Usage

PER_MILLION = 1_000_000


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Rates(_Strict):
    """USD per 1M tokens. A missing rate means the provider publishes none: cost becomes unknown."""

    input: float = Field(ge=0)
    output: float = Field(ge=0)
    cached_input: float | None = Field(default=None, ge=0)
    #: By cache retention, e.g. {"5m": 5.0, "1h": 8.0}; providers price each retention differently.
    cache_write: dict[str, float] = Field(default_factory=dict)


class LongContext(_Strict):
    """Higher rates once a prompt reaches ``from_input_tokens`` (inclusive)."""

    from_input_tokens: int = Field(gt=0)
    rates: Rates


class Pricing(_Strict):
    #: Where and when the numbers were read, so a later check can reach the evidence.
    source: str = Field(min_length=1)
    rates: Rates
    long_context: LongContext | None = None


def cost_usd(usage: Usage, pricing: Pricing | None) -> float | None:
    """Cost of one call, or ``None`` when any part of it has no published price."""
    if pricing is None:
        return None
    long = pricing.long_context
    rates = long.rates if long and usage.input_tokens >= long.from_input_tokens else pricing.rates
    uncached = usage.input_tokens - usage.cached_input_tokens - usage.cache_write_input_tokens
    total = uncached * rates.input + usage.output_tokens * rates.output
    if usage.cached_input_tokens:
        if rates.cached_input is None:
            return None
        total += usage.cached_input_tokens * rates.cached_input
    if usage.cache_write_input_tokens:
        writes = usage.cache_write_breakdown
        if not writes:
            # No retention reported: only unambiguous if the brain has exactly one write price.
            if len(rates.cache_write) != 1:
                return None
            writes = {next(iter(rates.cache_write)): usage.cache_write_input_tokens}
        for retention, tokens in writes.items():
            rate = rates.cache_write.get(retention)
            if rate is None:
                return None
            total += tokens * rate
    return total / PER_MILLION
