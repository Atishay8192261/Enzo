"""The context window inside the agent loop: count before every call, compact when over budget."""

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict

from enzo.agent.loop import AgentEvent, TurnDone, TurnOutcome, run_turn
from enzo.brain.config import BrainConfig, BrainsConfig
from enzo.brain.errors import ProviderServerError
from enzo.brain.models import Message, NativePart, TextPart, ToolCall
from enzo.brain.provider import BrainProvider
from enzo.brain.router import Router
from enzo.brain.tokens import TokenCounter
from enzo.compaction import Compacted, ContextWindow
from enzo.compaction.view import split_lead
from enzo.tools import ToolRegistry, tool
from tests.compaction.fakes import Request, RuleBrain, config

SYSTEM = Message.system("You are Enzo.")


def brains(**kw: Any) -> dict[str, RuleBrain]:
    return {
        "main-1": RuleBrain("main", **kw.pop("main", {})),
        "quick-1": RuleBrain("quick", **kw.pop("quick", {})),
        "other-1": RuleBrain("other", provider="openai"),
    }


def make_router(
    cfg: BrainsConfig, fakes: dict[str, RuleBrain], available: Callable[[str], bool] | None = None
) -> Router:
    def build(c: BrainConfig) -> BrainProvider:
        return fakes[c.model]

    names = {c.model: n for n, c in cfg.brains.items()}
    return Router(
        cfg,
        build=build,
        available=lambda c: (available or (lambda n: n != "other"))(names[c.model]),
    )


def turn(n: int, words: str | None = None, *, size: int = 200) -> list[Message]:
    user = Message(
        role="user",
        parts=(TextPart(text=words or f"message {n} " + "x" * size), TextPart(text="[sent]")),
    )
    reply = Message(
        role="assistant",
        parts=(
            NativePart(provider="anthropic", payload={"thinking": f"old{n}"}),
            TextPart(text=f"reply {n} " + "y" * size),
        ),
    )
    return [user, reply]


def chat(turns: int, *, special: dict[int, str] | None = None) -> list[Message]:
    out = [SYSTEM]
    for n in range(1, turns + 1):
        out += turn(n, (special or {}).get(n))
    return out


async def run(
    history: list[Message],
    *,
    cfg: BrainsConfig | None = None,
    fakes: dict[str, RuleBrain] | None = None,
    registry: ToolRegistry | None = None,
    window: ContextWindow | None = None,
    **kw: Any,
) -> tuple[list[AgentEvent], TurnOutcome, ContextWindow, dict[str, RuleBrain]]:
    cfg = cfg or config()
    fakes = fakes or brains()
    router = make_router(cfg, fakes, kw.pop("available", None))
    window = window or ContextWindow(router, TokenCounter(cfg))
    events = [
        e
        async for e in run_turn(
            router,
            registry or ToolRegistry(),
            history,
            max_output_tokens=200,
            window=window,
            affinity="c1",
            **kw,
        )
    ]
    done = events[-1]
    assert isinstance(done, TurnDone)
    return events, done.outcome, window, fakes


def main_requests(fakes: dict[str, RuleBrain]) -> list[Request]:
    return [r for r in fakes["main-1"].requests if not r.summarizing]


async def test_a_short_chat_is_sent_as_it_is() -> None:
    history = [*chat(3), Message.user("hello")]
    events, out, window, fakes = await run(history)
    assert not any(isinstance(e, Compacted) for e in events)
    assert list(main_requests(fakes)[0].messages) == history
    assert fakes["quick-1"].requests == []
    assert window.made == []
    assert out.stop == "final_answer"


async def test_a_turn_over_the_trigger_is_summarized_first() -> None:
    """Spec §5.2: count before the call; over budget, older turns are summarized; the newest
    turns stay word for word; the transcript itself stays complete."""
    history = [*chat(24, special={2: "We decide to fly to Lisbon on Friday the 12th."})]
    history.append(Message.user("what did we decide?"))
    events, out, window, fakes = await run(history)

    compacted = [e for e in events if isinstance(e, Compacted)]
    assert len(compacted) == 1
    assert compacted[0].failed is None
    assert compacted[0].after < compacted[0].before
    # The conversation's own provider's fast brain wrote it (in as many calls as its small
    # window needed); the other provider never saw it.
    assert fakes["quick-1"].requests
    assert all(r.summarizing for r in fakes["quick-1"].requests)
    assert fakes["other-1"].requests == []
    sent = main_requests(fakes)[0].messages
    lead, rest = split_lead(sent)
    assert lead == [SYSTEM]
    summary = rest[0].text
    assert summary.startswith("[Summary of turns 1 to ")
    assert "We decide to fly to Lisbon on Friday the 12th." in summary  # turn 2's decision
    # Recent turns verbatim, without the replay data made before the summary existed.
    record = window.made[0]
    kept = rest[1:]
    assert kept[0].text.startswith(f"message {record.turns + 1} ")
    assert [m.text for m in kept if m.role == "assistant"] == [
        f"reply {n} " + "y" * 200 for n in range(record.turns + 1, 25)
    ]
    assert not any(m.native("anthropic") for m in kept)
    assert kept[-1] == history[-1]
    # Nothing was cut from what is saved.
    assert out.messages[: len(history)] == tuple(history)
    # The summary calls are on the turn's ledger, as "compact", on the fast brain.
    summaries = len(fakes["quick-1"].requests)
    assert [(c.brain, c.task) for c in out.brain_calls] == [
        *[("quick", "compact")] * summaries,
        ("main", "streaming"),
    ]


async def test_replay_data_made_after_a_summary_is_kept_on_later_calls() -> None:
    """Messages produced with the summary in the prompt keep their reasoning: it is valid."""
    history = [*chat(24), Message.user("next")]
    _, out, window, fakes = await run(history)
    later = [*out.messages, Message.user("and again")]
    _, _, _, fakes2 = await run(later, fakes=fakes, window=window)
    sent = main_requests(fakes2)[-1].messages
    assert sent[-2].native("anthropic") is not None  # the reply made after the summary
    assert all(m.native("anthropic") is None for m in sent[2:-3])  # older kept ones: stripped
    # The prompt is the earlier one plus the new messages: append-only, so caches stay warm.
    first = main_requests(fakes)[0].messages
    assert sent[: len(first)] == first


async def test_mid_turn_only_the_hard_limit_compacts() -> None:
    """A tool result may take the prompt past the trigger mid-turn without a summary (one cache
    reset per turn at most); only past the hard limit is it compacted mid-turn."""

    class Args(BaseModel):
        model_config = ConfigDict(extra="forbid")
        size: int

    class Out(BaseModel):
        text: str

    @tool(name="big", description="Return text.")
    async def big(args: Args) -> Out:
        return Out(text="z" * args.size)

    def calls(size: int) -> Message:
        return Message.assistant(
            tool_calls=(ToolCall(id=f"b{size}", name="big", arguments_json=f'{{"size": {size}}}'),)
        )

    history = [*chat(10), Message.user("fetch it")]  # ~730 tokens: under the trigger
    soft = 1_000 * 4  # pushes past compact_at (1,000) but stays under the limit (2,800)
    hard = 2_200 * 4
    for size, compacts in ((soft, False), (hard, True)):
        fakes = brains(main={"script": [calls(size)]})
        events, out, _, _ = await run(history, fakes=fakes, registry=ToolRegistry([big]))
        assert any(isinstance(e, Compacted) for e in events) is compacts, size
        assert out.stop == "final_answer"
        if compacts:  # the current tool round lost its replay data, nothing else changed
            sent = main_requests(fakes)[-1].messages
            assert sent[-1].role == "tool"
            assert sent[-2].native("anthropic") is None


async def test_a_failed_summary_falls_back_to_the_whole_chat_when_it_fits() -> None:
    history = [*chat(24), Message.user("next")]
    fakes = brains(
        quick={
            "summary_errors": [ProviderServerError("down", provider="anthropic", model="quick-1")]
        }
    )
    events, out, window, _ = await run(history, fakes=fakes)
    compacted = [e for e in events if isinstance(e, Compacted)]
    assert len(compacted) == 1
    assert compacted[0].failed == "the model could not be reached"
    assert list(main_requests(fakes)[0].messages) == history  # it fit, so it went whole
    assert window.made == []
    assert [c.outcome for c in out.brain_calls] == ["server_error", "ok"]  # both on record


async def test_a_summary_the_model_did_not_write_is_a_failure_not_a_guess() -> None:
    history = [*chat(24), Message.user("next")]
    fakes = brains(quick={"refuses_summary": True})
    events, _, window, _ = await run(history, fakes=fakes)
    assert [e.failed for e in events if isinstance(e, Compacted)] == [
        "the summary could not be written"
    ]
    assert window.made == []


async def test_nothing_oversized_is_ever_sent() -> None:
    """Spec §1.3.5: over the hard limit and unable to compact, the turn stops honestly."""
    huge = [*chat(40), Message.user("next")]
    fakes = brains(
        quick={
            "summary_errors": [ProviderServerError("down", provider="anthropic", model="quick-1")]
        }
    )
    _, out, _, _ = await run(huge, fakes=fakes)
    assert out.stop == "over_budget"
    assert "Nothing was sent" in out.text
    assert "about " in out.text
    assert main_requests(fakes) == []

    alone = [SYSTEM, Message.user("w" * 20_000)]  # nothing older to summarize
    fakes = brains()
    _, out, _, _ = await run(alone, fakes=fakes)
    assert out.stop == "over_budget"
    assert main_requests(fakes) == []
    assert fakes["quick-1"].requests == []


async def test_the_summary_stays_with_the_conversations_provider() -> None:
    """A provider without a fast brain summarizes with the conversation's own brain; another
    provider is never asked, even when it is connected and has one."""
    cfg = config()
    raw = cfg.model_dump()
    raw["brains"]["quick"]["api"] = "openai-responses"  # the only fast brain is elsewhere
    cfg = BrainsConfig.model_validate(raw)
    fakes = brains()
    history = [*chat(24), Message.user("next")]
    events, out, _, _ = await run(
        history, cfg=cfg, fakes=fakes, available=lambda n: True, override="main"
    )
    assert any(isinstance(e, Compacted) and e.failed is None for e in events)
    asked = [r.summarizing for r in fakes["main-1"].requests]
    assert asked[-1] is False
    assert all(asked[:-1])
    assert len(asked) > 1
    assert fakes["quick-1"].requests == []
    assert fakes["other-1"].requests == []
    assert out.brain_calls[0].brain == "main"
    assert out.brain_calls[0].task == "compact"


async def test_the_provider_counts_exactly_only_near_a_limit() -> None:
    fakes = brains(main={"exact": True})
    await run([*chat(2), Message.user("hi")], fakes=fakes)
    assert fakes["main-1"].counted == 0  # far from any limit: the local estimate only
    fakes = brains(main={"exact": True})
    await run([*chat(13), Message.user("hi")], fakes=fakes)  # ~950 of 1,000
    assert fakes["main-1"].counted >= 1


async def test_usage_reports_sharpen_the_next_estimate() -> None:
    """A brain whose prompts are really twice the estimate gets compacted earlier next time."""
    cfg = config()
    fakes = brains(main={"true_ratio": 2.0})
    router = make_router(cfg, fakes)
    counter = TokenCounter(cfg)
    history = [*chat(10), Message.user("one")]  # ~730 estimated: under the trigger
    first = ContextWindow(router, counter)
    events = [
        e
        async for e in run_turn(
            router, ToolRegistry(), history, window=first, max_output_tokens=200
        )
    ]
    assert not any(isinstance(e, Compacted) for e in events)
    done = events[-1]
    assert isinstance(done, TurnDone)
    second = ContextWindow(router, counter)
    again = [*done.outcome.messages, Message.user("two")]
    events = [
        e
        async for e in run_turn(router, ToolRegistry(), again, window=second, max_output_tokens=200)
    ]
    assert any(isinstance(e, Compacted) for e in events)  # 2x the estimate: over the trigger
