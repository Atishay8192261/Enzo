import asyncio
import logging
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from enzo.brain import (
    AuthError,
    BadRequestError,
    BrainError,
    Message,
    ProviderServerError,
    RateLimitError,
    StreamDone,
    TextDelta,
    TextPart,
    ToolCall,
    ToolChoice,
    ToolSpec,
)
from enzo.brain.config import BrainConfig, BrainsConfig, load_config
from enzo.brain.fake import ScriptedBrain
from enzo.brain.models import StreamEvent
from enzo.brain.provider import BrainProvider
from enzo.brain.router import (
    AllBrainsFailedError,
    InMemoryLedger,
    NoBrainAvailableError,
    PromptOverBudgetError,
    RoutedDone,
    Router,
    classify,
)

NOW = datetime(2026, 11, 17, tzinfo=UTC)
PRICED = {"source": "t", "rates": {"input": 1.0, "output": 2.0}}


def brain_cfg(
    model: str,
    pricing: dict[str, Any] | None = None,
    *,
    tier: str = "powerful",
    provider: str = "anthropic",
    recommended: bool = False,
) -> dict[str, Any]:
    api = "anthropic" if provider == "anthropic" else "openai-responses"
    return {
        "api": api,
        "provider": provider,
        "model": model,
        "api_key_env": "KEY",
        "pricing": pricing,
        "tier": tier,
        "recommended": recommended,
    }


def cfg(*, with_backup: bool = True) -> BrainsConfig:
    """main (anthropic, powerful) falls back to backup (another provider, same tier)."""
    brains = {
        "main": brain_cfg("m-main", PRICED, recommended=True),
        "cheap": brain_cfg("m-cheap", tier="fast"),
    }
    if with_backup:
        brains["backup"] = brain_cfg("m-backup", provider="other")
    return BrainsConfig.model_validate(
        {"brains": brains, "routing": {"tasks": {"light": "fast", "hard": "powerful"}}}
    )


def router(
    brains: dict[str, BrainProvider | Exception],
    config: BrainsConfig | None = None,
    *,
    connected: set[str] | None = None,
) -> Router:
    def build(c: BrainConfig) -> BrainProvider:
        made = brains[c.model]
        if isinstance(made, Exception):
            raise made
        return made

    return Router(
        config or cfg(),
        build=build,
        available=lambda c: connected is None or c.provider_name in connected,
        ledger=InMemoryLedger(),
        clock=lambda: NOW,
    )


def ok(text: str = "hi") -> ScriptedBrain:
    return ScriptedBrain([Message.assistant(text)])


def failing(exc: BrainError) -> ScriptedBrain:
    return ScriptedBrain([exc])


def err(kind: type[BrainError], model: str = "m-main") -> BrainError:
    return kind("boom", provider="fake", model=model)


def records(r: Router) -> list[tuple[str, str]]:
    assert isinstance(r.ledger, InMemoryLedger)
    return [(rec.brain, rec.outcome) for rec in r.ledger.records]


def test_plan_follows_configuration_and_user_choice(caplog: pytest.LogCaptureFixture) -> None:
    r = router({})
    with caplog.at_level(logging.INFO, logger="enzo.brain.router"):
        light = r.plan("light")
        assert (light.tier, light.primary) == ("fast", "cheap")
        assert light.reason == "tier fast (task light); providers in order: anthropic, other"
        assert (r.plan("hard").primary, r.plan("hard").fallback) == ("main", "backup")
        chosen = r.plan("light", override="main")
        assert (chosen.primary, chosen.fallback, chosen.reason) == (
            "main",
            "backup",  # the other provider's brain of the same tier
            "selected by user",
        )
    assert any(rec.msg == "route" for rec in caplog.records)  # routing is visible in logs
    with pytest.raises(KeyError):
        r.plan("hard", override="ghost")


async def test_success_is_recorded_with_tokens_and_cost() -> None:
    r = router({"m-main": ok()})
    routed = await r.chat("hard", [Message.user("x")], max_output_tokens=10)
    assert (routed.brain, routed.response.text) == ("main", "hi")
    (rec,) = r.ledger.records  # type: ignore[attr-defined]
    assert (rec.outcome, rec.attempt, rec.fallback_from, rec.started_at) == ("ok", 1, None, NOW)
    assert rec.cost_usd == pytest.approx((rec.input_tokens * 1.0 + rec.output_tokens * 2.0) / 1e6)


async def test_unpriced_brain_records_unknown_cost() -> None:
    r = router({"m-backup": ok()})
    routed = await r.chat("hard", [Message.user("x")], max_output_tokens=10, override="backup")
    assert routed.attempts[0].cost_usd is None


class PaidByPlan(ScriptedBrain):
    """Replies like a ChatGPT plan sign-in: billed to the plan, not per token."""

    async def stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[StreamEvent]:
        async for event in super().stream(*args, **kwargs):
            if isinstance(event, StreamDone):
                billed = event.response.model_copy(update={"billing": "plan"})
                yield StreamDone(response=billed)
            else:
                yield event


async def test_a_plan_paid_reply_has_no_per_token_cost() -> None:
    r = router({"m-main": PaidByPlan([Message.assistant("hi")])})
    routed = await r.chat("hard", [Message.user("x")], max_output_tokens=10)
    (rec,) = routed.attempts
    assert (rec.billing, rec.cost_usd) == ("plan", None)  # priced brain, yet the plan pays
    assert rec.input_tokens > 0  # usage is still recorded


async def test_transient_failure_falls_back_exactly_once_and_is_recorded() -> None:
    backup = ok("from backup")
    r = router({"m-main": failing(err(RateLimitError)), "m-backup": backup})
    routed = await r.chat("hard", [Message.user("x")], max_output_tokens=10)
    assert (routed.brain, routed.response.text) == ("backup", "from backup")
    assert records(r) == [("main", "rate_limit"), ("backup", "ok")]
    assert routed.attempts[1].fallback_from == "main"  # the swap is never silent
    assert backup.calls[0].messages == (Message.user("x"),)  # same canonical prompt


@pytest.mark.parametrize("kind", [AuthError, BadRequestError])
async def test_non_transient_failures_never_fall_back(kind: type[BrainError]) -> None:
    backup = ok()
    r = router({"m-main": failing(err(kind)), "m-backup": backup})
    with pytest.raises(kind):
        await r.chat("hard", [Message.user("x")], max_output_tokens=10)
    assert backup.calls == []


async def test_both_failing_stops_and_reports_what_was_tried() -> None:
    r = router(
        {
            "m-main": failing(err(RateLimitError)),
            "m-backup": failing(err(ProviderServerError, "m-backup")),
        }
    )
    with pytest.raises(AllBrainsFailedError) as exc:
        await r.chat("hard", [Message.user("x")], max_output_tokens=10)
    assert [a.brain for a in exc.value.attempts] == ["main", "backup"]
    assert "main (fake/m-main): rate_limit" in str(exc.value)
    assert len(records(r)) == 2  # no third attempt, no loop


async def test_without_a_fallback_the_original_error_surfaces() -> None:
    r = router({"m-main": failing(err(RateLimitError))}, cfg(with_backup=False))
    with pytest.raises(RateLimitError):
        await r.chat("hard", [Message.user("x")], max_output_tokens=10)


async def test_fallback_brain_without_a_key_is_still_reported() -> None:
    no_key = AuthError("no API key: set KEY", provider="anthropic", model="m-backup")
    r = router({"m-main": failing(err(RateLimitError)), "m-backup": no_key})
    with pytest.raises(AllBrainsFailedError, match="set KEY"):
        await r.chat("hard", [Message.user("x")], max_output_tokens=10)
    assert records(r) == [("main", "rate_limit"), ("backup", "auth")]


class TalksThenFails(ScriptedBrain):
    async def stream(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSpec] = (),
        tool_choice: ToolChoice = "auto",
        max_output_tokens: int,
    ) -> AsyncIterator[StreamEvent]:
        yield TextDelta(text="half an answer")
        raise err(ProviderServerError)


async def test_no_fallback_once_text_reached_the_user() -> None:
    backup = ok()
    r = router({"m-main": TalksThenFails([]), "m-backup": backup})
    seen: list[Any] = []

    async def drain() -> None:
        async for event in r.stream("hard", [Message.user("x")], max_output_tokens=10):
            seen.append(event)

    with pytest.raises(ProviderServerError):
        await drain()
    assert [e.text for e in seen] == ["half an answer"]
    assert backup.calls == []


class CutOffOnPlan(TalksThenFails):
    """A ChatGPT plan reply cut off half-way, as by a Wi-Fi drop (#43)."""

    billing = "plan"


class StoppedOnPlan(ScriptedBrain):
    """A ChatGPT plan reply still streaming when the user stops it."""

    billing = "plan"

    async def stream(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSpec] = (),
        tool_choice: ToolChoice = "auto",
        max_output_tokens: int,
    ) -> AsyncIterator[StreamEvent]:
        yield TextDelta(text="thinking")
        await asyncio.Event().wait()


async def test_a_failed_or_stopped_plan_call_is_still_the_plans() -> None:
    """#43: a cut-off ChatGPT plan reply was recorded as per-token use of unknown cost, so the
    whole chat then read "ChatGPT plan + cost unknown"."""
    r = router({"m-main": CutOffOnPlan([])})
    with pytest.raises(ProviderServerError):
        async for _ in r.stream("hard", [Message.user("x")], max_output_tokens=10):
            pass
    r2 = router({"m-main": StoppedOnPlan([])})
    started = asyncio.Event()

    async def drain() -> None:
        async for _ in r2.stream("hard", [Message.user("x")], max_output_tokens=10):
            started.set()

    task = asyncio.create_task(drain())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    r3 = router({"m-main": failing(err(ProviderServerError))}, cfg(with_backup=False))
    with pytest.raises(ProviderServerError):
        await r3.chat("hard", [Message.user("x")], max_output_tokens=10)
    rows = [rec for x in (r, r2, r3) for rec in x.ledger.records]  # type: ignore[attr-defined]
    assert [(rec.outcome, rec.billing, rec.cost_usd) for rec in rows] == [
        ("server_error", "plan", None),
        ("cancelled", "plan", None),
        ("server_error", "usage", None),  # on an API key: unknown, as it should be
    ]


class EndsSilently(ScriptedBrain):
    async def stream(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSpec] = (),
        tool_choice: ToolChoice = "auto",
        max_output_tokens: int,
    ) -> AsyncIterator[StreamEvent]:
        return
        yield  # pragma: no cover


async def test_a_stream_without_a_final_response_is_a_recorded_failure() -> None:
    r = router({"m-main": EndsSilently([]), "m-backup": ok()})
    routed = await r.chat("hard", [Message.user("x")], max_output_tokens=10)
    assert routed.brain == "backup"
    assert records(r) == [("main", "server_error"), ("backup", "ok")]


async def test_budget_is_checked_before_the_brain_is_called() -> None:
    main = ok()
    r = router({"m-main": main})
    prompt = await main.count_tokens([Message.user("x")])
    with pytest.raises(PromptOverBudgetError) as exc:
        await r.chat("hard", [Message.user("x")], max_output_tokens=100, budget_tokens=prompt + 99)
    assert (exc.value.prompt_tokens, exc.value.budget) == (prompt, prompt + 99)
    assert main.calls == []
    routed = await r.chat(
        "hard", [Message.user("x")], max_output_tokens=100, budget_tokens=prompt + 100
    )
    assert routed.response.text == "hi"


async def test_stream_ends_with_routed_done() -> None:
    r = router({"m-main": ScriptedBrain([Message.assistant("a b")])})
    events = [e async for e in r.stream("hard", [Message.user("x")], max_output_tokens=10)]
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["a", "b"]
    assert isinstance(events[-1], RoutedDone)


def test_forget_rebuilds_only_that_providers_brains() -> None:
    config = BrainsConfig.model_validate(
        {
            "brains": {
                "a1": brain_cfg("m-a1"),
                "a2": brain_cfg("m-a2"),
                "x": brain_cfg("m-x", provider="xai"),
            },
        }
    )
    built: list[str] = []

    def build(c: BrainConfig) -> BrainProvider:
        built.append(c.model)
        return ok()

    r = Router(config, build=build, available=lambda _c: True, ledger=InMemoryLedger())
    first = [r.brain(n) for n in ("a1", "a2", "x")]
    r.forget("anthropic")
    second = [r.brain(n) for n in ("a1", "a2", "x")]
    assert built == ["m-a1", "m-a2", "m-x", "m-a1", "m-a2"]
    assert second[2] is first[2]
    assert second[0] is not first[0]
    r.forget("nobody")  # unknown provider: nothing to drop


def three_providers() -> BrainsConfig:
    return BrainsConfig.model_validate(
        {
            "brains": {
                "sonnet": brain_cfg("s", tier="balanced", recommended=True),
                "opus": brain_cfg("o", recommended=True),
                "sol": brain_cfg("g", tier="balanced", provider="openai", recommended=True),
                "astra": brain_cfg("a", provider="openai", recommended=True),
                "grok": brain_cfg("x", provider="xai", recommended=True),
            },
            "routing": {"tasks": {"streaming": "balanced", "hard": "powerful"}},
        }
    )


def test_conversations_spread_evenly_and_each_stays_with_one_provider() -> None:
    r = router({}, three_providers())
    picks = [r.plan("streaming", affinity=f"conversation-{i}").primary for i in range(300)]
    counts = {name: picks.count(name) for name in set(picks)}
    assert set(counts) == {"sonnet", "sol", "grok"}  # every connected provider gets work
    assert all(70 <= n <= 130 for n in counts.values()), counts  # no provider is favoured
    for i in range(20):  # sticky: same conversation, same provider, for every tier
        streaming = r.plan("streaming", affinity=f"c{i}")
        hard = r.plan("hard", affinity=f"c{i}")
        assert streaming == r.plan("streaming", affinity=f"c{i}")
        provider = r.config.brains[streaming.primary].provider_name
        assert r.config.brains[hard.primary].provider_name == provider


def test_fallback_is_another_provider_of_the_same_tier() -> None:
    r = router({}, three_providers())
    brains = r.config.brains
    for i in range(20):
        plan = r.plan("hard", affinity=f"c{i}")
        assert plan.fallback is not None
        assert brains[plan.fallback].provider_name != brains[plan.primary].provider_name
        assert brains[plan.fallback].tier == "powerful"


def test_only_connected_providers_are_used() -> None:
    r = router({}, three_providers(), connected={"xai"})
    plan = r.plan("streaming", affinity="c1")
    assert (plan.primary, plan.fallback) == ("grok", None)
    # xAI has no balanced model: the nearest stronger tier is used, and the log says so
    assert plan.reason.endswith("xai has no balanced brain, using powerful")
    assert r.available("grok")
    assert not r.available("sonnet")


def test_nothing_connected_says_how_to_connect() -> None:
    r = router({}, three_providers(), connected=set())
    with pytest.raises(NoBrainAvailableError, match=r"/login anthropic, /login openai, /login xai"):
        r.plan("streaming", affinity="c1")


def test_a_tier_can_be_chosen_instead_of_a_model() -> None:
    r = router({}, three_providers(), connected={"anthropic"})
    plan = r.plan("streaming", override="powerful")
    assert (plan.tier, plan.primary) == ("powerful", "opus")
    assert plan.reason.startswith("tier powerful (selected by user)")


def test_the_recommended_brain_of_a_tier_comes_first() -> None:
    config = BrainsConfig.model_validate(
        {"brains": {"older": brain_cfg("k3"), "newer": brain_cfg("k31", recommended=True)}}
    )
    assert router({}, config).plan("hard").primary == "newer"


def test_weaker_tiers_are_used_only_when_nothing_stronger_exists() -> None:
    config = BrainsConfig.model_validate(
        {
            "brains": {
                "quick": brain_cfg("q", tier="fast"),
                "mid": brain_cfg("m", tier="balanced"),
            },
            "routing": {"tasks": {"light": "fast", "hard": "powerful"}},
        }
    )
    r = router({}, config)
    assert r.plan("hard").primary == "mid"  # powerful missing: balanced beats fast
    assert r.plan("light").primary == "quick"


def user(*texts: str) -> Message:
    return Message(role="user", parts=tuple(TextPart(text=t) for t in texts))


def test_classify_reads_only_the_users_words_of_the_latest_message() -> None:
    config = cfg()
    limit = config.routing.classify.long_message_chars
    short, long = "hi", "x" * (limit + 1)
    assert classify(config, [user(short)])[0] == "streaming"
    task, why = classify(config, [user(short), Message.assistant("ok"), user(long)])
    assert task == "hard"
    assert why == f"message of {limit + 1} characters > {limit}: long-document reasoning"
    # context and memory ride after the user's words (ADR 0007) and never count
    assert classify(config, [user(short, "y" * (limit * 2))])[0] == "streaming"
    # a resumed turn (ending with the assistant's tool call) classifies like the turn it continues
    assert classify(config, [user(long), Message.assistant("calling")])[0] == "hard"
    assert classify(config, [user("x" * limit)])[0] == "streaming"  # the limit itself is not long


def test_a_weaker_brain_is_never_chosen_while_a_same_tier_one_exists() -> None:
    config = BrainsConfig.model_validate(
        {
            "brains": {
                "opus": brain_cfg("o"),
                "sol": brain_cfg("g", tier="balanced", provider="openai"),
                "grok": brain_cfg("x", provider="xai"),
            },
            "routing": {"tasks": {"hard": "powerful"}},
        }
    )
    r = router({}, config)
    for i in range(30):  # whichever provider the conversation prefers
        plan = r.plan("hard", affinity=f"c{i}")
        assert {plan.primary, plan.fallback} == {"opus", "grok"}  # never sol for a hard task


def test_a_brain_without_a_key_is_skipped_even_if_its_provider_has_others() -> None:
    config = BrainsConfig.model_validate(
        {
            "brains": {
                "opus": brain_cfg("o"),
                "sonnet": brain_cfg("s", tier="balanced"),
            },
            "routing": {"tasks": {"hard": "powerful"}},
        }
    )
    r = Router(config, build=lambda _c: ok(), available=lambda c: c.model != "o")
    assert r.plan("hard").primary == "sonnet"


def test_the_connect_hint_follows_catalog_order_not_the_alphabet() -> None:
    """Regression (#25): the default models' providers come first."""
    r = router({}, load_config(), connected=set())
    with pytest.raises(NoBrainAvailableError) as exc:
        r.plan("streaming")
    assert str(exc.value).endswith(
        "Connect one: /login anthropic, /login openai, /login xai, /login moonshot"
    )


class SlowToSpeak(ScriptedBrain):
    """Thinks before its first word, then streams the rest."""

    async def stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[StreamEvent]:
        await asyncio.sleep(0.05)
        async for event in super().stream(*args, **kwargs):
            yield event
            await asyncio.sleep(0.03)


async def test_time_to_first_token_is_recorded_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Spec §13.2: time to first token and to completion, per call."""
    r = router({"m-main": SlowToSpeak([Message.assistant("one two three")])})
    with caplog.at_level(logging.INFO, logger="enzo.brain.router"):
        routed = await r.chat("hard", [Message.user("x")], max_output_tokens=10)
    (rec,) = routed.attempts
    assert rec.first_token_ms is not None
    assert 50 <= rec.first_token_ms < 80  # the first word, not the whole reply (~140 ms)
    (line,) = [x for x in caplog.records if x.msg == "brain.call"]
    assert line.__dict__["ttft_ms"] == round(rec.first_token_ms)


async def test_a_reply_without_text_has_no_first_token(caplog: pytest.LogCaptureFixture) -> None:
    call = ToolCall(id="c1", name="step", arguments_json='{"value": "x"}')
    r = router({"m-main": ScriptedBrain([Message.assistant(tool_calls=(call,))])})
    with caplog.at_level(logging.INFO, logger="enzo.brain.router"):
        routed = await r.chat("hard", [Message.user("x")], max_output_tokens=10)
    assert routed.attempts[0].first_token_ms is None
    (line,) = [x for x in caplog.records if x.msg == "brain.call"]
    assert "ttft_ms" not in line.__dict__
