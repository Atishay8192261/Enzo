"""`enzo trace`: one task from the ledgers and the log (spec §13.3, §15.4)."""

import asyncio
import json
import logging
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from enzo import cli, tracing
from enzo.agent.service import ConversationService
from enzo.brain import Message, ToolCall
from enzo.brain.config import BrainConfig, BrainsConfig
from enzo.brain.fake import ScriptedBrain
from enzo.brain.provider import BrainProvider
from enzo.brain.router import Router
from enzo.logs import JsonLines
from enzo.store.repository import Store
from tests.agent import tools as t
from tests.pg import TABLES

KEY = "sk-" + "a1" * 12  # key-shaped, low entropy


def call(name: str, value: str, cid: str) -> Message:
    args = json.dumps({"value": value})
    return Message.assistant(tool_calls=(ToolCall(id=cid, name=name, arguments_json=args),))


def config() -> BrainsConfig:
    return BrainsConfig.model_validate(
        {
            "brains": {
                "main": {
                    "api": "anthropic",
                    "model": "m-main",
                    "api_key_env": "KEY",
                    "tier": "balanced",
                    "pricing": {"source": "t", "rates": {"input": 1.0, "output": 2.0}},
                }
            }
        }
    )


async def drain(events: object) -> None:
    async for _ in events:  # type: ignore[attr-defined]
        pass


async def two_tasks(url: str) -> tuple[uuid.UUID, list[str]]:
    """Task 1: a step, then an email that waits for approval, approved, sent. Task 2: a reply."""
    brain = ScriptedBrain(
        [
            call("step", f"a {KEY} \x1b[31mred \x9b31m", "c1"),
            call("send_email", "hello", "c2"),
            Message.assistant("sent it"),
            Message.assistant("second answer"),
        ],
        provider="anthropic",
        model="m-main",
    )

    def build(_c: BrainConfig) -> BrainProvider:
        return brain

    store = Store.from_url(url)
    try:
        service = ConversationService(
            store, Router(config(), build=build, available=lambda _c: True), t.registry()
        )
        chat = await store.create_conversation()
        await drain(await service.send(chat, "do it"))
        await service.wait_idle(chat)
        (pending,) = await store.pending_approvals(chat)
        await store.decide(chat, pending.id, approved=True)
        await drain(await service.resume(chat))
        await service.wait_idle(chat)
        await drain(await service.send(chat, "and now?"))
        await service.wait_idle(chat)
        runs = [str(r.turn_id) for r in await store.brain_calls(chat)]
        return chat, list(dict.fromkeys(runs))
    finally:
        await store.close()


@pytest.fixture
def recorded(database_url: str, tmp_path: Path) -> Iterator[tuple[uuid.UUID, list[str], Path]]:
    """Two tasks in the ledgers, and their JSON log lines in a file."""
    log = tmp_path / "gateway.log"
    handler = logging.FileHandler(log, encoding="utf-8")
    handler.setFormatter(JsonLines())
    enzo = logging.getLogger("enzo")
    enzo.addHandler(handler)
    level = enzo.level
    enzo.setLevel(logging.INFO)
    try:
        chat, runs = asyncio.run(two_tasks(database_url))
    finally:
        enzo.removeHandler(handler)
        enzo.setLevel(level)
        handler.close()
    yield chat, runs, log

    async def empty() -> None:
        store = Store.from_url(database_url)
        async with store.engine.begin() as conn:
            await conn.exec_driver_sql(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE")
        await store.close()

    asyncio.run(empty())


def load(url: str, turn: str, log: Path) -> tracing.Trace:
    async def go() -> tracing.Trace:
        store = Store.from_url(url)
        try:
            return await tracing.load(store.engine, turn, log_lines=cli.log_lines(log))
        finally:
            await store.close()

    return asyncio.run(go())


def test_a_task_with_an_approval_is_one_trace_across_its_runs(
    database_url: str, recorded: tuple[uuid.UUID, list[str], Path]
) -> None:
    chat, runs, log = recorded
    first, resumed, _ = runs
    trace = load(database_url, first, log)
    assert trace.runs == [first, resumed]  # the send and the resume after the approval
    assert trace.message == "do it"
    assert trace.conversation_id == str(chat)
    assert [b.model for b in trace.brain_calls] == ["m-main"] * 3
    assert all(b.cost_usd is not None for b in trace.brain_calls)
    assert trace.stop == "final_answer"  # from the log's turn.end of the last run
    assert trace.route is not None
    assert trace.brain_calls[-1].first_token_ms is not None  # "sent it" streamed text
    assert trace.brain_calls[0].first_token_ms is None  # a tool call only: no text
    step, email = trace.tool_calls
    assert (step.tool, step.status, step.approval) == ("step", "ok", "not needed")
    assert (email.tool, email.approval) == ("send_email", "approved")
    assert email.arguments == '{"value": "hello"}'
    assert email.result_chars > 0
    assert trace.waiting == []


def test_last_is_the_newest_task(
    database_url: str, recorded: tuple[uuid.UUID, list[str], Path]
) -> None:
    _, runs, log = recorded
    trace = load(database_url, "last", log)
    assert trace.runs == [runs[-1]]
    assert trace.message == "and now?"
    assert trace.tool_calls == []


def test_the_trace_reads_well_and_leaks_nothing(
    database_url: str, recorded: tuple[uuid.UUID, list[str], Path]
) -> None:
    _, runs, log = recorded
    shown = tracing.render(load(database_url, runs[0], log))
    assert f"Trace of {runs[1]}" in shown
    assert "Asked: “do it”" in shown
    assert "ended: answered" in shown
    assert "main · anthropic/m-main · ok" in shown
    assert "send_email · ok" in shown
    assert "approved by you" in shown
    assert "Total: 3 model calls" in shown
    assert KEY not in shown  # a key-shaped argument is redacted
    assert "[redacted]" in shown
    assert "\x1b" not in shown  # escape codes from the brain never reach the terminal
    assert "\x9b" not in shown  # nor a one-byte (C1) one, which JSON leaves as it is


def test_enzo_trace_prints_it(
    database_url: str,
    recorded: tuple[uuid.UUID, list[str], Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, runs, log = recorded
    monkeypatch.setenv("ENZO_DATABASE_URL", database_url)
    assert cli.main(["trace", "--log", str(log)]) == 0
    assert f"Trace of {runs[-1]}" in capsys.readouterr().out
    assert cli.main(["trace", runs[0], "--json", "--log", str(log)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["runs"] == runs[:2]
    assert cli.main(["trace", "t_000000000000"]) == 1
    assert "No model or tool call has turn id t_000000000000" in capsys.readouterr().out
    assert cli.main(["trace", "yesterday"]) == 1
    assert "is not a turn id" in capsys.readouterr().out


def test_nothing_to_trace_says_so(
    database_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ENZO_DATABASE_URL", database_url)
    assert cli.main(["trace"]) == 1
    assert "Nothing to trace yet" in capsys.readouterr().out
