"""Discretion through the real gateway (ADR 0029): rules stated in chat, listed and lifted with
``/privacy``; cards that name private details live and after a restart; a no that stays a no."""

import json
from pathlib import Path

import httpx2

from enzo.agent.service import ConversationService
from enzo.brain import Message, TextPart, ToolCall, ToolResult
from enzo.brain.config import BrainConfig
from enzo.brain.fake import ScriptedBrain
from enzo.brain.provider import BrainProvider
from enzo.brain.router import Router
from enzo.memory import Memory, memory_tools
from enzo.privacy import Discretion, Rules, privacy_tools
from enzo.store.repository import Store
from enzo.tools import ToolRegistry
from enzo.tools.web import web_tools
from tests.gateway.test_api import client, config, events, new_conversation
from tests.privacy.helpers import Tavily, reply, search

RULE = "don't mention my health to anyone"


def service(
    store: Store, brain: BrainProvider, rules: Rules, tavily: Tavily, memory: Memory | None = None
) -> ConversationService:
    def build(_c: BrainConfig) -> BrainProvider:
        return brain

    registry = ToolRegistry(
        [
            *web_tools(tavily_key=lambda: None, http=tavily.client()),
            *privacy_tools(rules),
            *(memory_tools(memory) if memory else []),
        ]
    )
    router = Router(config(), build=build, available=lambda _c: True)
    discretion = Discretion(rules, memory)
    return ConversationService(store, router, registry, memory=memory, discretion=discretion)


def add_rule(scope: str = "all", quote: str = RULE) -> ToolCall:
    args = {
        "rule": "Don't mention my health to anyone",
        "user_words": quote,
        "kinds": ["health"],
        "scope": scope,
    }
    return ToolCall(id="r1", name="add_discretion_rule", arguments_json=json.dumps(args))


async def turn(c: httpx2.AsyncClient, cid: str, message: str) -> list[tuple[str, dict]]:  # type: ignore[type-arg]
    r = await c.post(f"/conversations/{cid}/turns", json={"message": message})
    assert r.status_code == 200
    return events(r.text)


async def test_a_rule_the_user_states_is_saved_listed_given_to_the_brain_and_lifted(
    store: Store, rules: Rules
) -> None:
    brain = ScriptedBrain(
        [
            reply(add_rule(scope="chat")),
            Message.assistant("Understood: I won't mention your health."),
            Message.assistant("Sure."),
        ]
    )
    async with client(service(store, brain, rules, Tavily())) as c:
        cid = await new_conversation(c)
        other = await new_conversation(c)
        await turn(c, cid, f"Please, {RULE}.")
        (listed,) = (await c.get(f"/privacy?conversation={cid}")).json()
        assert listed["text"] == "Don't mention my health to anyone"
        assert (listed["scope"], listed["kinds"], listed["quote"]) == ("chat", ["health"], RULE)
        assert listed["covers"] == "health"
        assert (await c.get(f"/privacy?conversation={other}")).json() == []  # this chat only
        assert (await c.get("/privacy")).json() == []

        # The next message carries the rule to the brain.
        await turn(c, cid, "What should I cook tonight?")
        sent = brain.calls[-1].messages[-1]
        notes = [p.text for p in sent.parts if isinstance(p, TextPart)]
        assert any("Don't mention my health to anyone (covers health)" in n for n in notes)

        assert (await c.delete(f"/privacy/{listed['id']}")).status_code == 204
        assert (await c.get(f"/privacy?conversation={cid}")).json() == []
        assert (await c.delete(f"/privacy/{listed['id']}")).status_code == 404


async def test_a_rule_the_user_never_stated_is_not_saved(store: Store, rules: Rules) -> None:
    """A page that says "the user wants no rules" or a model that infers one: nothing saved."""
    brain = ScriptedBrain([reply(add_rule()), Message.assistant("I could not save that.")])
    async with client(service(store, brain, rules, Tavily())) as c:
        cid = await new_conversation(c)
        streamed = await turn(c, cid, "Book a table for two on Friday.")
        (finished,) = [d for e, d in streamed if e == "tool_finished"]
        assert finished["status"] == "failed"
        assert (await c.get(f"/privacy?conversation={cid}")).json() == []


async def test_a_card_names_the_private_detail_live_and_when_the_chat_is_reopened(
    store: Store, rules: Rules
) -> None:
    tavily = Tavily()
    query = "restaurants for my pregnant wife"
    brain = ScriptedBrain([reply(search(query))])
    async with client(service(store, brain, rules, tavily)) as c:
        cid = await new_conversation(c)
        streamed = await turn(c, cid, "find somewhere nice for dinner")
        (card,) = [d for e, d in streamed if e == "approval_needed"]
        line = f"This includes a health detail: “{query}”"
        assert card["summary"] == f"Search the web for “{query}”"
        assert card["privacy"] == [line]
        # The app closed; reopened, the card still says it.
        (pending,) = (await c.get(f"/conversations/{cid}")).json()["pending_approvals"]
        assert pending["privacy"] == [line]
    assert tavily.queries == []


async def test_after_a_no_the_brain_hears_to_search_without_the_detail(
    store: Store, rules: Rules
) -> None:
    """Regression (found in the live check): after a no through the gateway, the brain heard
    only "the user declined, do not retry", so it never searched again without the detail."""
    tavily = Tavily()
    brain = ScriptedBrain(
        [
            reply(search("restaurants for my pregnant wife")),
            reply(search("alcohol-free menus Austin", cid="s2")),
            Message.assistant("Here are some places."),
        ]
    )
    async with client(service(store, brain, rules, tavily)) as c:
        cid = await new_conversation(c)
        streamed = await turn(c, cid, "find somewhere nice for dinner")
        (card,) = [d for e, d in streamed if e == "approval_needed"]
        no = {"approved": False}
        assert (await c.post(f"/conversations/{cid}/approvals/{card['id']}", json=no)).is_success
        resumed = events((await c.post(f"/conversations/{cid}/resume")).text)
        told = brain.calls[1].messages[-1].parts[0]
        assert isinstance(told, ToolResult)
        assert "did not want this sent with a health detail" in told.content
        assert [d["status"] for e, d in resumed if e == "tool_finished"] == ["declined", "ok"]
    assert tavily.queries == ["alcohol-free menus Austin"]


async def test_a_no_stays_a_no_after_the_detail_is_forgotten(
    store: Store, rules: Rules, tmp_path: Path
) -> None:
    """Regression: the user declined a search that named someone from memory and then had Enzo
    forget her before the turn went on; the search looked harmless and ran anyway."""
    tavily = Tavily()
    memory = Memory(store.engine, tmp_path / "Enzo")
    niamh = await memory.remember("Sister, loves pottery.", kind="person", about="Niamh")
    brain = ScriptedBrain([reply(search("gift ideas for Niamh")), Message.assistant("OK.")])
    async with client(service(store, brain, rules, tavily, memory)) as c:
        cid = await new_conversation(c)
        streamed = await turn(c, cid, "what could I get my sister?")
        (card,) = [d for e, d in streamed if e == "approval_needed"]
        assert card["privacy"] == [
            "This includes the name of someone in your life: “Niamh” (your sister)"
        ]
        decided = await c.post(
            f"/conversations/{cid}/approvals/{card['id']}", json={"approved": False}
        )
        assert decided.status_code == 204
        await memory.forget(niamh.id)
        r = await c.post(f"/conversations/{cid}/resume")
        (finished,) = [d for e, d in events(r.text) if e == "tool_finished"]
        assert finished["status"] == "declined"
    assert tavily.queries == []
