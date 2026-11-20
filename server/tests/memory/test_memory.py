"""Memory (spec §6.2, §3 steps 2 and 6, §15.2): taught in one chat, recalled in the next,
corrected in plain words, inspectable with its provenance, and never holding a secret."""

import io
import json
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import text

from enzo import logs
from enzo.agent.service import ConversationService
from enzo.brain import Message, ToolCall
from enzo.brain.fake import ScriptedBrain
from enzo.brain.router import Router
from enzo.context import conversation_var, turn_var
from enzo.memory import Fact, Memory, MemoryRefusedError, memory_tools
from enzo.memory.guard import secret_reason
from enzo.store.repository import Store
from enzo.store.schema import MemoryRow
from enzo.tools.registry import ToolRegistry
from tests.gateway.test_api import client, config, events, new_conversation
from tests.test_logs import reset


@pytest.fixture
def memory(store: Store, tmp_path: Path) -> Memory:
    return Memory(store.engine, tmp_path / "Enzo")


def remember(call_id: str, text: str, **more: object) -> ToolCall:
    args = {"text": text, "kind": "preference", **more}
    return ToolCall(id=call_id, name="remember", arguments_json=json.dumps(args))


def service(store: Store, memory: Memory, brain: ScriptedBrain) -> ConversationService:
    router = Router(config(), build=lambda _c: brain, available=lambda _c: True)
    return ConversationService(store, router, ToolRegistry(memory_tools(memory)), memory=memory)


def memory_part(message: Message) -> str:
    """The memory that rode with a user message (ADR 0007), or "" when none did."""
    texts = [p.text for p in message.parts if hasattr(p, "text")]
    return next((t for t in texts if t.startswith("Relevant memory")), "")


async def test_taught_recalled_in_a_new_chat_corrected_and_the_new_value_used(
    store: Store, memory: Memory
) -> None:
    """Spec §17 "Memory" and §6.2 "Verify", end to end through the gateway."""
    brain = ScriptedBrain(
        [
            Message.assistant(tool_calls=(remember("c1", "Prefers window seats on flights."),)),
            Message.assistant("Noted: window seats."),
            # A new chat: the memory comes with the message; then the user corrects it.
            Message.assistant("I'll pick a window seat."),
            Message.assistant(
                tool_calls=(remember("c2", "Prefers aisle seats on flights.", replaces=1),)
            ),
            Message.assistant("Updated: aisle seats from now on."),
            Message.assistant("An aisle seat, then."),
        ]
    )
    out = io.StringIO()
    logs.configure(stream=out)
    try:
        async with client(service(store, memory, brain)) as c:
            first = await new_conversation(c)
            await c.post(f"/conversations/{first}/turns", json={"message": "I like window seats"})
            second = await new_conversation(c)
            await c.post(f"/conversations/{second}/turns", json={"message": "Book my flight seat"})
            said = "Actually I prefer aisle seats now"
            await c.post(f"/conversations/{second}/turns", json={"message": said})
            third = await new_conversation(c)
            ask = {"message": "Which seat on my flight?"}
            last = await c.post(f"/conversations/{third}/turns", json=ask)
            assert events(last.text)[-1][1]["text"] == "An aisle seat, then."
            listed = (await c.get("/memory")).json()
    finally:
        reset()

    recalled = memory_part(brain.calls[2].messages[-1])
    assert "#1 Prefers window seats on flights." in recalled
    assert "the user said it" in recalled
    final = memory_part(brain.calls[5].messages[-1])
    assert "Prefers aisle seats on flights." in final
    assert "window" not in final  # the old value is replaced, not kept alongside (§6.2)
    assert memory_part(brain.calls[0].messages[-1]) == ""  # nothing known yet: nothing sent
    (current,) = listed["memories"]
    assert (current["id"], current["text"], current["said_by"]) == (
        2,
        "Prefers aisle seats on flights.",
        "user",
    )
    assert current["conversation_id"] == second  # where it was learned (§6.5)
    assert current["source"].startswith("you said, ")
    # Each recall's log line names its chat (and never what was recalled).
    recalls = [json.loads(x) for x in out.getvalue().splitlines() if "memory.recalled" in x]
    assert [r.get("conversation") for r in recalls] == [second, second, third]
    assert "seats" not in out.getvalue()


async def test_recall_matches_any_word_in_any_form_and_skips_unrelated_messages(
    memory: Memory,
) -> None:
    await memory.remember("Prefers window seats on flights.", kind="preference")
    await memory.remember("Allergic to peanuts.", kind="about")
    await memory.remember("Sister, lives in Austin.", kind="person", about="Priya")
    assert [f.text for f in await memory.recall("Where should I sit on my next flight?")] == [
        "Prefers window seats on flights."
    ]  # "flight" finds "flights"
    assert [f.about for f in await memory.recall("what should I get priya for her birthday")] == [
        "Priya"
    ]  # who it is about counts too
    assert await memory.recall("What is a mutex?") == []
    assert await memory.recall("hi, how are you?") == []  # only common words
    assert await memory.recall("") == []
    assert await memory.recall("':|&!()") == []  # search syntax in a message is just text


async def test_a_correction_replaces_and_forgetting_deletes_every_version(
    memory: Memory, tmp_path: Path
) -> None:
    old = await memory.remember("Lives in San Jose.", kind="about")
    new = await memory.remember("Lives in Austin.", kind="about", replaces=old.id)
    assert [f.text for f in await memory.all()] == ["Lives in Austin."]
    assert await memory.search("San Jose") == []
    with pytest.raises(MemoryRefusedError, match=f"no current memory {old.id} to replace"):
        await memory.remember("Lives in Boston.", kind="about", replaces=old.id)
    with pytest.raises(MemoryRefusedError, match="no current memory 999 to replace"):
        await memory.remember("Lives in Boston.", kind="about", replaces=999)
    forgotten = await memory.forget(new.id)
    assert forgotten.text == "Lives in Austin."
    assert await memory.all() == []
    async with memory._sessions() as session:  # the older version went too
        assert (await session.execute(text("SELECT count(*) FROM memories"))).scalar() == 0
    with pytest.raises(MemoryRefusedError, match="no current memory"):
        await memory.forget(new.id)


async def test_old_versions_never_crowd_out_the_current_one(memory: Memory) -> None:
    old = await memory.remember(
        "Window seats, window seats, always window seats.", kind="preference"
    )
    await memory.remember("Aisle seats now.", kind="preference", replaces=old.id)
    # The old wording matches better, yet only what is current can come back.
    assert [f.text for f in await memory.search("window seats", limit=1)] == ["Aisle seats now."]


def test_recalled_memory_says_who_it_came_from() -> None:
    from enzo.agent.service import _snippet  # noqa: PLC0415 - the one place it is shaped

    fact = Fact(
        id=7,
        text="Sister, lives in Austin.",
        kind="person",
        about="Priya",
        said_by="inferred",
        happened_on=None,
        learned_at=datetime(2026, 11, 20, tzinfo=UTC),
        chat="Birthday ideas",
        conversation_id=None,
    )
    snippet = _snippet(fact)
    assert (snippet.id, snippet.text) == (7, "Priya: Sister, lives in Austin.")
    assert snippet.source == "Enzo inferred it in “Birthday ideas”"  # an inference says so (§6.5)
    plain = _snippet(replace(fact, about=None, said_by="user", chat=None))
    assert (plain.text, plain.source) == ("Sister, lives in Austin.", "the user said it")


async def test_a_memory_records_its_chat_and_run(store: Store, memory: Memory) -> None:
    chat = await store.create_conversation(title="Trip to NYC")
    conversation_var.set(str(chat))
    turn_var.set("t_000000000001")
    try:
        await memory.remember("Flies out of SJC.", kind="fact", happened_on=date(2026, 11, 2))
    finally:
        conversation_var.set(None)
        turn_var.set(None)
    (fact,) = await memory.all()
    assert (fact.conversation_id, fact.chat) == (chat, "Trip to NYC")
    async with memory._sessions() as session:
        row = await session.get(MemoryRow, fact.id)
        assert row is not None
        assert row.turn_id == "t_000000000001"  # joins the log and the ledgers (#48)


async def test_the_readable_copy_follows_every_change(memory: Memory, tmp_path: Path) -> None:
    path = tmp_path / "Enzo" / "Memory.md"
    old = await memory.remember("Prefers window seats on flights.", kind="preference")
    await memory.remember("Sister.", kind="person", about="Priya", said_by="inferred")
    text = path.read_text()
    assert "## Preferences\n\n- Prefers window seats on flights. _(you said, " in text
    assert "## People\n\n- **Priya**: Sister. _(Enzo inferred, " in text
    assert path.stat().st_mode & 0o777 == 0o600
    await memory.remember("Prefers aisle seats.", kind="preference", replaces=old.id)
    assert "window" not in path.read_text()
    for fact in await memory.all():
        await memory.forget(fact.id)
    assert path.read_text().rstrip().endswith("Nothing yet.")
    assert not list(path.parent.glob(".memory-*"))  # no half-written copies left behind


@pytest.mark.parametrize(
    "text",
    [
        "My wifi password is hunter2",
        "Bank PIN: 4321",
        "OpenAI key sk-" + "0" * 24,
        "GitHub token ghp_" + "0" * 30,
        "Card 4111 1111 1111 1111",
        "SSN 123-45-6789",
        "Token " + "ab12" * 10,
        "-----BEGIN RSA PRIVATE KEY-----",
    ],
)
async def test_secrets_are_never_kept(memory: Memory, tmp_path: Path, text: str) -> None:
    with pytest.raises(MemoryRefusedError, match="Enzo never keeps those"):
        await memory.remember(text, kind="fact")
    assert await memory.all() == []
    assert not (tmp_path / "Enzo" / "Memory.md").exists()


@pytest.mark.parametrize(
    "text",
    [
        "Uses a password manager.",
        "Phone number is 408 555 0134.",
        "Order 1234567890123 arrived.",
        "Born 3 March 1990.",
        "Pins tabs in the browser.",
    ],
)
def test_ordinary_facts_pass_the_secret_check(text: str) -> None:
    assert secret_reason(text) is None


async def test_the_tools_save_search_and_forget(memory: Memory) -> None:
    registry = ToolRegistry(memory_tools(memory))
    saved = await registry.execute(
        remember("c1", "Prefers short answers.", happened_on="2026-11-20")
    )
    assert saved.status == "ok"
    assert json.loads(saved.result.content) == {
        "id": 1,
        "text": "Prefers short answers.",
        "replaced": None,
    }
    assert (
        registry.activity(remember("c1", "Prefers short answers."))
        == "Remembering: Prefers short answers."
    )
    listed = await registry.execute(ToolCall(id="c2", name="search_memory", arguments_json="{}"))
    (found,) = json.loads(listed.result.content)["memories"]
    assert (found["id"], found["happened_on"]) == (1, "2026-11-20")
    refused = await registry.execute(remember("c3", "My password is hunter2"))
    assert refused.status == "failed"
    assert "never keeps those" in refused.result.content
    bad = await registry.execute(remember("c4", "x", kind="secret"))
    assert bad.status == "invalid_arguments"  # checked before anything runs
    gone = await registry.execute(ToolCall(id="c5", name="forget", arguments_json='{"id": 1}'))
    assert json.loads(gone.result.content)["text"] == "Prefers short answers."
    assert await memory.all() == []


async def test_what_is_remembered_never_reaches_the_log(memory: Memory) -> None:
    out = io.StringIO()
    logs.configure(stream=out)
    try:
        fact = await memory.remember("Secretly planning a surprise for Priya.", kind="fact")
        await memory.recall("surprise for priya")
        await memory.forget(fact.id)
    finally:
        reset()
    events_logged = [json.loads(line)["event"] for line in out.getvalue().splitlines()]
    assert events_logged == ["memory.saved", "memory.recalled", "memory.forgotten"]
    assert "surprise" not in out.getvalue().lower()


async def test_the_memory_endpoints(store: Store, memory: Memory) -> None:
    await memory.remember("Prefers window seats.", kind="preference")
    async with client(service(store, memory, ScriptedBrain([]))) as c:
        listed = (await c.get("/memory")).json()
        assert [m["text"] for m in listed["memories"]] == ["Prefers window seats."]
        assert listed["file"].endswith("Enzo/Memory.md")
        assert (await c.delete("/memory/1")).status_code == 204
        assert (await c.delete("/memory/1")).status_code == 404
        assert (await c.get("/memory")).json()["memories"] == []
