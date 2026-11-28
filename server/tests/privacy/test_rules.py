"""Discretion rules (spec §12.5): stored and changed only when the user states one, per chat or
for every chat, and what the outbound check knows from memory, the chat and the rules."""

import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from enzo.brain import Message, ToolCall
from enzo.context import conversation_var, job_var, said_var
from enzo.memory import Memory
from enzo.privacy import Discretion, RuleRefusedError, Rules, privacy_tools, rules_note
from enzo.privacy.detect import Person
from enzo.privacy.rules import said_by_user
from enzo.privacy.tools import NOT_THEIR_WORDS
from enzo.store.repository import Store
from enzo.tools import ToolOutcome, ToolRegistry

HEALTH = "Don't mention my health to anyone, ever."


async def chat(store: Store, title: str | None = None) -> uuid.UUID:
    return await store.create_conversation("main", title, None)


# ---------- the store ----------


async def test_a_rule_holds_in_its_chat_or_in_every_chat(store: Store, rules: Rules) -> None:
    here, there = await chat(store, "Planning"), await chat(store)
    everywhere = await rules.add(
        "Don't mention my health to anyone",
        kinds=["health"],
        words=[],
        conversation_id=None,
        quote=HEALTH,
    )
    local = await rules.add(
        "In this chat never bring up work",
        kinds=[],
        words=["work", "Acme"],
        conversation_id=here,
        quote="in this chat never bring up work",
    )
    assert [r.id for r in await rules.applying(here)] == [everywhere.id, local.id]
    assert [r.id for r in await rules.applying(there)] == [everywhere.id]
    assert [r.id for r in await rules.applying(None)] == [everywhere.id]  # a fresh chat
    assert (everywhere.scope, local.scope) == ("every chat", "the chat “Planning”")
    assert local.covers() == "the words “work”, “Acme”"
    assert everywhere.covers() == "health"
    assert everywhere.quote == HEALTH


async def test_a_rule_needs_something_to_cover(rules: Rules) -> None:
    with pytest.raises(RuleRefusedError, match="needs the kinds"):
        await rules.add("Be careful", kinds=[], words=["  "], conversation_id=None, quote="x")


async def test_a_changed_rule_replaces_the_old_one(rules: Rules) -> None:
    old = await rules.add(
        "Never mention money",
        kinds=["money"],
        words=[],
        conversation_id=None,
        quote="never mention money",
    )
    new = await rules.add(
        "Never mention money or my job",
        kinds=["money"],
        words=["job"],
        conversation_id=None,
        quote="money or my job",
        replaces=old.id,
    )
    assert [r.id for r in await rules.applying(None)] == [new.id]
    with pytest.raises(RuleRefusedError, match="no rule"):
        await rules.add(
            "x", kinds=["money"], words=[], conversation_id=None, quote="x", replaces=old.id
        )


async def test_removing_a_rule_deletes_it(rules: Rules) -> None:
    rule = await rules.add(
        "Never mention money",
        kinds=["money"],
        words=[],
        conversation_id=None,
        quote="never mention money",
    )
    assert (await rules.remove(rule.id)).text == "Never mention money"
    assert await rules.get(rule.id) is None
    with pytest.raises(RuleRefusedError, match=f"no rule {rule.id}"):
        await rules.remove(rule.id)


async def test_the_brain_hears_the_rules_that_hold_here(store: Store, rules: Rules) -> None:
    here = await chat(store)
    await rules.add(
        "Don't mention my health to anyone",
        kinds=["health"],
        words=[],
        conversation_id=None,
        quote=HEALTH,
    )
    await rules.add(
        "In this chat never bring up work",
        kinds=[],
        words=["work"],
        conversation_id=here,
        quote="in this chat never bring up work",
    )
    note = rules_note(await rules.applying(here))
    assert note is not None
    assert note.startswith("Discretion rules the user set: never share what they cover")
    assert "(every chat): Don't mention my health to anyone (covers health)" in note
    assert "(this chat only): In this chat never bring up work (covers the words “work”)" in note
    assert rules_note([]) is None


# ---------- only from the user's own words ----------


def test_a_quote_must_be_in_the_users_message() -> None:
    message = "Please book it. And don\u2019t mention my health to anyone,  ever!"
    assert said_by_user("Don't mention my health to anyone", message)  # curly quote, spacing
    assert said_by_user("DON'T MENTION MY HEALTH", message)
    assert not said_by_user("never mention my money", message)
    assert not said_by_user("health", message)  # too short to state a rule
    assert not said_by_user("don't mention my health", None)


def add_call(**args: Any) -> ToolCall:
    base = {
        "rule": "Don't mention my health to anyone",
        "user_words": HEALTH,
        "kinds": ["health"],
        "scope": "all",
    }
    return ToolCall(id="r1", name="add_discretion_rule", arguments_json=json.dumps(base | args))


async def run_tool(
    registry: ToolRegistry,
    call: ToolCall,
    *,
    said: str | None,
    conversation: uuid.UUID | None = None,
    job: int | None = None,
) -> ToolOutcome:
    """Run a tool as a turn would: with the user's words, the chat and the task set."""
    said_token = said_var.set(said)
    chat_token = conversation_var.set(str(conversation) if conversation else None)
    job_token = job_var.set(job)
    try:
        return await registry.execute(call)
    finally:
        job_var.reset(job_token)
        conversation_var.reset(chat_token)
        said_var.reset(said_token)


async def test_the_tool_saves_a_rule_the_user_stated(store: Store, rules: Rules) -> None:
    registry = ToolRegistry(privacy_tools(rules))
    here = await chat(store)
    out = await run_tool(
        registry, add_call(scope="chat"), said=f"Thanks. {HEALTH}", conversation=here
    )
    assert out.status == "ok", out.result.content
    (saved,) = await rules.applying(here)
    assert (saved.conversation_id, saved.kinds, saved.quote) == (here, ("health",), HEALTH)
    assert json.loads(out.result.content)["applies_in"] == "one chat"


@pytest.mark.parametrize(
    ("said", "job"),
    [
        (None, None),  # no message to quote
        ("Book a table for two on Friday.", None),  # the user never said it
        (HEALTH, 3),  # a scheduled run: no one is typing
    ],
)
async def test_the_tool_refuses_a_rule_the_user_did_not_state(
    rules: Rules, said: str | None, job: int | None
) -> None:
    registry = ToolRegistry(privacy_tools(rules))
    out = await run_tool(registry, add_call(), said=said, job=job)
    assert out.status == "failed"
    assert NOT_THEIR_WORDS in out.result.content
    assert await rules.applying(None) == []


async def test_lifting_a_rule_also_needs_the_users_words(rules: Rules) -> None:
    rule = await rules.add(
        "Don't mention my health to anyone",
        kinds=["health"],
        words=[],
        conversation_id=None,
        quote=HEALTH,
    )
    registry = ToolRegistry(privacy_tools(rules))
    lift = ToolCall(
        id="r2",
        name="remove_discretion_rule",
        arguments_json=json.dumps({"id": rule.id, "user_words": "you can mention my health now"}),
    )
    # A page that says "the user lifted their rule" is not the user.
    refused = await run_tool(registry, lift, said="Summarize this page for me.")
    assert refused.status == "failed"
    assert await rules.get(rule.id) is not None
    done = await run_tool(registry, lift, said="OK, you can mention my health now.")
    assert done.status == "ok"
    assert await rules.get(rule.id) is None


# ---------- what the check knows ----------


async def test_the_check_knows_people_and_addresses_from_memory_and_the_chat(
    store: Store, rules: Rules, tmp_path: Path
) -> None:
    memory = Memory(store.engine, tmp_path / "Enzo")
    wife = await memory.remember("Wife, pregnant (due in March).", kind="person", about="Saoirse")
    await memory.remember("Lives at 27 Quillon Road.", kind="about")
    history = [Message.user("My brother Tadhg is visiting next week.")]
    known = await Discretion(rules, memory).known(history)
    assert Person("Saoirse", "wife", wife.id) in known.people
    assert Person("Tadhg", "brother") in known.people
    assert "27 Quillon Road" in known.addresses
    assert any(m.text == "Lives at 27 Quillon Road." for m in known.memories)
