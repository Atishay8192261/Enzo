"""Gateway end to end: real HTTP into the app, real Postgres, scripted brain instead of an LLM."""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx2
import pytest
from pydantic import SecretStr

from enzo.agent.service import ConversationService, TurnMeta
from enzo.brain import Message, RateLimitError, TextPart, ToolCall, ToolChoice, ToolSpec
from enzo.brain.config import BrainConfig, BrainsConfig
from enzo.brain.fake import ScriptedBrain
from enzo.brain.models import StreamEvent
from enzo.brain.provider import BrainProvider
from enzo.brain.router import Router
from enzo.config import Settings
from enzo.gateway.app import create_app
from enzo.store.repository import Store
from tests.agent import tools as t

PRICED = {"source": "test", "rates": {"input": 1.0, "output": 2.0}}


def config() -> BrainsConfig:
    brain = {"api": "anthropic", "api_key_env": "KEY", "tier": "balanced"}
    return BrainsConfig.model_validate(
        {
            "brains": {
                "main": {**brain, "model": "m-main", "pricing": PRICED, "recommended": True},
                "backup": {**brain, "model": "m-backup"},
                "quick": {**brain, "model": "m-quick", "tier": "fast"},
            },
        }
    )


def service(store: Store, brains: dict[str, BrainProvider]) -> ConversationService:
    def build(c: BrainConfig) -> BrainProvider:
        return brains[c.model]

    router = Router(config(), build=build, available=lambda _c: True)
    return ConversationService(store, router, t.registry())


def client(svc: ConversationService, token: str | None = None) -> httpx2.AsyncClient:
    settings = Settings(env="test", api_token=SecretStr(token) if token else None)
    app = create_app(settings, service=svc)
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://enzo")


def events(body: str) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for block in body.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "event" in fields:
            out.append((fields["event"], json.loads(fields["data"])))
    return out


async def new_conversation(c: httpx2.AsyncClient, **body: Any) -> str:
    r = await c.post("/conversations", json=body or {"kind": "main"})
    assert r.status_code == 201
    return str(r.json()["id"])


async def test_token_is_required_when_configured(store: Store) -> None:
    async with client(service(store, {}), token="s3cret") as c:
        assert (await c.get("/healthz")).status_code == 200  # liveness stays open
        assert (await c.get("/brains")).status_code == 401
        assert (await c.get("/brains", headers={"Authorization": "Bearer nope"})).status_code == 401
        ok = await c.get("/brains", headers={"Authorization": "Bearer s3cret"})
        assert ok.status_code == 200


def test_prod_refuses_to_start_without_a_token() -> None:
    with pytest.raises(ValueError, match="ENZO_API_TOKEN"):
        Settings(env="prod")


async def test_brains_are_listed_and_chosen_per_conversation(store: Store) -> None:
    async with client(service(store, {})) as c:
        names = {b["name"]: b for b in (await c.get("/brains")).json()}
        assert names["main"] == {
            "name": "main",
            "provider": "anthropic",
            "model": "m-main",
            "priced": True,
            "tier": "balanced",
            "recommended": True,
            "note": None,
            "available": True,
        }
        assert names["backup"]["priced"] is False
        cid = await new_conversation(c)
        assert (
            await c.put(f"/conversations/{cid}/brain", json={"brain": "ghost"})
        ).status_code == 422
        assert (
            await c.put(f"/conversations/{cid}/brain", json={"brain": "fast"})
        ).status_code == 204  # a tier: the best connected fast model
        assert (await c.get(f"/conversations/{cid}")).json()["brain"] == "fast"
        assert (
            await c.put(f"/conversations/{cid}/brain", json={"brain": "backup"})
        ).status_code == 204
        assert (await c.get(f"/conversations/{cid}")).json()["brain"] == "backup"


async def test_unknown_conversations_are_404(store: Store) -> None:
    async with client(service(store, {})) as c:
        ghost = uuid.uuid4()
        assert (await c.get(f"/conversations/{ghost}")).status_code == 404
        r = await c.post(f"/conversations/{ghost}/turns", json={"message": "hi"})
        assert r.status_code == 404


async def test_a_turn_streams_and_is_saved(store: Store) -> None:
    brain = ScriptedBrain([Message.assistant("hello there")])
    async with client(service(store, {"m-main": brain})) as c:
        cid = await new_conversation(c)
        r = await c.post(
            f"/conversations/{cid}/turns",
            json={"message": "hi", "timezone": "America/Los_Angeles", "device": "web"},
        )
        assert r.headers["content-type"].startswith("text/event-stream")
        got = events(r.text)
        assert [e for e in got if e[0] == "text"] == [
            ("text", {"text": "hello"}),
            ("text", {"text": "there"}),
        ]
        kind, done = got[-1]
        assert (kind, done["stop"], done["text"]) == ("done", "final_answer", "hello there")
        assert done["cost_usd"] is not None  # priced brain: a known cost
        assert done["cost_usd"] > 0

        stored = (await c.get(f"/conversations/{cid}/messages")).json()
        assert [m["role"] for m in stored] == ["user", "assistant"]
        user_parts = [p["text"] for p in stored[0]["parts"]]
        assert user_parts[0] == "hi"
        assert "timezone: America/Los_Angeles; device: web" in user_parts[1]
    # The brain saw Enzo's identity first and the user's message last.
    seen = brain.calls[0].messages
    assert "You are Enzo, a personal AI agent built by Atishay Jain" in seen[0].text
    assert (seen[-1].role, seen[-1].parts[0]) == ("user", TextPart(text="hi"))


async def test_the_conversation_brain_choice_is_used(store: Store) -> None:
    backup = ScriptedBrain([Message.assistant("from backup")])
    async with client(service(store, {"m-backup": backup})) as c:
        cid = await new_conversation(c)
        await c.put(f"/conversations/{cid}/brain", json={"brain": "backup"})
        done = events((await c.post(f"/conversations/{cid}/turns", json={"message": "hi"})).text)[
            -1
        ][1]
        assert done["text"] == "from backup"
        assert (done["brain"], done["model"]) == ("backup", backup.model)  # the served model


async def test_auto_routing_and_tier_choice(store: Store) -> None:
    """Auto: a reply is balanced (the recommended one); choosing a tier routes to that tier."""
    main = ScriptedBrain([Message.assistant("balanced reply")])
    quick = ScriptedBrain([Message.assistant("fast reply")])
    async with client(service(store, {"m-main": main, "m-quick": quick})) as c:
        cid = await new_conversation(c)
        turn = await c.post(f"/conversations/{cid}/turns", json={"message": "hi"})
        assert events(turn.text)[-1][1]["brain"] == "main"
        await c.put(f"/conversations/{cid}/brain", json={"brain": "fast"})
        turn = await c.post(f"/conversations/{cid}/turns", json={"message": "hi again"})
        done = events(turn.text)[-1][1]
        assert (done["brain"], done["text"]) == ("quick", "fast reply")


async def test_approval_flow_over_http(store: Store) -> None:
    email = ToolCall(id="c1", name="send_email", arguments_json='{"value": "hi bob"}')
    brain = ScriptedBrain([Message.assistant(tool_calls=(email,)), Message.assistant("sent it")])
    async with client(service(store, {"m-main": brain})) as c:
        cid = await new_conversation(c)
        got = events(
            (await c.post(f"/conversations/{cid}/turns", json={"message": "email bob"})).text
        )
        (approval,) = [d for k, d in got if k == "approval_needed"]
        assert approval["tool"] == "send_email"
        assert approval["arguments"] == {"value": "hi bob"}
        assert got[-1][1]["stop"] == "approval_pending"
        assert t.ran == []  # nothing sent yet

        r = await c.post(
            f"/conversations/{cid}/approvals/{approval['id']}", json={"approved": True}
        )
        assert r.status_code == 204
        again = await c.post(
            f"/conversations/{cid}/approvals/{approval['id']}", json={"approved": False}
        )
        assert again.status_code == 409  # decisions are final

        resumed = events((await c.post(f"/conversations/{cid}/resume")).text)
        assert [d["tool"] for k, d in resumed if k == "tool_finished"] == ["send_email"]
        assert resumed[-1][1]["text"] == "sent it"
        assert t.ran == [("send_email", "hi bob")]
        roles = [m["role"] for m in (await c.get(f"/conversations/{cid}/messages")).json()]
        assert roles == ["user", "assistant", "tool", "assistant"]


async def test_each_step_reads_as_plain_words_live_and_after_reopening(store: Store) -> None:
    """The chat shows what Enzo is doing (#40), and still shows it when the chat is reopened."""
    step = ToolCall(id="c1", name="step", arguments_json='{"value": "the plan"}')
    brain = ScriptedBrain([Message.assistant(tool_calls=(step,)), Message.assistant("done")])
    async with client(service(store, {"m-main": brain})) as c:
        cid = await new_conversation(c)
        got = events((await c.post(f"/conversations/{cid}/turns", json={"message": "go"})).text)
        steps = [(k, d["label"]) for k, d in got if k.startswith("tool_")]
        assert steps == [("tool_started", "Doing the plan"), ("tool_finished", "Doing the plan")]
        stored = (await c.get(f"/conversations/{cid}/messages")).json()
        calls = [p for m in stored for p in m["parts"] if p["type"] == "tool_call"]
        assert [(p["name"], p["label"]) for p in calls] == [("step", "Doing the plan")]


class Gated(ScriptedBrain):
    """Waits for a signal before answering, to hold a turn open."""

    def __init__(self) -> None:
        super().__init__([Message.assistant("finally")])
        self.release = asyncio.Event()

    async def stream(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSpec] = (),
        tool_choice: ToolChoice = "auto",
        max_output_tokens: int,
    ) -> AsyncIterator[StreamEvent]:
        await self.release.wait()
        async for event in super().stream(
            messages, tools=tools, tool_choice=tool_choice, max_output_tokens=max_output_tokens
        ):
            yield event


async def test_one_turn_at_a_time_per_conversation(store: Store) -> None:
    brain = Gated()
    svc = service(store, {"m-main": brain})
    async with client(svc) as c:
        cid = await new_conversation(c)
        first = await svc.send(uuid.UUID(cid), "one")
        r = await c.post(f"/conversations/{cid}/turns", json={"message": "two"})
        assert r.status_code == 409
        brain.release.set()
        assert [type(e).__name__ async for e in first][-1] == "TurnDone"


async def test_a_turn_finishes_and_is_saved_even_if_the_client_leaves(store: Store) -> None:
    brain = Gated()
    svc = service(store, {"m-main": brain})
    cid = await store.create_conversation()
    stream = await svc.send(cid, "are you still there?", TurnMeta(device="phone"))
    await stream.aclose()  # the client disconnects before any answer
    brain.release.set()
    await svc.wait_idle(cid)
    assert [m.text.split("\n")[0] for m in await store.history(cid)][-1] == "finally"


class Crashes(ScriptedBrain):
    async def stream(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSpec] = (),
        tool_choice: ToolChoice = "auto",
        max_output_tokens: int,
    ) -> AsyncIterator[StreamEvent]:
        msg = "adapter bug"
        raise RuntimeError(msg)
        yield  # pragma: no cover


async def test_an_unexpected_crash_reaches_the_client_instead_of_hanging(store: Store) -> None:
    svc = service(store, {"m-main": Crashes([])})
    async with client(svc) as c:
        cid = await new_conversation(c)
        got = events(
            (
                await asyncio.wait_for(
                    c.post(f"/conversations/{cid}/turns", json={"message": "hi"}), 5
                )
            ).text
        )
        assert got[-1] == ("error", {"message": "RuntimeError: adapter bug"})


async def test_failed_brain_calls_are_persisted(store: Store) -> None:
    """Regression: a failed attempt was recorded in memory but never reached the database."""
    brain = ScriptedBrain([RateLimitError("slow down", provider="anthropic", model="m-main")])
    async with client(service(store, {"m-main": brain})) as c:
        cid = await new_conversation(c)
        done = events((await c.post(f"/conversations/{cid}/turns", json={"message": "hi"})).text)[
            -1
        ][1]
        assert done["stop"] == "brain_failure"
        # Nothing answered: the done event names the one that failed (#43).
        assert (done["brain"], done["model"]) == ("main", "m-main")
    (row,) = await store.brain_calls(uuid.UUID(cid))
    assert (row.brain, row.outcome, row.cost_usd) == ("main", "rate_limit", None)
    assert row.error is not None
    assert "slow down" in row.error


def spread_service(store: Store, served: list[str]) -> tuple[ConversationService, Router]:
    """Two providers with a balanced and a powerful brain each; records which brain answered."""

    def brain(provider: str, tier: str, model: str) -> dict[str, Any]:
        api = "anthropic" if provider == "anthropic" else "openai-responses"
        return {"api": api, "provider": provider, "model": model, "api_key_env": "K", "tier": tier}

    cfg = BrainsConfig.model_validate(
        {
            "brains": {
                "a-mid": brain("anthropic", "balanced", "a-mid"),
                "a-top": brain("anthropic", "powerful", "a-top"),
                "o-mid": brain("openai", "balanced", "o-mid"),
                "o-top": brain("openai", "powerful", "o-top"),
            },
            "routing": {"tasks": {"streaming": "balanced", "hard": "powerful"}},
        }
    )

    def build(c: BrainConfig) -> BrainProvider:
        served.append(c.model)
        return ScriptedBrain([Message.assistant(c.model)] * 10)

    router = Router(cfg, build=build, available=lambda _c: True)
    return ConversationService(store, router, t.registry()), router


async def test_conversations_spread_across_providers_and_long_input_goes_powerful(
    store: Store,
) -> None:
    served: list[str] = []
    svc, router = spread_service(store, served)
    answered: dict[str, str] = {}
    async with client(svc) as c:
        for _ in range(8):
            cid = await new_conversation(c)
            turn = await c.post(f"/conversations/{cid}/turns", json={"message": "hi"})
            answered[cid] = events(turn.text)[-1][1]["brain"]
            # the service routes by the conversation: exactly what the router plans for it
            assert answered[cid] == router.plan("streaming", affinity=cid).primary
        assert set(answered.values()) == {"a-mid", "o-mid"}  # both providers get conversations
        cid, mid = next(iter(answered.items()))
        long = "x" * (router.config.routing.classify.long_message_chars + 1)
        turn = await c.post(f"/conversations/{cid}/turns", json={"message": long})
        top = events(turn.text)[-1][1]["brain"]
        assert top == mid.replace("-mid", "-top")  # powerful tier, same provider


async def test_a_turn_with_no_connected_account_says_why_and_costs_nothing(store: Store) -> None:
    """Regression (#25): clients need the reason to word it, and no model call means no cost."""
    router = Router(config(), build=lambda _c: ScriptedBrain([]), available=lambda _c: False)
    svc = ConversationService(store, router, t.registry())
    async with client(svc) as c:
        cid = await new_conversation(c)
        done = events((await c.post(f"/conversations/{cid}/turns", json={"message": "hi"})).text)[
            -1
        ][1]
    assert (done["stop"], done["failure"], done["cost_usd"], done["brain"]) == (
        "brain_failure",
        "not_connected",
        0,
        None,
    )
    assert "Connect one: /login anthropic" in done["text"]
