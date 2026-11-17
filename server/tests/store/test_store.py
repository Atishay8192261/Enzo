import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from enzo.agent.approvals import approval_request
from enzo.brain import Message, NativePart, TextPart, ToolCall, ToolResult
from enzo.brain.router import CallRecord
from enzo.store.repository import ApprovalError, Store
from enzo.tools.registry import ToolOutcome
from tests.agent.tools import Args

NOW = datetime(2026, 11, 17, 12, tzinfo=UTC)
CALL = ToolCall(id="c1", name="send_email", arguments_json='{"value": "hi"}')


def record(cost: float | None) -> CallRecord:
    return CallRecord(
        brain="main",
        provider="anthropic",
        model="claude-opus-5-5",
        task="hard",
        attempt=1,
        started_at=NOW,
        latency_ms=12.5,
        outcome="ok",
        input_tokens=100,
        output_tokens=20,
        cached_input_tokens=60,
        cache_write_input_tokens=10,
        cost_usd=cost,
    )


async def test_history_round_trips_exactly_including_native_replay_data(store: Store) -> None:
    conv = await store.create_conversation("main", "first chat")
    turn = [
        Message.user("hi"),
        Message(
            role="assistant",
            parts=(
                NativePart(
                    provider="anthropic",
                    payload={"content": [{"type": "thinking", "signature": "s"}]},
                ),
                TextPart(text="checking"),
                CALL,
            ),
        ),
        Message.tool(ToolResult(call_id="c1", name="send_email", content="{}", is_error=True)),
    ]
    await store.save_turn(conv, messages=turn)
    assert await store.history(conv) == turn


async def test_turns_append_in_order(store: Store) -> None:
    conv = await store.create_conversation()
    await store.save_turn(conv, messages=[Message.user("1"), Message.assistant("one")])
    await store.save_turn(conv, messages=[Message.user("2"), Message.assistant("two")])
    assert [m.text for m in await store.history(conv)] == ["1", "one", "2", "two"]


async def test_concurrent_turns_never_collide(store: Store) -> None:
    conv = await store.create_conversation()
    await asyncio.gather(
        *(store.save_turn(conv, messages=[Message.user(str(i))]) for i in range(10))
    )
    assert sorted(m.text for m in await store.history(conv)) == [str(i) for i in range(10)]


async def test_unknown_cost_is_stored_as_unknown_not_zero(store: Store) -> None:
    conv = await store.create_conversation()
    await store.save_turn(conv, messages=[], brain_calls=[record(None), record(0.00123456)])
    unknown, known = await store.brain_calls(conv)
    assert unknown.cost_usd is None
    assert str(known.cost_usd) == "0.00123456"
    assert (known.cached_input_tokens, known.cache_write_input_tokens) == (60, 10)


async def test_tool_calls_are_kept_for_tracing(store: Store) -> None:
    conv = await store.create_conversation()
    result = ToolResult(call_id="c1", name="send_email", content='{"value": "sent"}')
    await store.save_turn(conv, messages=[], tool_outcomes=[ToolOutcome(CALL, result, "ok", 3.2)])
    (row,) = await store.tool_calls(conv)
    assert (row.tool, row.arguments_json, row.status, row.result, row.latency_ms) == (
        "send_email",
        '{"value": "hi"}',
        "ok",
        '{"value": "sent"}',
        3.2,
    )


async def test_only_a_recorded_decision_counts_and_it_is_final(store: Store) -> None:
    conv = await store.create_conversation()
    request = approval_request(CALL, Args(value="hi"))
    await store.save_turn(conv, messages=[], pending=[request])
    await store.save_turn(conv, messages=[], pending=[request])  # asked again: still one record
    assert await store.decisions(conv) == {}
    await store.decide(conv, request.id, approved=True)
    assert await store.decisions(conv) == {request.id: True}
    with pytest.raises(ApprovalError, match="already decided"):
        await store.decide(conv, request.id, approved=False)
    with pytest.raises(ApprovalError, match="no approval request"):
        await store.decide(conv, "appr_made_up", approved=True)


async def test_decisions_do_not_leak_between_conversations(store: Store) -> None:
    a, b = await store.create_conversation(), await store.create_conversation()
    request = approval_request(CALL, Args(value="hi"))
    await store.save_turn(a, messages=[], pending=[request])
    await store.decide(a, request.id, approved=True)
    assert await store.decisions(b) == {}
    with pytest.raises(ApprovalError):
        await store.decide(b, request.id, approved=True)


async def test_brain_choice_is_stored_per_conversation(store: Store) -> None:
    conv = await store.create_conversation("side", "trip planning")
    await store.set_brain(conv, "gpt-sol")
    info = await store.conversation(conv)
    assert info is not None
    assert (info.kind, info.title, info.brain) == ("side", "trip planning", "gpt-sol")
    with pytest.raises(KeyError):
        await store.set_brain(uuid.uuid4(), "gpt-sol")


async def test_saving_to_a_missing_conversation_fails_loudly(store: Store) -> None:
    with pytest.raises(KeyError):
        await store.save_turn(uuid.uuid4(), messages=[Message.user("x")])


async def test_the_database_enforces_conversation_kinds(store: Store) -> None:
    with pytest.raises(IntegrityError):
        await store.create_conversation("secret")  # type: ignore[arg-type]
