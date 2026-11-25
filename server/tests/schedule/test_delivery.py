"""How a reminder reaches the user: a desktop notification, and live to every open app."""

import asyncio
import json
import socket
from collections.abc import AsyncIterator, Callable

import httpx2
import pytest
import uvicorn
from fastapi import FastAPI

from enzo.agent.service import ConversationService
from enzo.brain.router import Router
from enzo.config import Settings
from enzo.gateway.app import create_app
from enzo.schedule import Background, Bus, Event, Runner, Schedule, notify
from enzo.store.repository import Store
from enzo.tools.registry import ToolRegistry
from tests.gateway.test_api import config


def test_macos_gets_the_words_as_arguments_never_as_script(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("enzo.schedule.notify.shutil.which", lambda name: f"/usr/bin/{name}")
    argv = notify.command("Enzo reminder", 'Call "mom" \x1b[2J" & quit', platform="darwin")
    assert argv is not None
    assert argv[0] == "osascript"
    script = " ".join(argv[1:-2])
    assert "mom" not in script  # the text is never part of the script
    assert argv[-2:] == ["Enzo reminder", 'Call "mom" [2J" & quit']  # no escape codes either


def test_linux_uses_notify_send_and_otherwise_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "enzo.schedule.notify.shutil.which",
        lambda name: "/usr/bin/notify-send" if name == "notify-send" else None,
    )
    assert notify.command("Enzo reminder", "Tea", platform="linux") == [
        "notify-send",
        "--app-name=Enzo",
        "Enzo reminder",
        "Tea",
    ]
    monkeypatch.setattr("enzo.schedule.notify.shutil.which", lambda _name: None)
    assert notify.command("Enzo reminder", "Tea", platform="linux") is None


async def test_a_notification_that_cannot_be_shown_is_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(notify, "command", lambda _t, _b: ["/nonexistent/notifier", "x"])
    assert await notify.desktop("Enzo reminder", "Tea") is False


async def until(ready: Callable[[], bool], seconds: float = 5) -> None:
    for _ in range(int(seconds / 0.02)):
        if ready():
            return
        await asyncio.sleep(0.02)
    msg = "timed out waiting"
    raise AssertionError(msg)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def served(app: FastAPI, port: int) -> AsyncIterator[uvicorn.Server]:
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, lifespan="off")
    )
    task = asyncio.create_task(server.serve())
    await until(lambda: server.started)
    try:
        yield server
    finally:
        server.should_exit = True
        await task


async def test_open_apps_hear_a_reminder_the_moment_it_arrives(store: Store) -> None:
    bus = Bus()
    schedule = Schedule(store.engine)
    router = Router(config(), build=lambda _c: None, available=lambda _c: True)  # type: ignore[arg-type,return-value]
    svc = ConversationService(store, router, ToolRegistry([]))
    app = create_app(
        Settings(env="test"),
        service=svc,
        background=Background(schedule, bus, Runner(schedule, bus)),
    )
    port = free_port()
    async for _server in served(app, port):
        async with (
            httpx2.AsyncClient(base_url=f"http://127.0.0.1:{port}") as c,
            c.stream("GET", "/events") as stream,
        ):
            assert stream.status_code == 200
            await until(lambda: bus.listeners > 0)
            bus.publish(Event("reminder", {"text": "Call mom", "words": "Call mom"}))
            lines = stream.aiter_lines()
            got: dict[str, str] = {}
            async with asyncio.timeout(5):
                async for line in lines:
                    if line.startswith("event: "):
                        got["event"] = line[7:]
                    elif line.startswith("data: "):
                        got["data"] = line[6:]
                        break
        assert got["event"] == "reminder"
        assert json.loads(got["data"])["text"] == "Call mom"
        await until(lambda: bus.listeners == 0)  # an app that leaves stops listening
