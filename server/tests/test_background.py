"""Reminders outlive the app (spec §9.2): the gateway stays on in the background while one is
still to come, delivers it, then stops itself. `enzo stop` turns it off by hand."""

import asyncio
import os
import signal
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from enzo import __version__, cli, launcher
from enzo.accounts.vault import generate_key
from enzo.schedule import Schedule
from enzo.store.migrate import upgrade
from tests.pg import create_database, drop_database


@pytest.fixture
def state(tmp_path: Path) -> Path:
    (tmp_path / "clients").mkdir()
    return tmp_path


def gateway_running(monkeypatch: pytest.MonkeyPatch, stopped: list[int]) -> None:
    monkeypatch.setattr(launcher, "_gateway_pid", lambda _s: 4242)
    monkeypatch.setattr(launcher, "_stop", lambda _s, pid: stopped.append(pid))


def test_the_last_app_leaves_it_on_while_reminders_are_due(
    state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stopped: list[int] = []
    said: list[str] = []
    gateway_running(monkeypatch, stopped)
    monkeypatch.setattr(launcher, "upcoming", lambda _port: (2, "Fri 21 Nov 09:00"))
    launcher.release(state, 8787, say=said.append)
    assert stopped == []
    assert said == [
        "Enzo stays on in the background for your 2 scheduled items (next: Fri 21 Nov 09:00). "
        "`enzo stop` turns it off."
    ]
    assert (state / launcher.BACKGROUND).exists()


def test_a_watch_that_is_on_keeps_it_on_too(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stopped: list[int] = []
    said: list[str] = []
    gateway_running(monkeypatch, stopped)
    monkeypatch.setattr(launcher, "upcoming", lambda _port: None)
    monkeypatch.setattr(launcher, "watching", lambda _port: 2)
    launcher.release(state, 8787, say=said.append)
    assert stopped == []
    assert said == ["Enzo stays on in the background for your 2 watches. `enzo stop` turns it off."]
    monkeypatch.setattr(launcher, "upcoming", lambda _port: (1, "Fri 21 Nov 09:00"))
    monkeypatch.setattr(launcher, "watching", lambda _port: 1)
    launcher.release(state, 8787, say=said.append)
    assert said[-1].startswith(
        "Enzo stays on in the background for your 1 scheduled item and 1 watch"
    )


def test_with_nothing_due_the_last_app_stops_it(
    state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stopped: list[int] = []
    gateway_running(monkeypatch, stopped)
    monkeypatch.setattr(launcher, "upcoming", lambda _port: None)
    launcher.release(state, 8787, say=lambda _m: None)
    assert stopped == [4242]
    assert not (state / launcher.BACKGROUND).exists()


def test_it_stops_itself_only_when_kept_on_for_reminders_and_no_app_is_open(state: Path) -> None:
    assert not launcher.idle(state)  # an app is using it, or it was started by hand
    (state / launcher.BACKGROUND).touch()
    (state / "clients" / str(os.getpid())).touch()  # an app opened again
    assert not launcher.idle(state)
    (state / "clients" / str(os.getpid())).unlink()
    assert launcher.idle(state)
    assert not (state / launcher.BACKGROUND).exists()


def test_enzo_stop(
    state: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ENZO_STATE_DIR", str(state))
    stopped: list[int] = []
    gateway_running(monkeypatch, stopped)
    (state / launcher.BACKGROUND).touch()
    assert cli.main(["stop"]) == 0
    assert stopped == [4242]
    assert not (state / launcher.BACKGROUND).exists()
    assert capsys.readouterr().out.startswith("Stopped Enzo. Reminders wait until you open Enzo")
    monkeypatch.setattr(launcher, "_gateway_pid", lambda _s: None)
    assert cli.main(["stop"]) == 0
    assert capsys.readouterr().out == "Enzo is not running in the background.\n"


def test_an_idle_gateway_asks_itself_to_stop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    signals: list[int] = []
    monkeypatch.setattr(
        "enzo.cli.os.kill", lambda pid, sig: signals.append(sig) if pid == os.getpid() else None
    )
    monkeypatch.setattr(launcher, "idle", lambda _s: False)
    cli._stop_if_idle(tmp_path)
    assert signals == []
    monkeypatch.setattr(launcher, "idle", lambda _s: True)
    cli._stop_if_idle(tmp_path)
    assert signals == [signal.SIGTERM]  # uvicorn then shuts down cleanly


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.mark.timeout(120)
def test_a_real_gateway_delivers_after_the_app_closed_then_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole path in real processes: start, close the app, deliver, stop on its own."""
    name, url = create_database()
    state, port = tmp_path / "state", free_port()
    monkeypatch.setenv("ENZO_DATABASE_URL", url)
    monkeypatch.setenv("ENZO_STATE_DIR", str(state))
    monkeypatch.setenv("ENZO_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ENZO_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ENZO_WORKSPACE_DIR", str(tmp_path / "Enzo"))
    monkeypatch.setenv("ENZO_VAULT_KEY", generate_key())
    monkeypatch.setenv("ENZO_NOTIFY", "0")
    try:
        upgrade(url)
        due = datetime.now(UTC) + timedelta(seconds=8)

        async def set_reminder() -> None:
            engine = create_async_engine(url)
            await Schedule(engine).remind("Stretch", at=due, timezone="UTC")
            await engine.dispose()

        asyncio.run(set_reminder())  # set before the start: the gateway sees it at once
        launcher.attach(state, port, say=lambda _m: None)
        said: list[str] = []
        launcher.release(state, port, say=said.append)
        assert said
        assert said[0].startswith("Enzo stays on in the background for your 1 scheduled item")
        assert launcher.health(port) is not None  # still on, though no app is open

        pid = launcher._gateway_pid(state)
        assert pid is not None
        process = launcher._children.pop(pid)
        process.wait(timeout=60)  # it delivered, then stopped itself
        assert launcher.health(port) is None

        async def delivered() -> int:
            engine = create_async_engine(url)
            async with engine.connect() as conn:
                found = (await conn.execute(text("SELECT count(*) FROM job_runs"))).scalar()
            await engine.dispose()
            return int(found or 0)

        assert asyncio.run(delivered()) == 1
        log = (state / "logs" / "gateway.log").read_text()
        assert '"event": "reminder.delivered"' in log
        assert '"event": "gateway.idle_stop"' in log
        assert not (state / launcher.GATEWAY_PID).exists()
    finally:
        pid = launcher._gateway_pid(state)
        if pid is not None:
            launcher._stop(state, pid)
        drop_database(name)


def test_a_gateway_still_stopping_is_waited_for_never_doubled(
    state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It stopped itself and stopped answering, but has not exited yet: a new app waits."""
    events: list[str] = []
    monkeypatch.setattr(launcher, "health", lambda _p: None)
    monkeypatch.setattr(launcher, "_gateway_pid", lambda _s: 4242)
    monkeypatch.setattr(launcher, "_alive", lambda pid: pid == 4242)
    monkeypatch.setattr(launcher, "_stop", lambda _s, pid: events.append(f"waited for {pid}"))
    monkeypatch.setattr(launcher, "port_taken", lambda _p: False)
    monkeypatch.setattr(launcher, "_start", lambda *_a: events.append("started"))
    launcher.attach(state, 8787, say=lambda _m: None)
    assert events == ["waited for 4242", "started"]


def test_an_app_opening_again_keeps_it_from_stopping_itself(
    state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (state / launcher.BACKGROUND).touch()  # kept on for reminders after the last app closed
    monkeypatch.setattr(launcher, "health", lambda _p: {"status": "ok", "version": __version__})
    monkeypatch.setattr(launcher, "_gateway_pid", lambda _s: 4242)
    launcher.attach(state, 8787, say=lambda _m: None)
    assert not (state / launcher.BACKGROUND).exists()
    (state / "clients" / str(os.getpid())).unlink()  # the app closes again without releasing
    assert not launcher.idle(state)  # still not idle: only the last app's release decides
