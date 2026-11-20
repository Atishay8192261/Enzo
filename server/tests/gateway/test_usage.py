"""What each reply and each chat used (#49): from the ledger, the same live and after reopening."""

import asyncio
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import asyncpg
from alembic import command

from enzo.brain import (
    Message,
    ProviderConnectionError,
    StreamDone,
    TextDelta,
    ToolCall,
    ToolChoice,
    ToolSpec,
)
from enzo.brain.fake import ScriptedBrain
from enzo.brain.models import StreamEvent
from enzo.gateway.usage import per_turn, usage_of
from enzo.store.repository import Store
from tests.gateway.test_api import client, events, new_conversation, service
from tests.pg import alembic_config, create_database, drop_database

T0 = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


@dataclass
class Call:
    started_at: datetime
    model: str = "m"
    latency_ms: float = 100.0
    outcome: str = "ok"
    input_tokens: int = 10
    output_tokens: int = 2
    cached_input_tokens: int = 0
    cost_usd: float | None = 0.01
    billing: str = "usage"


def test_usage_adds_up_and_never_invents_a_price() -> None:
    calls = [
        Call(at(1), model="a", input_tokens=100, output_tokens=5, cached_input_tokens=40),
        Call(at(0), model="b", outcome="rate_limited", input_tokens=0, output_tokens=0, cost_usd=0),
        Call(at(2), model="a", cost_usd=0.02),
        Call(
            at(3), model="c", billing="plan", cost_usd=None
        ),  # the plan paid: no price, not unknown
    ]
    u = usage_of(calls)
    assert (u.input_tokens, u.output_tokens, u.cached_input_tokens) == (120, 9, 40)
    assert u.calls == 4
    assert u.cost_usd is not None
    assert round(u.cost_usd, 6) == 0.03
    assert u.plan is True
    assert u.models == ["a", "c"]  # who answered, in order; a failed call answered nothing
    assert u.model_ms == 400.0
    assert u.started_at == at(0)
    assert usage_of([*calls, Call(at(4), cost_usd=None)]).cost_usd is None
    empty = usage_of([])
    assert (empty.cost_usd, empty.calls, empty.started_at) == (0.0, 0, None)


def test_calls_belong_to_the_user_message_they_answered() -> None:
    saves = [(at(10), True), (at(20), False), (at(30), True)]  # turn, its resume, next turn
    calls = [Call(at(25)), Call(at(1)), Call(at(15)), Call(at(31))]  # the last is still running
    turns = per_turn(saves, calls)
    assert [[c.started_at for c in t] for t in turns] == [[at(1), at(15)], [at(25), at(31)]]
    assert per_turn([], [Call(at(1))]) == []
    # a call that started the very moment its turn was saved is still that turn's
    assert len(per_turn([(at(10), True), (at(20), True)], [Call(at(10))])[0]) == 1


async def test_a_reopened_chat_shows_what_each_reply_used(store: Store) -> None:
    email = ToolCall(id="c1", name="send_email", arguments_json='{"value": "hi bob"}')
    step = ToolCall(id="c2", name="step", arguments_json='{"value": "x"}')
    brain = ScriptedBrain(
        [
            Message.assistant(tool_calls=(email,)),
            Message.assistant("sent it"),
            Message.assistant(tool_calls=(step,)),
            Message.assistant("done"),
        ]
    )
    async with client(service(store, {"m-main": brain})) as c:
        cid = await new_conversation(c)
        first = events(
            (await c.post(f"/conversations/{cid}/turns", json={"message": "email"})).text
        )
        live = [first[-1][1]["usage"]]
        (approval,) = [d for k, d in first if k == "approval_needed"]
        await c.post(f"/conversations/{cid}/approvals/{approval['id']}", json={"approved": True})
        resumed = events((await c.post(f"/conversations/{cid}/resume")).text)
        live.append(resumed[-1][1]["usage"])
        second = events((await c.post(f"/conversations/{cid}/turns", json={"message": "go"})).text)
        live.append(second[-1][1]["usage"])
        # the served model, as the brain reported it (spec §1.3.6)
        assert all(u["input_tokens"] > 0 and u["models"] == ["scripted-1"] for u in live)

        got = (await c.get(f"/conversations/{cid}/usage")).json()
        turns = got["turns"]
        assert len(turns) == 2  # the approval and its resume are one turn
        assert turns[0]["calls"] == 2
        assert turns[0]["input_tokens"] == live[0]["input_tokens"] + live[1]["input_tokens"]
        assert (turns[1]["calls"], turns[1]["input_tokens"]) == (2, live[2]["input_tokens"])
        assert got["total"]["input_tokens"] == sum(u["input_tokens"] for u in live)
        assert got["total"]["cost_usd"] > 0  # m-main has a published price
        assert turns[0]["started_at"] < turns[1]["started_at"]

        stored = (await c.get(f"/conversations/{cid}/messages")).json()
        calls = {p["id"]: p for m in stored for p in m["parts"] if p["type"] == "tool_call"}
        assert calls["c2"]["latency_ms"] >= 0  # each step's time is kept too
        assert (await c.get(f"/conversations/{cid}/usage")).status_code == 200


async def test_moving_on_from_a_card_starts_a_new_turn(store: Store) -> None:
    """Sending a new message instead of answering closes the open call first (#39): that save
    starts with the closing, not with the user's message, and is still a new turn."""
    email = ToolCall(id="c1", name="send_email", arguments_json='{"value": "hi"}')
    brain = ScriptedBrain([Message.assistant(tool_calls=(email,)), Message.assistant("ok")])
    async with client(service(store, {"m-main": brain})) as c:
        cid = await new_conversation(c)
        await c.post(f"/conversations/{cid}/turns", json={"message": "email"})
        await c.post(f"/conversations/{cid}/turns", json={"message": "never mind"})
        roles = [m["role"] for m in (await c.get(f"/conversations/{cid}/messages")).json()]
        assert roles == ["user", "assistant", "tool", "user", "assistant"]
        turns = (await c.get(f"/conversations/{cid}/usage")).json()["turns"]
        assert [t["calls"] for t in turns] == [1, 1]


async def test_usage_of_an_unknown_chat_is_not_found(store: Store) -> None:
    async with client(service(store, {})) as c:
        missing = "00000000-0000-0000-0000-000000000000"
        assert (await c.get(f"/conversations/{missing}/usage")).status_code == 404


class OnThePlan(ScriptedBrain):
    """Signed in with ChatGPT: the plan pays. Its second reply is cut off by a Wi-Fi drop."""

    billing = "plan"

    async def stream(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSpec] = (),
        tool_choice: ToolChoice = "auto",
        max_output_tokens: int,
    ) -> AsyncIterator[StreamEvent]:
        if self.calls:
            yield TextDelta(text="first word")
            raise ProviderConnectionError("connection lost", provider="openai", model="gpt-x")
        async for event in super().stream(
            messages, tools=tools, tool_choice=tool_choice, max_output_tokens=max_output_tokens
        ):
            if isinstance(event, StreamDone):
                yield StreamDone(response=event.response.model_copy(update={"billing": "plan"}))
            else:
                yield event


async def test_a_cut_off_plan_reply_leaves_the_chat_on_the_plan(store: Store) -> None:
    """#43: the cut-off reply read "cost unknown", and the whole chat "ChatGPT plan + cost
    unknown" from then on, though nothing was billed outside the plan."""
    async with client(service(store, {"m-main": OnThePlan([Message.assistant("ok")])})) as c:
        cid = await new_conversation(c)
        await c.post(f"/conversations/{cid}/turns", json={"message": "hi"})
        kind, done = events(
            (await c.post(f"/conversations/{cid}/turns", json={"message": "more"})).text
        )[-1]
        assert kind == "done"
        assert (done["plan"], done["cost_usd"]) == (True, 0)  # "Using ChatGPT plan"
        assert (done["brain"], done["model"]) == ("main", "gpt-x")  # names the brain that failed
        chat = (await c.get(f"/conversations/{cid}/usage")).json()
        assert [(u["plan"], u["cost_usd"]) for u in [chat["total"], *chat["turns"]]] == [
            (True, 0),
            (True, 0),
            (True, 0),
        ]


async def test_tool_calls_keep_when_each_started(store: Store) -> None:
    """#43: a turn's tool calls were all stamped with the time the turn was saved."""
    a = ToolCall(id="a", name="step", arguments_json='{"value": "one"}')
    b = ToolCall(id="b", name="step", arguments_json='{"value": "two"}')
    brain = ScriptedBrain(
        [
            Message.assistant(tool_calls=(a,)),
            Message.assistant(tool_calls=(b,)),
            Message.assistant("done"),
        ]
    )
    async with client(service(store, {"m-main": brain})) as c:
        cid = await new_conversation(c)
        await c.post(f"/conversations/{cid}/turns", json={"message": "go"})
    conv = uuid.UUID(cid)
    tools = await store.tool_calls(conv)
    brains = await store.brain_calls(conv)
    assert [t.call_id for t in tools] == ["a", "b"]
    # brain, tool a, brain, tool b, brain: each step after the one before it.
    order = [brains[0].started_at, tools[0].created_at, brains[1].started_at]
    order += [tools[1].created_at, brains[2].started_at]
    assert order == sorted(order)
    assert tools[0].created_at < tools[1].created_at


def test_chats_tainted_before_the_fix_are_repaired() -> None:
    """A failed call by a brain the same chat used on the plan becomes the plan's; a failed call
    on an API key keeps its unknown cost."""
    name, url = create_database()
    raw = url.replace("postgresql+asyncpg://", "postgresql://")

    async def run(sql: str, *args: object) -> list[asyncpg.Record]:
        conn = await asyncpg.connect(raw)
        try:
            return await conn.fetch(sql, *args)
        finally:
            await conn.close()

    call = (
        "INSERT INTO brain_calls (conversation_id, brain, provider, model, task, attempt, "
        "started_at, latency_ms, outcome, input_tokens, output_tokens, cached_input_tokens, "
        "cache_write_input_tokens, cost_usd, billing) "
        "VALUES ($1, $2, 'openai', 'm', 'streaming', 1, now(), 1, $3, 0, 0, 0, 0, NULL, $4)"
    )
    try:
        cfg = alembic_config(url)
        command.upgrade(cfg, "0004")
        plan_chat, key_chat = uuid.uuid4(), uuid.uuid4()
        for cid in (plan_chat, key_chat):
            asyncio.run(run("INSERT INTO conversations (id, kind) VALUES ($1, 'main')", cid))
        asyncio.run(run(call, plan_chat, "gpt-sol", "ok", "plan"))
        asyncio.run(run(call, plan_chat, "gpt-sol", "connection", "usage"))  # the #43 row
        asyncio.run(run(call, plan_chat, "claude", "server_error", "usage"))  # another brain
        asyncio.run(run(call, key_chat, "gpt-sol", "connection", "usage"))
        command.upgrade(cfg, "head")
        rows = asyncio.run(
            run("SELECT conversation_id, brain, outcome, billing FROM brain_calls ORDER BY id")
        )
        assert [tuple(r) for r in rows] == [
            (plan_chat, "gpt-sol", "ok", "plan"),
            (plan_chat, "gpt-sol", "connection", "plan"),
            (plan_chat, "claude", "server_error", "usage"),
            (key_chat, "gpt-sol", "connection", "usage"),
        ]
    finally:
        drop_database(name)
