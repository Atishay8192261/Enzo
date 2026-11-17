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
    TextDelta,
    ToolChoice,
    ToolSpec,
)
from enzo.brain.config import BrainConfig, BrainsConfig
from enzo.brain.fake import ScriptedBrain
from enzo.brain.models import StreamEvent
from enzo.brain.provider import BrainProvider
from enzo.brain.router import (
    AllBrainsFailedError,
    InMemoryLedger,
    PromptOverBudgetError,
    RoutedDone,
    Router,
)

NOW = datetime(2026, 11, 17, tzinfo=UTC)
PRICED = {"source": "t", "rates": {"input": 1.0, "output": 2.0}}


def brain_cfg(model: str, pricing: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"api": "anthropic", "model": model, "api_key_env": "KEY", "pricing": pricing}


def cfg(fallback: dict[str, str] | None = None) -> BrainsConfig:
    return BrainsConfig.model_validate(
        {
            "brains": {
                "main": brain_cfg("m-main", PRICED),
                "backup": brain_cfg("m-backup"),
                "cheap": brain_cfg("m-cheap"),
            },
            "routing": {
                "default": "main",
                "tasks": {"light": "cheap"},
                "fallback": {"main": "backup"} if fallback is None else fallback,
            },
        }
    )


def router(
    brains: dict[str, BrainProvider | Exception], config: BrainsConfig | None = None
) -> Router:
    def build(c: BrainConfig) -> BrainProvider:
        made = brains[c.model]
        if isinstance(made, Exception):
            raise made
        return made

    return Router(config or cfg(), build=build, ledger=InMemoryLedger(), clock=lambda: NOW)


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
        assert (r.plan("light").primary, r.plan("light").reason) == ("cheap", "routing.tasks.light")
        assert (r.plan("hard").primary, r.plan("hard").fallback) == ("main", "backup")
        assert r.plan("light", override="main").reason == "selected by user"
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
    r = router({"m-main": failing(err(RateLimitError))}, cfg(fallback={}))
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
                "x": {
                    "api": "openai-responses",
                    "provider": "xai",
                    "model": "m-x",
                    "api_key_env": "K",
                },
            },
            "routing": {"default": "a1"},
        }
    )
    built: list[str] = []

    def build(c: BrainConfig) -> BrainProvider:
        built.append(c.model)
        return ok()

    r = Router(config, build=build, ledger=InMemoryLedger(), clock=lambda: NOW)
    first = [r.brain(n) for n in ("a1", "a2", "x")]
    r.forget("anthropic")
    second = [r.brain(n) for n in ("a1", "a2", "x")]
    assert built == ["m-a1", "m-a2", "m-x", "m-a1", "m-a2"]
    assert second[2] is first[2]
    assert second[0] is not first[0]
    r.forget("nobody")  # unknown provider: nothing to drop
