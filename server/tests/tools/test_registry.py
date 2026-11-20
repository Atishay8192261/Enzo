import asyncio
import json
import time
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from enzo.brain import ToolCall
from enzo.tools import Tool, ToolRegistry, tool


class EchoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1)
    delay_s: float = 0.0


class EchoResult(BaseModel):
    echoed: str


calls: list[str] = []


@tool(name="echo", description="Echo text back.")
async def echo(args: EchoArgs) -> EchoResult:
    calls.append(args.text)
    await asyncio.sleep(args.delay_s)
    return EchoResult(echoed=args.text)


@tool(name="boom", description="Always fails.")
async def boom(args: EchoArgs) -> EchoResult:
    msg = "disk on fire"
    raise RuntimeError(msg)


@tool(name="liar", description="Returns the wrong type.")
async def liar(args: EchoArgs) -> EchoResult:
    return EchoArgs(text="not a result")  # type: ignore[return-value]


@tool(name="slow", description="Too slow.", timeout_s=0.05)
async def slow(args: EchoArgs) -> EchoResult:
    await asyncio.sleep(5)
    return EchoResult(echoed="never")


@tool(name="send_email", description="Send an email.", requires_approval=True)
async def send_email(args: EchoArgs) -> EchoResult:
    return EchoResult(echoed="sent")


REGISTRY = ToolRegistry([echo, boom, liar, slow, send_email])


def call(name: str, args: object, call_id: str = "c1") -> ToolCall:
    raw = args if isinstance(args, str) else json.dumps(args)
    return ToolCall(id=call_id, name=name, arguments_json=raw)


def test_decorator_takes_models_from_type_hints() -> None:
    assert (echo.params, echo.returns, echo.requires_approval) == (EchoArgs, EchoResult, False)
    assert send_email.requires_approval is True
    assert echo.spec().parameters["required"] == ["text"]


def test_decorator_rejects_untyped_tools() -> None:
    with pytest.raises(TypeError, match="one Pydantic model"):

        @tool(name="bad", description="d")  # type: ignore[type-var]
        async def bad(text: str) -> str:
            return text


def test_registry_rejects_duplicates_and_bad_names() -> None:
    with pytest.raises(ValueError, match="already registered"):
        ToolRegistry([echo, echo])
    with pytest.raises(ValidationError):
        ToolRegistry([Tool("bad name", "d", EchoArgs, EchoResult, echo.handler)])


async def test_valid_call_runs_and_returns_the_typed_result() -> None:
    outcome = await REGISTRY.execute(call("echo", {"text": "hi"}))
    assert (outcome.status, outcome.result.is_error) == ("ok", False)
    assert json.loads(outcome.result.content) == {"echoed": "hi"}
    assert outcome.result.call_id == "c1"


async def test_invalid_arguments_never_reach_the_tool() -> None:
    calls.clear()
    outcome = await REGISTRY.execute(call("echo", {"text": ""}))
    assert outcome.status == "invalid_arguments"
    assert outcome.result.is_error
    assert json.loads(outcome.result.content)["error"] == "invalid_arguments"
    assert calls == []


@pytest.mark.parametrize(
    ("name", "status", "fragment"),
    [
        ("nope", "unknown_tool", "Unknown tool 'nope'"),
        ("boom", "failed", "RuntimeError: disk on fire"),
        ("liar", "bad_result", "returned EchoArgs, expected EchoResult"),
        ("slow", "timeout", "timed out after 0.05s"),
    ],
)
async def test_tool_problems_come_back_as_error_results(
    name: str, status: str, fragment: str
) -> None:
    outcome = await REGISTRY.execute(call(name, {"text": "x"}))
    assert (outcome.status, outcome.result.is_error) == (status, True)
    body = json.loads(outcome.result.content)
    assert body["error"] == status
    assert fragment in body["message"]


async def test_independent_calls_run_concurrently_in_call_order() -> None:
    batch = [call("echo", {"text": t, "delay_s": 0.2}, f"c{t}") for t in ("a", "b", "c")]
    started = time.perf_counter()
    outcomes = await REGISTRY.execute_all(batch)
    elapsed = time.perf_counter() - started
    assert [o.result.call_id for o in outcomes] == ["ca", "cb", "cc"]
    assert elapsed < 0.45  # sequential would take 0.6s


@pytest.mark.parametrize(
    ("name", "args"),
    [("echo", {"text": "x", "delay_s": 0.1}), ("slow", {"text": "x"})],
)
async def test_an_outcome_records_when_the_call_started(name: str, args: object) -> None:
    """#43: a turn's tool rows all carried the save time; a timeline needs each start."""
    before = datetime.now(UTC)
    outcome = await REGISTRY.execute(call(name, args))
    after = datetime.now(UTC)
    assert before <= outcome.started_at
    # The start, not the end: a call that took 50 ms or more started that long before now.
    assert outcome.started_at <= after - timedelta(milliseconds=45)
    assert outcome.latency_ms >= 45


async def test_cancellation_is_not_swallowed_as_a_tool_error() -> None:
    task = asyncio.create_task(REGISTRY.execute(call("echo", {"text": "x", "delay_s": 5})))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_specs_can_load_only_the_tools_a_task_needs() -> None:
    assert [s.name for s in REGISTRY.specs(["echo"])] == ["echo"]
    assert len(REGISTRY.specs()) == 5
    with pytest.raises(KeyError):
        REGISTRY.specs(["missing"])
