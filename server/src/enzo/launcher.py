"""Run the gateway for the apps: start it when one opens, share it, stop it after the last closes.

Each open app (``enzo``, ``enzo web``) registers its pid. The gateway the launcher starts writes its
own pid file; only such a gateway is ever stopped here, never one a developer started by hand. A
file lock keeps two apps opening or closing at once from racing.
"""

import contextlib
import fcntl
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx2
import psutil

from enzo import __version__
from enzo.logs import LOG_FILE, STARTUP_LOG, log_dir
from enzo.paths import private_dir

#: Gateways this process started: kept so they can be reaped when stopped (no zombie left).
_children: dict[int, subprocess.Popen[bytes]] = {}

GATEWAY_PID = "gateway.pid"
START_TIMEOUT_S = 90.0
#: The first start also sets up the bundled database: about a minute on a laptop (#56).
FIRST_START_TIMEOUT_S = 300.0


class LauncherError(Exception):
    """Why Enzo could not start. The message is shown to the user as is."""


@dataclass(frozen=True, slots=True)
class Gateway:
    url: str
    #: Started for this app (False: it was already running).
    started: bool


def url_for(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def health(port: int) -> dict[str, str] | None:
    """The gateway's /healthz answer, or None when nothing (or not Enzo) answers there."""
    try:
        reply = httpx2.get(f"{url_for(port)}/healthz", timeout=1.0)
    except httpx2.HTTPError:
        return None
    if reply.status_code != httpx2.codes.OK:
        return None
    body = reply.json()
    return body if isinstance(body, dict) and body.get("status") == "ok" else None


def attach(
    state: Path, port: int, *, say: Callable[[str], None] = print, first_start: bool = False
) -> Gateway:
    """Use the running gateway, or start one; then count this app as one of its users.

    ``first_start``: the database is not set up yet, which the gateway does before it answers.
    """
    private_dir(state / "clients")
    with _locked(state):
        info = health(port)
        managed = _gateway_pid(state)
        if info is not None and info.get("version") != __version__ and managed is not None:
            say(f"Restarting Enzo's gateway for version {__version__}…")
            _stop(state, managed)
            info = None
        started = False
        if info is None:
            if port_taken(port):
                msg = (
                    f"Port {port} is used by another program, so Enzo can't start there. "
                    "Close it, or choose another port with ENZO_PORT=8788."
                )
                raise LauncherError(msg)
            if first_start:
                say("Starting Enzo. First start only: setting up your database (about a minute)…")
            else:
                say("Starting Enzo…")
            _start(state, port, FIRST_START_TIMEOUT_S if first_start else START_TIMEOUT_S)
            started = True
        (state / "clients" / str(os.getpid())).touch()
    return Gateway(url_for(port), started)


def release(state: Path) -> None:
    """This app is closing. If it was the last, stop the gateway the launcher started."""
    with _locked(state):
        (state / "clients" / str(os.getpid())).unlink(missing_ok=True)
        if _open_clients(state):
            return
        pid = _gateway_pid(state)
        if pid is not None:
            _stop(state, pid)


def write_own_pid(state: Path) -> None:
    """Called by a gateway started by the launcher, so the launcher knows it may stop it."""
    path = private_dir(state) / GATEWAY_PID
    path.write_text(f"{os.getpid()}\n", encoding="utf-8")


def forget_own_pid(state: Path) -> None:
    path = state / GATEWAY_PID
    with contextlib.suppress(OSError, ValueError):
        if int(path.read_text(encoding="utf-8")) == os.getpid():
            path.unlink()


def _start(state: Path, port: int, timeout_s: float = START_TIMEOUT_S) -> None:
    # The gateway logs to its own file once it is up; what it prints before that (a crash at
    # start) lands here, kept for the latest start only.
    logs = private_dir(log_dir(state))
    startup = logs / STARTUP_LOG
    with startup.open("wb") as out:
        process = subprocess.Popen(
            [sys.executable, "-m", "enzo.cli", "serve", "--managed"],
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # survives the app's terminal; stopped by release()
            env={**os.environ, "ENZO_PORT": str(port)},
        )
    _children[process.pid] = process
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if health(port) is not None:
            return
        if process.poll() is not None:
            break
        time.sleep(0.25)
    log = startup if _tail(startup) else logs / LOG_FILE
    msg = f"Enzo's gateway did not start. The end of its log ({log}):\n{_tail(log) or '(empty)'}"
    raise LauncherError(msg)


def _stop(state: Path, pid: int) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and _alive(pid):
        time.sleep(0.1)
    if _alive(pid):
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
    child = _children.pop(pid, None)
    if child is not None:
        child.wait(timeout=5)  # reap it: a stopped child must not linger as a zombie
    (state / GATEWAY_PID).unlink(missing_ok=True)


def _gateway_pid(state: Path) -> int | None:
    try:
        pid = int((state / GATEWAY_PID).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return pid if _alive(pid) else None


def _open_clients(state: Path) -> list[int]:
    alive = []
    for entry in (state / "clients").iterdir():
        if entry.name.isdigit() and _alive(int(entry.name)):
            alive.append(int(entry.name))
        else:
            entry.unlink(missing_ok=True)  # an app that crashed: it no longer counts
    return alive


def _alive(pid: int) -> bool:
    """Running, and not a zombie (a process that ended but was not reaped yet)."""
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def port_taken(port: int) -> bool:
    with socket.socket() as probe:
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _tail(log: Path, lines: int = 15) -> str:
    try:
        return "\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


@contextlib.contextmanager
def _locked(state: Path) -> Iterator[None]:
    with (private_dir(state) / "launcher.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
