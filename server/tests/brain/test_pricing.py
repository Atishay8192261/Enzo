import pytest

from enzo.brain import Usage
from enzo.brain.config import load_config
from enzo.brain.pricing import LongContext, Pricing, Rates, cost_usd

RATES = Rates(input=4.0, output=20.0, cached_input=0.2, cache_write={"5m": 5.0, "1h": 8.0})
PRICING = Pricing(source="test", rates=RATES)


def usage(**kw: int | dict[str, int]) -> Usage:
    return Usage.model_validate({"input_tokens": 0, "output_tokens": 0, **kw})


def test_plain_input_and_output() -> None:
    cost = cost_usd(usage(input_tokens=1_000_000, output_tokens=1_000_000), PRICING)
    assert cost == pytest.approx(24.0)


def test_cache_reads_and_writes_use_their_own_rates() -> None:
    u = usage(
        input_tokens=1_000_000,
        output_tokens=0,
        cached_input_tokens=600_000,
        cache_write_input_tokens=300_000,
        cache_write_breakdown={"5m": 200_000, "1h": 100_000},
    )
    # 100k uncached * 4 + 600k * 0.2 + 200k * 5 + 100k * 8, per 1M
    assert cost_usd(u, PRICING) == pytest.approx(0.4 + 0.12 + 1.0 + 0.8)


def test_unreported_retention_uses_the_only_write_price_or_is_unknown() -> None:
    u = usage(input_tokens=100, cache_write_input_tokens=100)
    single = Pricing(source="t", rates=Rates(input=1, output=1, cache_write={"5m": 2.0}))
    assert cost_usd(u, single) == pytest.approx(100 * 2.0 / 1e6)
    assert cost_usd(u, PRICING) is None  # two retentions priced: ambiguous, so unknown


@pytest.mark.parametrize(
    "u",
    [
        usage(input_tokens=10, cached_input_tokens=5),  # no cached price
        usage(input_tokens=10, cache_write_input_tokens=5, cache_write_breakdown={"12h": 5}),
    ],
)
def test_missing_prices_make_cost_unknown_never_zero(u: Usage) -> None:
    pricing = Pricing(source="t", rates=Rates(input=1, output=1, cache_write={"5m": 1.0}))
    assert cost_usd(u, pricing) is None


def test_no_pricing_means_unknown() -> None:
    assert cost_usd(usage(input_tokens=10), None) is None


def test_long_context_rates_start_at_the_threshold() -> None:
    pricing = Pricing(
        source="t",
        rates=Rates(input=1, output=1),
        long_context=LongContext(from_input_tokens=1000, rates=Rates(input=10, output=10)),
    )
    assert cost_usd(usage(input_tokens=999), pricing) == pytest.approx(999 / 1e6)
    assert cost_usd(usage(input_tokens=1000), pricing) == pytest.approx(10_000 / 1e6)


def test_default_config_prices_match_published_numbers() -> None:
    brains = load_config().brains
    opus = brains["claude-opus"].pricing
    astra = brains["gpt-astra"].pricing
    assert opus is not None
    assert astra is not None
    assert cost_usd(usage(input_tokens=1_000_000, output_tokens=1_000_000), opus) == pytest.approx(
        24.0
    )
    long = usage(input_tokens=300_000, output_tokens=0)
    assert cost_usd(long, astra) == pytest.approx(0.3 * 16.0)  # >272K: long-context input rate
