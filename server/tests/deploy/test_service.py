"""Enzo as an always-on service: the systemd unit and launchd agent, set up, checked, stopped and
removed through fake service managers on PATH (the real gateway runs when ``FAKE_RUN=1``)."""

import os
import plistlib
import shutil
import subprocess
from pathlib import Path

import pytest

from enzo import __version__, launcher
from enzo.accounts.vault import KEY_FILE, VaultUnavailableError, open_vault, read_key_file
from enzo.deploy import service
from tests.deploy import fakes

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, database_url: str) -> dict[str, Path]:
    """An always-on machine: its own home and Enzo folders, fake service managers on PATH."""
    bin_dir = fakes.install(
        tmp_path / "bin", "systemctl", "loginctl", "sudo", "journalctl", "launchctl"
    )
    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "fake"
    monkeypatch.setenv("PATH", fakes.path_with(bin_dir))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("FAKE_STATE", str(state))
    monkeypatch.setenv("ENZO_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ENZO_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ENZO_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ENZO_WORKSPACE_DIR", str(home / "Enzo"))
    monkeypatch.setenv("ENZO_DATABASE_URL", database_url)
    monkeypatch.setenv("ENZO_NOTIFY", "0")
    monkeypatch.delenv("ENZO_VAULT_KEY", raising=False)
    monkeypatch.setattr(service, "_home", lambda: home)
    return {"bin": bin_dir, "home": home, "state": state, "config": tmp_path / "config"}


def test_the_unit_restarts_on_failure_starts_at_boot_and_quotes_its_command() -> None:
    unit = service.systemd_unit(Path("/home/a b/.local/bin/enzo"), 8789)
    assert 'ExecStart="/home/a b/.local/bin/enzo" serve --service' in unit
    assert "Restart=on-failure" in unit
    assert "WantedBy=default.target" in unit
    assert "Environment=ENZO_PORT=8789" in unit
    assert "KillMode=mixed" in unit  # the gateway stops its own database
    odd = service.systemd_unit(Path("/home/x/100%$HOME/enzo"), 8789)
    assert 'ExecStart="/home/x/100%%$$HOME/enzo"' in odd  # no specifier or variable expansion


def test_systemd_itself_accepts_the_unit(tmp_path: Path) -> None:
    analyze = shutil.which("systemd-analyze")
    assert analyze is not None, "systemd-analyze checks the unit (Ubuntu has it)"
    exe = tmp_path / "enzo"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    unit = tmp_path / service.UNIT
    unit.write_text(service.systemd_unit(exe, 8789))
    done = subprocess.run(  # noqa: S603
        [analyze, "verify", "--man=no", str(unit)], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr
    assert "enzo.service" not in done.stderr, done.stderr  # no unknown key or bad value


def test_the_launch_agent_restarts_on_failure_and_starts_at_login(tmp_path: Path) -> None:
    plist = plistlib.loads(service.launchd_plist(Path("/Users/a/.local/bin/enzo"), 8790, tmp_path))
    assert plist["Label"] == "enzo.gateway"
    assert plist["ProgramArguments"] == ["/Users/a/.local/bin/enzo", "serve", "--service"]
    assert plist["EnvironmentVariables"] == {"ENZO_PORT": "8790"}
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] == {"SuccessfulExit": False}
    assert plist["Umask"] == 0o077


@pytest.mark.timeout(120)
def test_install_runs_the_gateway_from_the_unit_turns_on_lingering_and_makes_a_vault_key(
    machine: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_RUN", "1")
    monkeypatch.setenv("FAKE_LINGER", "allow")
    exe = fakes.enzo_command(machine["bin"])
    port = fakes.free_port()
    done = service.install(exe, port, mgr=service.Systemd(machine["home"]), wait_s=60)
    try:
        assert done.ok, done.problem
        assert done.linger is True
        assert done.vault == "created"
        unit = machine["home"] / ".config/systemd/user/enzo.service"
        assert f'ExecStart="{exe}" serve --service' in unit.read_text()
        calls = fakes.calls(machine["state"])
        assert "systemctl --user enable enzo.service" in calls
        assert "systemctl --user restart enzo.service" in calls
        assert any(c.startswith("loginctl enable-linger") for c in calls)
        assert launcher.health(port) == {"status": "ok", "version": __version__, "env": "dev"}
        key = machine["config"] / KEY_FILE
        assert key.stat().st_mode & 0o777 == 0o600
        assert type(open_vault(machine["config"], None)).__name__ == "EncryptedFileVault"
        found = service.status(mgr=service.Systemd(machine["home"]))
        assert (found.state, found.boot, found.port, found.gateway) == (
            "running",
            True,
            port,
            __version__,
        )
        assert found.scheduled == 0
        service.stop(mgr=service.Systemd(machine["home"]))
        assert launcher.health(port) is None
        stopped = service.status(mgr=service.Systemd(machine["home"]))
        assert (stopped.state, stopped.boot) == ("stopped", False)  # stays off after a restart
        assert stopped.journal == [
            "enzo: Traceback (most recent call last)",
            "enzo: it broke on purpose",
        ]
        assert service.start(mgr=service.Systemd(machine["home"]), wait_s=60)
        again = service.install(exe, port, mgr=service.Systemd(machine["home"]), wait_s=60)
        assert again.ok
        assert again.vault == "existing"
    finally:
        service.remove(mgr=service.Systemd(machine["home"]))
    assert not (machine["home"] / ".config/systemd/user/enzo.service").exists()
    assert service.recorded_port() is None
    assert launcher.health(port) is None


def test_lingering_refused_says_the_one_command_to_run_and_never_prompts_for_a_password(
    machine: dict[str, Path],
) -> None:
    exe = fakes.enzo_command(machine["bin"])
    done = service.install(
        exe,
        fakes.free_port(),
        mgr=service.Systemd(machine["home"]),
        wait_s=1,
        healthy=lambda _port: {"status": "ok", "version": __version__},
    )
    assert not done.ok
    assert done.linger is False
    assert done.problem is not None
    assert "sudo loginctl enable-linger" in done.problem
    sudo = [c for c in fakes.calls(machine["state"]) if c.startswith("sudo")]
    assert sudo
    assert all(c.startswith("sudo -n ") for c in sudo)


def test_passwordless_sudo_turns_lingering_on(
    machine: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_SUDO", "1")
    done = service.install(
        fakes.enzo_command(machine["bin"]),
        fakes.free_port(),
        mgr=service.Systemd(machine["home"]),
        wait_s=1,
        healthy=lambda _port: {"status": "ok", "version": __version__},
    )
    assert done.ok
    assert done.linger is True


def test_no_user_manager_or_a_taken_port_changes_nothing(
    machine: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    exe = fakes.enzo_command(machine["bin"])
    monkeypatch.setenv("FAKE_NO_USER_MANAGER", "1")
    done = service.install(exe, fakes.free_port(), mgr=service.Systemd(machine["home"]))
    assert not done.ok
    assert "systemd user services" in str(done.problem)
    monkeypatch.delenv("FAKE_NO_USER_MANAGER")
    with launcher_listening() as port:
        taken = service.install(exe, port, mgr=service.Systemd(machine["home"]))
    assert not taken.ok
    assert f"Port {port} is used" in str(taken.problem)
    assert not (machine["home"] / ".config/systemd/user/enzo.service").exists()
    assert not (machine["config"] / KEY_FILE).exists()


def test_a_service_that_keeps_failing_is_reported_with_its_last_lines(
    machine: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_LINGER", "allow")
    monkeypatch.setenv("FAKE_ACTIVE", "failed")
    monkeypatch.setattr(service, "FAILING_S", 0.5)
    done = service.install(
        fakes.enzo_command(machine["bin"]),
        fakes.free_port(),
        mgr=service.Systemd(machine["home"]),
        wait_s=30,
        healthy=lambda _port: None,
    )
    assert not done.ok
    assert "Enzo did not start there (failed)" in str(done.problem)
    assert "it broke on purpose" in str(done.problem)


@pytest.mark.timeout(120)
def test_on_a_mac_the_agent_is_loaded_in_the_login_session(
    machine: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_RUN", "1")
    exe = fakes.enzo_command(machine["bin"])
    port = fakes.free_port()
    mgr = service.Launchd(machine["home"])
    done = service.install(exe, port, mgr=mgr, wait_s=60)
    try:
        assert done.ok, done.problem
        plist = machine["home"] / "Library/LaunchAgents/enzo.gateway.plist"
        assert plistlib.loads(plist.read_bytes())["ProgramArguments"][0] == str(exe)
        uid = os.getuid()
        assert f"launchctl bootstrap gui/{uid} {plist}" in fakes.calls(machine["state"])
        assert launcher.health(port) is not None
        assert service.status(mgr=mgr).state == "running"
        service.stop(mgr=mgr)
        assert launcher.health(port) is None
        assert f"launchctl disable gui/{uid}/enzo.gateway" in fakes.calls(machine["state"])
    finally:
        service.remove(mgr=mgr)
    assert not (machine["home"] / "Library/LaunchAgents/enzo.gateway.plist").exists()


def test_a_mac_nobody_is_logged_in_to_says_to_log_in_once(
    machine: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_NO_GUI", "1")
    done = service.install(fakes.enzo_command(machine["bin"]), 8789, mgr=service.Launchd())
    assert not done.ok
    assert "automatic login" in str(done.problem)


def test_a_machine_with_accounts_in_its_keychain_keeps_them_there(tmp_path: Path) -> None:
    (tmp_path / "connections.json").write_text('{"connections": {"anthropic": {}}}')
    assert service.ensure_vault(tmp_path) == "keychain"
    assert not (tmp_path / KEY_FILE).exists()


def test_a_vault_key_others_can_read_is_refused(tmp_path: Path) -> None:
    assert service.ensure_vault(tmp_path) == "created"
    key = tmp_path / KEY_FILE
    key.chmod(0o644)
    with pytest.raises(VaultUnavailableError, match="chmod 600"):
        read_key_file(tmp_path)
    with pytest.raises(VaultUnavailableError):
        open_vault(tmp_path, None)


def test_the_packaged_installer_is_the_one_users_run() -> None:
    packaged = Path(service.__file__).with_name("install.sh")
    assert packaged.read_bytes() == (ROOT / "install.sh").read_bytes()


class launcher_listening:  # noqa: N801 - used as a context manager
    """Another program on a port."""

    def __enter__(self) -> int:
        import socket  # noqa: PLC0415

        self._sock = socket.create_server(("127.0.0.1", 0))
        return int(self._sock.getsockname()[1])

    def __exit__(self, *_exc: object) -> None:
        self._sock.close()
