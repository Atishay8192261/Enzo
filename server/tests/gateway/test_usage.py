"""What each reply and each chat used (#49): from the ledger, the same live and after reopening."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from enzo.brain import Message, ToolCall
from enzo.brain.fake import ScriptedBrain
from enzo.gateway.usage import per_turn, usage_of
from enzo.store.repository import Store
from tests.gateway.test_api import client, events, new_conversation, service

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
