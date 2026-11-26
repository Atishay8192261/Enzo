"""Spec §5.3, end to end: real Postgres, the real service and loop, a rule-based brain.

1. A 50+ turn chat still has a decision from turn 2 in the prompt after compaction.
2. A very large pasted document is answered from the stored file, not a truncated prompt.
3. A correction survives a forced compaction (and the next ones).
"""

import uuid
from pathlib import Path
from typing import Any

import httpx2

from enzo.agent.loop import TurnDone
from enzo.agent.service import ConversationService, TurnMeta
from enzo.brain.config import BrainConfig, BrainsConfig
from enzo.brain.provider import BrainProvider
from enzo.brain.router import Router
from enzo.brain.tokens import estimate_tokens
from enzo.compaction import Budget, Compacted
from enzo.config import Settings
from enzo.gateway.app import create_app
from enzo.store.repository import Store
from enzo.tools import ToolRegistry
from enzo.tools.stored import OutputStore, stored_tools
from tests.compaction.fakes import Request, RuleBrain, config

DECISION = "We decide to fly to Lisbon on Friday the 12th, window seats."
CORRECTION = "Actually, make it 4 people, not 3."


def service(
    store: Store, tmp_path: Path, cfg: BrainsConfig | None = None, **quick: Any
) -> tuple[ConversationService, dict[str, RuleBrain]]:
    cfg = cfg or config()
    fakes = {
        "main-1": RuleBrain("main"),
        "quick-1": RuleBrain("quick", **quick),
        "other-1": RuleBrain("other", provider="openai"),
    }

    def build(c: BrainConfig) -> BrainProvider:
        return fakes[c.model]

    router = Router(cfg, build=build, available=lambda c: c.model != "other-1")
    ctx = cfg.context
    outputs = OutputStore(
        tmp_path / "context",
        tool_inline_chars=ctx.tool_inline_chars,
        paste_inline_chars=ctx.paste_inline_chars,
        excerpt_chars=ctx.excerpt_chars,
    )
    tools = ToolRegistry(stored_tools(outputs), retain=outputs)
    svc = ConversationService(store, router, tools, outputs=outputs, system_prompt="You are Enzo.")
    return svc, fakes


async def say(svc: ConversationService, chat: uuid.UUID, words: str) -> list[Any]:
    events = [e async for e in await svc.send(chat, words, TurnMeta(timezone="UTC"))]
    assert isinstance(events[-1], TurnDone), events[-1]
    return events


def asked(fakes: dict[str, RuleBrain]) -> list[Request]:
    return [r for r in fakes["main-1"].requests if not r.summarizing]


def words(n: int) -> str:
    return f"Message {n}: " + " ".join(f"note{n}-{i}" for i in range(30))


async def test_a_decision_from_turn_2_is_still_in_the_prompt_after_50_turns(
    store: Store, tmp_path: Path
) -> None:
    svc, fakes = service(store, tmp_path)
    chat = await store.create_conversation()
    for n in range(1, 56):
        await say(svc, chat, DECISION if n == 2 else words(n))
    await say(svc, chat, "Remind me what we decided at the start?")

    summaries = await store.compactions(chat)
    assert len(summaries) >= 3  # compacted again and again as the chat grew
    last = asked(fakes)[-1]
    assert DECISION in last.text  # turn 2's decision reached the prompt (read from the request)
    assert "Message 3:" not in last.text  # while the early wording itself is gone
    assert last.text.count(DECISION) == 1
    # Every prompt fit the budget; summaries only ever ran on the conversation's own provider.
    cfg = config()
    limit = Budget.of(cfg.brains["main"], cfg.context, reply=200).limit
    assert all(
        estimate_tokens(r.messages, r.tools, chars_per_token=4.0) <= limit for r in asked(fakes)
    )
    assert fakes["other-1"].requests == []
    # The transcript the user sees is complete and unchanged.
    history = await store.history(chat)
    assert len([m for m in history if m.role == "user"]) == 56
    assert history[2].parts[0].text == DECISION  # type: ignore[union-attr]
    # Each summary call is on the ledger as "compact", paid like any other call.
    tasks = [c.task for c in await store.brain_calls(chat)]
    assert tasks.count("compact") >= len(summaries)


async def test_after_a_summary_the_prompt_only_grows_even_across_a_restart(
    store: Store, tmp_path: Path
) -> None:
    """Caches stay warm between compactions: each prompt starts with the previous one, byte for
    byte, also when the summary comes back from the database after a restart."""
    roomy = config(compact_at=1_500, keep_recent=300)  # room for a few turns after a summary
    svc, fakes = service(store, tmp_path, roomy)
    chat = await store.create_conversation()
    n = 0
    while not await store.compactions(chat):  # until the first summary
        n += 1
        await say(svc, chat, words(n))
    before = asked(fakes)[-1].messages
    again, fakes2 = service(store, tmp_path, roomy)  # a fresh gateway: nothing in memory
    for later in range(3):
        events = await say(again, chat, words(100 + later))
        assert not any(isinstance(e, Compacted) for e in events)
        after = asked(fakes2)[-1].messages
        assert after[: len(before)] == before
        before = after


async def test_a_correction_survives_forced_compactions(store: Store, tmp_path: Path) -> None:
    """The summarizer here never points at corrections: the code keeps them word for word."""
    svc, fakes = service(store, tmp_path, marks_corrections=False)
    chat = await store.create_conversation()
    await say(svc, chat, "Book a table for 3 people on Saturday.")
    await say(svc, chat, CORRECTION)
    for n in range(3, 40):
        await say(svc, chat, words(n))
    assert len(await store.compactions(chat)) >= 2
    last = asked(fakes)[-1].text
    assert f'Turn 2: "{CORRECTION}"' in last
    assert "they override anything earlier" in last
    for summary in await store.compactions(chat):
        assert [c.words for c in summary.summary.corrections] == [CORRECTION]


async def test_a_very_large_paste_is_answered_from_the_stored_file(
    store: Store, tmp_path: Path
) -> None:
    svc, fakes = service(store, tmp_path)
    chat = await store.create_conversation()
    document = "".join(
        f"CLAUSE-{i}: the tenant owes {i} euros on day {i % 28}. " for i in range(2_000)
    )
    events = await say(svc, chat, f"{document}\n\nWhat does CLAUSE-1777 say?")
    done = events[-1]
    assert isinstance(done, TurnDone)
    assert (
        done.outcome.text == "From the document: CLAUSE-1777: the tenant owes 1777 euros on day 13."
    )
    first = asked(fakes)[0]
    assert "CLAUSE-1777: the tenant" not in first.text  # the prompt held a start, not the whole
    assert "What does CLAUSE-1777 say?" in first.text  # and the question after the document
    assert len(first.text) < 3_000
    # The whole message is kept for the user, and whole on disk for Enzo.
    history = await store.history(chat)
    assert history[0].parts[0].text.startswith(document)  # type: ignore[union-attr]
    stored = list((tmp_path / "context" / str(chat)).glob("*.txt"))
    assert any(document in p.read_text() for p in stored)


async def test_reopened_chats_show_where_older_messages_were_summarized(
    store: Store, tmp_path: Path
) -> None:
    svc, _ = service(store, tmp_path, config(compact_at=1_500, keep_recent=300))
    chat = await store.create_conversation()
    n = 0
    while not await store.compactions(chat):
        n += 1
        await say(svc, chat, words(n))
    (summary,) = await store.compactions(chat)
    app = create_app(Settings(env="test"), service=svc)
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://enzo") as c:
        shown = (await c.get(f"/conversations/{chat}/messages")).json()
        big = await c.post(f"/conversations/{chat}/turns", json={"message": "x" * 200_000})
    marked = [i for i, m in enumerate(shown) if "summarized" in m]
    assert len(marked) == 1
    assert shown[marked[0]]["summarized"] == summary.turns
    # The notice sits right after the user message of the turn that triggered it.
    assert shown[marked[0] - 1]["role"] == "user"
    assert shown[marked[0]]["role"] == "assistant"
    assert big.status_code == 200  # a very long message is accepted, not refused
    assert "event: compacted" in big.text or "event: done" in big.text
