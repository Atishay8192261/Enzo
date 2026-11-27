"""Recall by meaning before each message (spec §3 step 2, §6.3), through the gateway: what reaches
the brain's request, in the ADR 0007 order, within the recall budget; and the brain's own search
of past chats."""

import json
import uuid
from datetime import date
from pathlib import Path

from enzo.agent.prompt import MemorySnippet, PastExcerpt, fit_recall, render_memory, render_past
from enzo.agent.service import ConversationService
from enzo.brain import Message, ToolCall
from enzo.brain.fake import ScriptedBrain
from enzo.brain.router import Router
from enzo.brain.tokens import text_tokens
from enzo.memory import Memory, memory_tools
from enzo.semantic import SemanticIndex, search_tools
from enzo.store.repository import Store
from enzo.tools.registry import ToolRegistry
from tests.gateway.test_api import client, config, events, new_conversation
from tests.semantic.embedders import ConceptEmbedder


def service(store: Store, brain: ScriptedBrain, tmp_path: Path) -> ConversationService:
    index = SemanticIndex(store.engine, ConceptEmbedder())
    memory = Memory(store.engine, tmp_path / "Enzo", index=index)
    router = Router(config(), build=lambda _c: brain, available=lambda _c: True)
    tools = ToolRegistry([*memory_tools(memory), *search_tools(index)])
    return ConversationService(store, router, tools, memory=memory, index=index)


def last_user_parts(brain: ScriptedBrain) -> list[str]:
    (*_, asked) = [m for m in brain.calls[-1].messages if m.role == "user"]
    return [p.text for p in asked.parts if hasattr(p, "text")]


def remember(text: str) -> Message:
    args = json.dumps({"text": text, "kind": "preference"})
    return Message.assistant(tool_calls=(ToolCall(id="r1", name="remember", arguments_json=args),))


async def test_a_fact_taught_one_way_reaches_the_brain_when_asked_another_way(
    store: Store, tmp_path: Path
) -> None:
    """§6.3 verify, end to end: taught in one chat, asked in other words in a new chat."""
    brain = ScriptedBrain(
        [
            remember("Allergic to shellfish."),
            Message.assistant("Noted."),
            Message.assistant("Better not: shrimp is shellfish."),
            Message.assistant("No note on that."),
        ]
    )
    svc = service(store, brain, tmp_path)
    async with client(svc) as c:
        first = await new_conversation(c)
        await c.post(f"/conversations/{first}/turns", json={"message": "I can't eat shellfish"})
        second = await new_conversation(c)
        r = await c.post(f"/conversations/{second}/turns", json={"message": "Shrimp scampi ok?"})
        assert events(r.text)[-1][1]["text"] == "Better not: shrimp is shellfish."
        words, context, recalled, *rest = last_user_parts(brain)
        assert words == "Shrimp scampi ok?"
        assert context.startswith("[sent ")
        assert recalled.startswith("Relevant memory from Enzo's records")
        assert "#1 Allergic to shellfish. (source: the user said it in “I can't eat shellfish”" in (
            recalled
        )
        # The past chat itself is in the memory already; its turn may ride too, after it.
        assert all(p.startswith("Possibly relevant past chats") for p in rest)
        # An unrelated question in a third chat carries no memory and no past chat.
        third = await new_conversation(c)
        await c.post(f"/conversations/{third}/turns", json={"message": "Capital of Peru?"})
        assert last_user_parts(brain)[2:] == []


async def test_a_past_chat_rides_with_a_message_about_it_but_not_the_current_one(
    store: Store, tmp_path: Path
) -> None:
    brain = ScriptedBrain(
        [
            Message.assistant("Try Ippudo on 4th: rich tonkotsu."),
            Message.assistant("It was Ippudo on 4th."),
        ]
    )
    svc = service(store, brain, tmp_path)
    async with client(svc) as c:
        lunch = await new_conversation(c)
        await c.post(f"/conversations/{lunch}/turns", json={"message": "Find ramen near work"})
        assert svc.index is not None
        await svc.index.sync()  # the indexer runs in the background; here, now
        later = await new_conversation(c)
        ask = {"message": "Which noodle soup spot did you suggest?"}
        await c.post(f"/conversations/{later}/turns", json=ask)
        parts = last_user_parts(brain)
        past = next(p for p in parts if p.startswith("Possibly relevant past chats"))
        assert "From chat “Find ramen near work”" in past
        assert "User: Find ramen near work Enzo: Try Ippudo on 4th: rich tonkotsu." in past
        assert parts.index(past) > 1  # after the user's words and the context (ADR 0007)


async def test_recall_stays_within_its_budget_dropping_past_chats_first() -> None:
    memory = [
        MemorySnippet(f"Fact {i} " + "x" * 200, "the user said it", date(2026, 11, 20), i)
        for i in range(6)
    ]
    past = [PastExcerpt("User: " + "y" * 590, "chat “A”, 1 Nov 2026") for _ in range(2)]
    whole = text_tokens(render_memory(memory)) + text_tokens(render_past(past))
    kept_memory, kept_past = fit_recall(memory, past, budget=whole - 1)
    assert (len(kept_memory), len(kept_past)) == (6, 1)
    kept_memory, kept_past = fit_recall(memory, past, budget=text_tokens(render_memory(memory)))
    assert (len(kept_memory), len(kept_past)) == (6, 0)
    kept_memory, kept_past = fit_recall(memory, past, budget=200)
    assert kept_past == []
    assert text_tokens(render_memory(kept_memory)) <= 200
    assert [m.id for m in kept_memory] == list(range(len(kept_memory)))  # the best stay
    assert fit_recall(memory, past, budget=whole) == (memory, past)


def test_a_long_past_turn_is_cut_to_an_excerpt() -> None:
    long = PastExcerpt(
        "User: plan it\nEnzo: " + "Day one in Alfama. " * 100, "chat “Trip”, 2 Nov 2026"
    )
    line = render_past([long]).splitlines()[1]
    assert line.endswith("…")
    assert len(line) < 700


async def test_the_brain_searches_past_chats_by_meaning_and_gets_them_fenced(
    store: Store, tmp_path: Path
) -> None:
    args = json.dumps({"query": "that noodle soup place"})
    brain = ScriptedBrain(
        [
            Message.assistant("Try Ippudo on 4th."),
            Message.assistant(
                tool_calls=(ToolCall(id="s1", name="search_past_chats", arguments_json=args),)
            ),
            Message.assistant("It was Ippudo, in your lunch chat."),
        ]
    )
    svc = service(store, brain, tmp_path)
    async with client(svc) as c:
        lunch = await new_conversation(c)
        await c.post(f"/conversations/{lunch}/turns", json={"message": "Find ramen near work"})
        assert svc.index is not None
        await svc.index.sync()
        now = await new_conversation(c)
        r = await c.post(f"/conversations/{now}/turns", json={"message": "What was that place?"})
        steps = [d for e, d in events(r.text) if e == "tool_started"]
        assert steps
        assert "noodle soup" in json.dumps(steps)  # the step names what was looked for
    result = next(m for m in brain.calls[-1].messages if m.role == "tool").parts[0]
    content = result.content  # type: ignore[union-attr]
    assert content.startswith('<untrusted-data source="search_past_chats"')
    found = json.loads(content.split("\n", 1)[1].rsplit("\n", 1)[0])
    (hit,) = found["results"]
    assert hit["kind"] == "chat"
    assert hit["where"].startswith("chat “Find ramen near work”, ")
    assert hit["text"] == "User: Find ramen near work\nEnzo: Try Ippudo on 4th."
    assert found["by_meaning"] is True
    assert uuid.UUID(lunch)  # a real chat
