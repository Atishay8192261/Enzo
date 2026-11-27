"""Enzo always on, on this machine (`enzo service`, ADR 0034).

- **Linux:** a systemd *user* service, ``~/.config/systemd/user/enzo.service``, plus lingering
  (``loginctl enable-linger``), so the user's service manager starts at boot and keeps Enzo running
  after they log out. No root needed.
- **macOS:** a launchd agent, ``~/Library/LaunchAgents/enzo.gateway.plist``, started at login (with
  automatic login turned on, that is at boot).
- Both restart Enzo when it fails. The gateway listens on 127.0.0.1 only: `enzo deploy` reaches it
  through an SSH tunnel, and nothing is exposed to the network.
- A machine that starts Enzo at boot has no unlocked keychain, so a fresh one gets an owner-only
  vault key file (``vault.key``, ADR 0009 amended): accounts stay encrypted at rest.

`enzo deploy` runs these commands on the always-on machine over SSH, with ``--json``; they can
also be used directly on a home machine.
"""

import contextlib
import json
import os
import platform
import plistlib
import pwd
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

import httpx2

from enzo import __version__, launcher, logs
from enzo.accounts.vault import VaultUnavailableError, create_key_file, read_key_file
from enzo.config import Settings
from enzo.paths import data_dir, private_dir, state_dir

#: Not the local default (8787): the user's computer may run its own Enzo while it reaches this
#: one through a tunnel on the same port number (the sign-in callbacks need the same number).
DEFAULT_PORT = 8789
UNIT = "enzo.service"
LABEL = "enzo.gateway"
#: In Enzo's state folder: this machine runs Enzo as a service, on this port.
RECORD = "service.json"
#: The first start sets up the database: about a minute on a laptop, longer on a small VM.
FIRST_START_S = 300.0
STATE = Literal["running", "starting", "stopped", "failed", "not installed"]


class ServiceError(Exception):
    """Why Enzo can't run as a service here. The message is shown to the user as is."""


@dataclass(slots=True)
class Status:
    installed: bool
    manager: str
    state: STATE
    #: Starts by itself at boot (Linux: enabled and lingering; macOS: loaded at login).
    boot: bool
    port: int | None = None
    pid: int | None = None
    restarts: int | None = None
    since: str | None = None
    #: Linux only: the user's services keep running with nobody logged in.
    linger: bool | None = None
    #: The Enzo version installed here, and the one the running gateway answers with.
    version: str = __version__
    gateway: str | None = None
    accounts: list[str] = field(default_factory=list)
    scheduled: int | None = None
    next: str | None = None
    disk_free_gb: float | None = None
    vault: str | None = None
    log: str | None = None
    #: The service manager's last lines, when Enzo is not running (why it stopped).
    journal: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Installed:
    ok: bool
    port: int
    manager: str
    version: str = __version__
    linger: bool | None = None
    #: "created" (a new vault key file), "existing", or "keychain".
    vault: str = "existing"
    problem: str | None = None
    notes: list[str] = field(default_factory=list)


class Manager(Protocol):
    name: str

    def problem(self) -> str | None:
        """Why this machine can't run Enzo as a service, or None."""
        ...

    def install(self, exe: Path, port: int) -> None: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def remove(self) -> None: ...
    def status(self) -> Status: ...
    def ensure_boot(self) -> tuple[bool | None, str | None]:
        """Make it start at boot: (lingering, a problem to report) — Linux only."""
        ...


def run(
    argv: list[str], *, env: dict[str, str] | None = None, timeout: float = 60
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed service-manager commands
        argv, capture_output=True, text=True, timeout=timeout, check=False, env=env
    )


def _home() -> Path:
    return Path.home()


def systemd_quote(value: str) -> str:
    """One ExecStart word: quoted, with systemd's specifiers (%) and variables ($) kept literal."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{escaped}"'


def systemd_unit(exe: Path, port: int) -> str:
    return f"""# Enzo always on: made by `enzo service install`, deleted by `enzo service remove`.
[Unit]
Description=Enzo, your personal AI agent (always on)
# Five failed starts within five minutes: stop trying (`enzo deploy status` says why).
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
ExecStart={systemd_quote(str(exe))} serve --service
Environment=ENZO_PORT={port}
Restart=on-failure
RestartSec=5
# Enzo stops its own database on the way out: signal only the gateway, then wait for it.
KillMode=mixed
TimeoutStopSec=30
UMask=0077

[Install]
WantedBy=default.target
"""


def launchd_plist(exe: Path, port: int, log: Path) -> bytes:
    return plistlib.dumps(
        {
            "Label": LABEL,
            "ProgramArguments": [str(exe), "serve", "--service"],
            "EnvironmentVariables": {"ENZO_PORT": str(port)},
            "RunAtLoad": True,
            # Restart when it fails (not after `enzo service stop`, which unloads it).
            "KeepAlive": {"SuccessfulExit": False},
            "ThrottleInterval": 10,
            "ExitTimeOut": 30,
            "Umask": 0o077,
            "ProcessType": "Standard",
            # What it prints before its own log is set up (a crash at start).
            "StandardOutPath": str(log),
            "StandardErrorPath": str(log),
        }
    )


class Systemd:
    name = "systemd"

    def __init__(self, home: Path | None = None) -> None:
        home = home or _home()
        config = os.environ.get("XDG_CONFIG_HOME") or str(home / ".config")
        self.unit_path = Path(config) / "systemd" / "user" / UNIT
        self.user = pwd.getpwuid(os.getuid()).pw_name

    def _env(self) -> dict[str, str]:
        """`systemctl --user` finds the user's manager through XDG_RUNTIME_DIR, which a plain
        SSH command may lack."""
        env = dict(os.environ)
        runtime = Path(f"/run/user/{os.getuid()}")
        if not env.get("XDG_RUNTIME_DIR") and runtime.is_dir():
            env["XDG_RUNTIME_DIR"] = str(runtime)
        return env

    def _ctl(self, *args: str) -> subprocess.CompletedProcess[str]:
        return run(["systemctl", "--user", *args], env=self._env())

    def problem(self) -> str | None:
        if shutil.which("systemctl") is None:
            return (
                "This machine doesn't use systemd, which Enzo needs to stay on (most Linux "
                "distributions do: Ubuntu, Debian, Fedora…)."
            )
        found = self._ctl("show-environment")
        if found.returncode != 0:
            why = _last_line(found.stderr) or "no answer"
            return (
                f"Your systemd user services aren't reachable here ({why}). Log in once over SSH "
                "(not with su or sudo) and try again."
            )
        return None

    def install(self, exe: Path, port: int) -> None:
        private_dir(self.unit_path.parent.parent.parent)  # ~/.config, if it was missing
        self.unit_path.parent.mkdir(parents=True, exist_ok=True)
        self.unit_path.write_text(systemd_unit(exe, port), encoding="utf-8")
        self._must("daemon-reload")
        self._must("enable", UNIT)
        self._ctl("reset-failed", UNIT)  # a unit that hit its start limit may start again
        self._must("restart", UNIT)  # restart: an update runs the new version at once

    def start(self) -> None:
        self._must("enable", UNIT)
        self._ctl("reset-failed", UNIT)
        self._must("start", UNIT)

    def stop(self) -> None:
        self._must("disable", "--now", UNIT)

    def remove(self) -> None:
        if self.unit_path.exists():
            self._ctl("disable", "--now", UNIT)
            self.unit_path.unlink()
        self._ctl("daemon-reload")
        self._ctl("reset-failed", UNIT)

    def _must(self, *args: str) -> None:
        done = self._ctl(*args)
        if done.returncode != 0:
            why = _last_line(done.stderr) or done.returncode
            msg = f"systemctl --user {' '.join(args)} failed: {why}"
            raise ServiceError(msg)

    def linger(self) -> bool:
        found = run(["loginctl", "show-user", self.user, "--property=Linger", "--value"])
        return found.returncode == 0 and found.stdout.strip() == "yes"

    def ensure_boot(self) -> tuple[bool | None, str | None]:
        if self.linger():
            return True, None
        run(["loginctl", "enable-linger", self.user])
        if not self.linger() and shutil.which("sudo"):
            # Only without a password prompt (-n): Enzo never asks for or handles a password.
            run(["sudo", "-n", "loginctl", "enable-linger", self.user])
        if self.linger():
            return True, None
        return False, (
            "Enzo would stop when you log out of this machine: its system doesn't let you turn on "
            f"lingering yourself. Run this once there: sudo loginctl enable-linger {self.user}"
        )

    def status(self) -> Status:
        installed = self.unit_path.exists()
        if not installed:
            return Status(installed=False, manager=self.name, state="not installed", boot=False)
        props = self._ctl(
            "show",
            UNIT,
            "--property=ActiveState,SubState,MainPID,NRestarts,UnitFileState,ActiveEnterTimestamp",
        )
        values = dict(line.split("=", 1) for line in props.stdout.splitlines() if "=" in line)
        active = values.get("ActiveState", "")
        state: STATE = {
            "active": "running",
            "activating": "starting",
            "reloading": "running",
            "failed": "failed",
        }.get(active, "stopped")  # type: ignore[assignment]  # the four states above
        if active == "activating" and values.get("SubState") == "auto-restart":
            state = "failed"  # it crashed and waits to try again
        pid = int(values.get("MainPID") or 0) or None
        restarts = int(values["NRestarts"]) if values.get("NRestarts", "").isdigit() else None
        linger = self.linger()
        boot = values.get("UnitFileState") == "enabled" and linger
        out = Status(
            installed=True,
            manager=self.name,
            state=state,
            boot=boot,
            pid=pid,
            restarts=restarts,
            since=values.get("ActiveEnterTimestamp") or None,
            linger=linger,
        )
        if state != "running":
            journal = run(
                ["journalctl", "--user", "--unit", UNIT, "-n", "12", "--no-pager", "-o", "cat"],
                env=self._env(),
            )
            out.journal = journal.stdout.splitlines()[-12:] if journal.returncode == 0 else []
        return out


class Launchd:
    name = "launchd"

    def __init__(self, home: Path | None = None) -> None:
        home = home or _home()
        self.plist_path = home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        self.domain = f"gui/{os.getuid()}"
        self.target = f"{self.domain}/{LABEL}"

    def problem(self) -> str | None:
        if shutil.which("launchctl") is None:
            return "launchctl is missing on this Mac."
        if run(["launchctl", "print", self.domain]).returncode != 0:
            return (
                "Nobody is logged in at this Mac's screen, and a Mac starts your own background "
                "apps only then. Log in there once, and turn on automatic login (System Settings → "
                "Users & Groups) so Enzo starts after a restart too. Then try again."
            )
        return None

    def install(self, exe: Path, port: int) -> None:
        log = private_dir(logs.log_dir(state_dir())) / "service.log"
        self.plist_path.parent.mkdir(parents=True, exist_ok=True)
        self.plist_path.write_bytes(launchd_plist(exe, port, log))
        run(["launchctl", "bootout", self.target])  # an older copy, if loaded
        _wait_until(lambda: not self._loaded(), 10)
        self._must("enable", self.target)
        self._must("bootstrap", self.domain, str(self.plist_path))
        self._must("kickstart", self.target)

    def start(self) -> None:
        self._must("enable", self.target)
        if not self._loaded():
            self._must("bootstrap", self.domain, str(self.plist_path))
        self._must("kickstart", self.target)

    def stop(self) -> None:
        run(["launchctl", "bootout", self.target])
        self._must("disable", self.target)  # stays off at the next login too

    def remove(self) -> None:
        run(["launchctl", "bootout", self.target])
        run(["launchctl", "enable", self.target])  # forget the "disabled" mark
        self.plist_path.unlink(missing_ok=True)

    def _loaded(self) -> bool:
        return run(["launchctl", "print", self.target]).returncode == 0

    def _must(self, *args: str) -> None:
        done = run(["launchctl", *args])
        if done.returncode != 0:
            why = _last_line(done.stderr) or _last_line(done.stdout) or str(done.returncode)
            msg = f"launchctl {args[0]} failed: {why}"
            raise ServiceError(msg)

    def ensure_boot(self) -> tuple[bool | None, str | None]:
        return None, None  # an agent in ~/Library/LaunchAgents loads at every login

    def status(self) -> Status:
        if not self.plist_path.exists():
            return Status(installed=False, manager=self.name, state="not installed", boot=False)
        found = run(["launchctl", "print", self.target])
        if found.returncode != 0:
            return Status(installed=True, manager=self.name, state="stopped", boot=False)
        fields: dict[str, str] = {}
        for line in found.stdout.splitlines():
            key, sep, value = line.strip().partition(" = ")
            if sep and key not in fields:
                fields[key] = value.strip()
        pid = int(fields["pid"]) if fields.get("pid", "").isdigit() else None
        running = fields.get("state") == "running" and pid is not None
        exit_code = fields.get("last exit code", "")
        failed = not running and exit_code not in ("", "0", "(never exited)")
        runs = int(fields["runs"]) if fields.get("runs", "").isdigit() else None
        out = Status(
            installed=True,
            manager=self.name,
            state="running" if running else "failed" if failed else "starting",
            boot=True,
            pid=pid,
            restarts=max(runs - 1, 0) if runs is not None else None,
        )
        if not running:
            log = logs.log_dir(state_dir()) / "service.log"
            out.journal = logs.last_lines(log, 12) if log.exists() else []
            out.journal = [line.rstrip("\n") for line in out.journal]
        return out


def manager(system: str | None = None) -> Manager:
    system = system or platform.system()
    if system == "Linux":
        return Systemd()
    if system == "Darwin":
        return Launchd()
    msg = f"Enzo can't run as a service on {system} yet (Linux and macOS can)."
    raise ServiceError(msg)


def record_path() -> Path:
    return state_dir() / RECORD


def recorded_port() -> int | None:
    """The port of the Enzo service on this machine, when it has one (the apps here use it)."""
    try:
        port = json.loads(record_path().read_text(encoding="utf-8"))["port"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return port if isinstance(port, int) else None


def ensure_vault(config: Path) -> str:
    """A service starts with no keychain unlocked: a fresh machine gets a vault key file.

    "created", "existing" (a key file was there), or "keychain" (this machine already keeps
    accounts in its keychain: left as it is).
    """
    if read_key_file(config) is not None:
        return "existing"
    if Settings().vault_key is not None:
        return "existing"
    connected = config / "connections.json"
    if (config / "vault.json").exists() or _has_connections(connected):
        return "keychain"
    private_dir(config)
    create_key_file(config)
    return "created"


def _has_connections(path: Path) -> bool:
    try:
        return bool(json.loads(path.read_text(encoding="utf-8")).get("connections"))
    except (OSError, ValueError, AttributeError):
        return False


def install(
    exe: Path,
    port: int,
    *,
    mgr: Manager | None = None,
    wait_s: float = FIRST_START_S,
    healthy: Callable[[int], dict[str, str] | None] = launcher.health,
) -> Installed:
    """Install (or update) the service, start it, and wait until the gateway answers."""
    mgr = mgr or manager()
    if problem := mgr.problem():
        return Installed(ok=False, port=port, manager=mgr.name, problem=problem)
    current = recorded_port()
    if healthy(port) is None and launcher.port_taken(port) and current != port:
        problem = (
            f"Port {port} is used by another program on this machine. Choose another with "
            "--port (for example --port 8790)."
        )
        return Installed(ok=False, port=port, manager=mgr.name, problem=problem)
    if current is not None and current != port:
        mgr.stop()  # moving to another port: the old one is released first
    cfg = Settings()
    vault = ensure_vault(cfg.config_dir)
    notes = []
    if vault == "keychain":
        notes.append(
            "Accounts here stay in this machine's keychain, which a service started at boot "
            "may not be able to open. If replies say an account isn't connected, connect it again."
        )
    mgr.install(exe, port)
    _write_record(port, mgr.name)
    linger, problem = mgr.ensure_boot()
    answered = _wait_healthy(port, mgr, wait_s, healthy)
    if answered is None:
        status = mgr.status()
        tail = "\n".join(status.journal[-6:])
        problem = f"Enzo did not start there ({status.state})." + (f"\n{tail}" if tail else "")
    elif answered.get("version") != __version__:
        problem = f"Another Enzo ({answered.get('version')}) answers on port {port}, not this one."
    return Installed(
        ok=problem is None,
        port=port,
        manager=mgr.name,
        linger=linger,
        vault=vault,
        problem=problem,
        notes=notes,
    )


#: A start that keeps failing this long is reported, not waited out (systemd retries meanwhile).
FAILING_S = 20.0


def _wait_healthy(
    port: int, mgr: Manager, wait_s: float, healthy: Callable[[int], dict[str, str] | None]
) -> dict[str, str] | None:
    began = time.monotonic()
    failing_since: float | None = None
    while time.monotonic() - began < wait_s:
        answered = healthy(port)
        if answered is not None:
            return answered
        if mgr.status().state == "failed":
            failing_since = failing_since or time.monotonic()
            if time.monotonic() - failing_since > FAILING_S:
                return None
        else:
            failing_since = None
        time.sleep(0.5)
    return None


def _write_record(port: int, manager_name: str) -> None:
    path = private_dir(state_dir()) / RECORD
    body = {"port": port, "manager": manager_name, "installed_at": datetime.now(UTC).isoformat()}
    path.write_text(json.dumps(body) + "\n", encoding="utf-8")


def status(*, mgr: Manager | None = None) -> Status:
    """How the service is, with what the gateway itself says when it answers."""
    mgr = mgr or manager()
    out = mgr.status()
    out.port = recorded_port()
    out.log = str(logs.log_dir(state_dir()) / logs.LOG_FILE)
    out.vault = _vault_kind(Settings().config_dir)
    with contextlib.suppress(OSError):
        out.disk_free_gb = round(shutil.disk_usage(_home()).free / 1e9, 1)
    if out.port is None:
        return out
    answered = launcher.health(out.port)
    if answered is None:
        return out
    out.gateway = answered.get("version")
    base = launcher.url_for(out.port)
    try:
        rows = httpx2.get(f"{base}/connections", timeout=3.0).json()
        out.accounts = [
            f"{r['provider']} ({r['method']})" if r.get("method") else r["provider"]
            for r in rows
            if r.get("connected")
        ]
    except (httpx2.HTTPError, ValueError, KeyError, TypeError):
        out.accounts = []
    waiting = launcher.upcoming(out.port)
    if waiting:
        out.scheduled, out.next = waiting
    else:
        out.scheduled = 0
    return out


def _vault_kind(config: Path) -> str:
    try:
        key = read_key_file(config)
    except VaultUnavailableError as exc:
        return f"unusable: {exc}"
    return "encrypted file" if key or Settings().vault_key else "keychain"


def stop(*, mgr: Manager | None = None) -> None:
    (mgr or manager()).stop()


def start(*, mgr: Manager | None = None, wait_s: float = 90) -> bool:
    mgr = mgr or manager()
    mgr.start()
    port = recorded_port()
    if port is None:
        return False
    return _wait_until(lambda: launcher.health(port) is not None, wait_s)


def remove(*, delete_data: bool = False, mgr: Manager | None = None) -> list[str]:
    """Stop and remove the service. With ``delete_data`` also Enzo's own folders here (the
    database, accounts and logs); the user's ~/Enzo folder is left. Returns what was deleted."""
    mgr = mgr or manager()
    mgr.remove()
    record_path().unlink(missing_ok=True)
    deleted: list[str] = []
    if delete_data:
        for folder in (data_dir(), state_dir(), Settings().config_dir):
            if folder.exists():
                shutil.rmtree(folder)
                deleted.append(str(folder))
    return deleted


def _wait_until(ready: Callable[[], bool], seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if ready():
            return True
        time.sleep(0.25)
    return ready()


def _last_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def this_enzo() -> Path:
    """The `enzo` command this process runs as: what the service starts."""
    exe = Path(os.path.abspath(sys.argv[0]))
    if exe.name != "enzo" or not os.access(exe, os.X_OK):
        found = shutil.which("enzo")
        if found is None:
            msg = "Can't find the enzo command to run as a service: run ~/.local/bin/enzo service"
            raise ServiceError(msg)
        exe = Path(found)
    return exe


def to_json(value: Status | Installed) -> str:
    return json.dumps(asdict(value))


def describe(found: Status) -> str:
    """The status as lines for a person: ✓ fine, · worth knowing, ✗ a problem."""
    if not found.installed:
        return "· Enzo isn't set up as a service here (`enzo deploy` does it from your computer)."
    lines = []
    if found.state == "running" and found.gateway:
        restarts = f" · restarted {found.restarts} times" if found.restarts else ""
        boot = "starts at boot" if found.boot else "does NOT start at boot"
        lines.append(f"✓ Running Enzo {found.gateway} on 127.0.0.1:{found.port} · {boot}{restarts}")
        accounts = ", ".join(found.accounts) or "none yet (/login chatgpt)"
        lines.append(f"· Accounts: {accounts}")
        if found.scheduled:
            lines.append(f"· Scheduled: {found.scheduled}, next {found.next}")
    else:
        lines.append(f"✗ Enzo is {found.state} ({found.manager}).")
        lines.extend(f"    {line}" for line in found.journal[-6:])
    if found.linger is False:
        lines.append(
            "✗ Lingering is off: Enzo stops when you log out (sudo loginctl enable-linger)."
        )
    if found.disk_free_gb is not None:
        lines.append(f"· Disk: {found.disk_free_gb:g} GB free · log: {found.log}")
    return "\n".join(lines)
