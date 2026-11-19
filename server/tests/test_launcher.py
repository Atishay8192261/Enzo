"""The installed `enzo`: the launcher starts the gateway on the bundled Postgres, shares it between
open apps and stops both after the last one closes (#23)."""

import os
import signal
import socket
import subprocess
import time
from pathlib import Path

import pytest

from enzo import cli, launcher
from enzo.accounts.vault import generate_key


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def postgres_running(data: Path) -> bool:
    found = subprocess.run(  # noqa: S603 - fixed command
        ["pgrep", "-f", str(data / "postgres")],  # noqa: S607
        capture_output=True,
        check=False,
    )
    return found.returncode == 0


@pytest.fixture
def installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, int]:
    """An installed Enzo's folders, in a temp home, on its own port."""
    data, state = tmp_path / "data", tmp_path / "state"
    monkeypatch.setenv("ENZO_DATA_DIR", str(data))
    monkeypatch.setenv("ENZO_STATE_DIR", str(state))
    monkeypatch.setenv("ENZO_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ENZO_WORKSPACE_DIR", str(tmp_path / "Enzo"))
    monkeypatch.setenv("ENZO_VAULT_KEY", generate_key())
    monkeypatch.delenv("ENZO_DATABASE_URL", raising=False)
    return data, state, free_port()


@pytest.mark.timeout(120)
def test_the_last_app_to_close_stops_the_gateway_and_its_database(
    installed: tuple[Path, Path, int],
) -> None:
    data, state, port = installed
    gateway = launcher.attach(state, port, say=lambda _m: None)
    try:
        assert gateway.started
        assert launcher.health(port) is not None
        assert postgres_running(data)  # the bundled database came up with it

        other_app = state / "clients" / str(os.getppid())  # a second app, still open
        other_app.touch()
        launcher.release(state)
        assert launcher.health(port) is not None  # still needed by the other app

        other_app.unlink()
        (state / "clients" / str(os.getpid())).touch()
        launcher.release(state)
        assert launcher.health(port) is None
        assert not postgres_running(data)  # regression: Postgres outlived the gateway
        assert not (state / launcher.GATEWAY_PID).exists()
    finally:
        pid = launcher._gateway_pid(state)
        if pid is not None:
            launcher._stop(state, pid)


def test_a_port_taken_by_another_program_is_explained(installed: tuple[Path, Path, int]) -> None:
    _, state, _ = installed
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        with pytest.raises(launcher.LauncherError, match=f"Port {port} is used by another program"):
            launcher.attach(state, port, say=lambda _m: None)


def test_a_gateway_started_by_hand_is_never_stopped(tmp_path: Path) -> None:
    """Only a gateway the launcher started (it wrote its pid file) is stopped on close."""
    state = tmp_path / "state"
    (state / "clients").mkdir(parents=True)
    launcher.release(state)  # no pid file: nothing to stop, nothing raised
    assert not (state / launcher.GATEWAY_PID).exists()


@pytest.mark.parametrize("sig", [signal.SIGHUP, signal.SIGTERM])
def test_a_closed_terminal_still_releases_the_gateway(
    sig: signal.Signals, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: closing the window (SIGHUP) or a kill (SIGTERM) left the gateway running."""
    released: list[Path] = []
    monkeypatch.setattr(
        launcher, "attach", lambda _s, port, **_k: launcher.Gateway(launcher.url_for(port), True)
    )
    monkeypatch.setattr(launcher, "release", released.append)

    def app_running() -> None:
        with cli._gateway():
            os.kill(os.getpid(), sig)
            time.sleep(5)  # pragma: no cover - the signal ends the app first

    with pytest.raises(SystemExit) as ended:
        app_running()
    assert ended.value.code == 128 + sig
    assert len(released) == 1
    assert signal.getsignal(sig) is not cli._exit_on_signal
