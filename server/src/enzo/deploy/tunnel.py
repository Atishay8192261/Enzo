"""One SSH port forward to the always-on Enzo, shared by every app open here.

``127.0.0.1:<port>`` here goes to ``127.0.0.1:<port>`` there, so nothing listens beyond loopback
on either machine, and a sign-in page that comes back to ``127.0.0.1:<port>/auth/…`` in this
computer's browser reaches the gateway there (ChatGPT and Google allow any loopback port).

Like the launcher's gateway: the first app starts the forward (a detached ``ssh -N``), each app
registers its pid, and the last one to close stops it. While an app is open, a keeper restarts
the forward when it drops (the laptop slept, the network changed); the apps retry on their own.
"""

import contextlib
import fcntl
import os
import signal
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import psutil

from enzo import launcher
from enzo.deploy.record import Deployment
from enzo.deploy.ssh import Remote, SshError, explain
from enzo.paths import private_dir

FOLDER = "remote"
PID = "tunnel.pid"
LOG = "tunnel.log"
#: Connecting, and the gateway there answering through the forward.
CONNECT_S = 25.0

_children: dict[int, subprocess.Popen[bytes]] = {}


def local_port(dep: Deployment) -> int:
    """The forward's port here: the gateway's own number there (sign-in pages come back to it).
    ``ENZO_TUNNEL_PORT`` moves it, only for trying both ends on one machine."""
    return int(os.environ.get("ENZO_TUNNEL_PORT") or dep.port)


class TunnelError(Exception):
    """Why the forward can't be used. The message is shown to the user as is."""


def folder(state: Path) -> Path:
    return state / FOLDER


def attach(state: Path, dep: Deployment, *, ssh: str = "ssh") -> dict[str, str]:
    """Use the forward (starting it if needed) and count this app as one of its users. Returns
    what the gateway there answered (its version)."""
    here = private_dir(folder(state))
    private_dir(here / "clients")
    with _locked(here):
        answered = _ensure(here, dep, ssh, wait_s=CONNECT_S)
        (here / "clients" / str(os.getpid())).touch()
    return answered


def release(state: Path) -> None:
    here = folder(state)
    if not here.exists():
        return
    with _locked(here):
        (here / "clients" / str(os.getpid())).unlink(missing_ok=True)
        if not _clients(here):
            _stop(here)


def close(state: Path) -> bool:
    """Stop the forward whoever uses it (the always-on Enzo is being stopped or removed)."""
    here = folder(state)
    if not here.exists():
        return False
    with _locked(here):
        pid = _pid(here)
        _stop(here)
        return pid is not None


def keep(state: Path, dep: Deployment, *, ssh: str = "ssh") -> bool:
    """Start the forward again if it dropped (no waiting: the apps retry). True: it is up."""
    here = private_dir(folder(state))
    with _locked(here):
        if _pid(here) is not None:
            return True
        if launcher.port_taken(local_port(dep)):
            return False
        _spawn(here, dep, ssh)
        return True


class Keeper:
    """While an app is open: restart the forward whenever it drops."""

    def __init__(
        self, state: Path, dep: Deployment, *, ssh: str = "ssh", every_s: float = 2.0
    ) -> None:
        self._args = (state, dep, ssh)
        self._every = every_s
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="enzo-tunnel", daemon=True)

    def __enter__(self) -> "Keeper":
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=10)

    def _run(self) -> None:
        state, dep, ssh = self._args
        while not self._stop.wait(self._every):
            with contextlib.suppress(OSError, SshError, TunnelError):
                keep(state, dep, ssh=ssh)


def _ensure(here: Path, dep: Deployment, ssh: str, *, wait_s: float) -> dict[str, str]:
    if _pid(here) is None:
        if launcher.port_taken(local_port(dep)):
            msg = (
                f"Port {local_port(dep)} on this computer is used by another program, so Enzo "
                f"can't reach {dep.name} through it. Close that program, or move Enzo there to "
                f"another port: enzo deploy {dep.destination} --port 8790"
            )
            raise TunnelError(msg)
        _spawn(here, dep, ssh)
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        answered = launcher.health(local_port(dep))
        if answered is not None:
            return answered
        if _pid(here) is None:  # ssh gave up: say why
            raise SshError(explain(_log(here), dep.destination))
        time.sleep(0.25)
    said = _log(here)
    _stop(here)
    if "administratively prohibited" in said:  # the SSH server forbids forwarding
        raise SshError(explain(said, dep.destination))
    msg = f"Enzo on {dep.name} isn't answering (SSH works). See why with: enzo deploy status"
    raise TunnelError(msg)


def _spawn(here: Path, dep: Deployment, ssh: str) -> None:
    forward = f"127.0.0.1:{local_port(dep)}:127.0.0.1:{dep.port}"
    argv = Remote(dep.destination, ssh=ssh).argv(
        "-N",
        "-o",
        "ExitOnForwardFailure=yes",
        # Its own connection, never a shared master: this process is the forward.
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPath=none",
        "-L",
        forward,
    )
    with (here / LOG).open("wb") as log:
        process = subprocess.Popen(  # noqa: S603 - the user's own ssh
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=log,
            start_new_session=True,  # outlives this app's terminal; stopped by the last release
        )
    _children[process.pid] = process
    (here / PID).write_text(f"{process.pid}\n", encoding="utf-8")


def _stop(here: Path) -> None:
    pid = _pid(here)
    if pid is not None:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _alive(pid):
            time.sleep(0.05)
        if _alive(pid):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
    for child_pid, child in list(_children.items()):
        if child.poll() is not None or child_pid == pid:
            with contextlib.suppress(subprocess.TimeoutExpired):
                child.wait(timeout=5)
            _children.pop(child_pid, None)
    (here / PID).unlink(missing_ok=True)


def _pid(here: Path) -> int | None:
    try:
        pid = int((here / PID).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    child = _children.get(pid)
    if child is not None and child.poll() is not None:  # ours, and it ended: reap it
        _children.pop(pid, None)
        return None
    return pid if _is_forward(pid) else None


def _is_forward(pid: int) -> bool:
    """Alive, and an ssh forward (not another program that got the pid after a restart)."""
    try:
        process = psutil.Process(pid)
        words = process.cmdline()
        return process.status() != psutil.STATUS_ZOMBIE and "-N" in words and "-L" in words
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def _alive(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _clients(here: Path) -> list[int]:
    alive = []
    for entry in (here / "clients").iterdir():
        if entry.name.isdigit() and _alive(int(entry.name)):
            alive.append(int(entry.name))
        else:
            entry.unlink(missing_ok=True)
    return alive


def _log(here: Path) -> str:
    try:
        return (here / LOG).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


@contextlib.contextmanager
def _locked(here: Path) -> Iterator[None]:
    with (here / "tunnel.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
