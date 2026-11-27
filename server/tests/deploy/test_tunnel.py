"""The SSH side: ssh's errors in plain words, the machine check, and the shared forward the apps
use (a fake ssh relays the port, so the forward really carries the gateway's answers)."""

import json
import os
import subprocess
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from enzo import __version__, launcher
from enzo.deploy import probe, tunnel
from enzo.deploy.record import Deployment
from enzo.deploy.ssh import Remote, SshError, check_destination, explain
from tests.deploy import fakes


class Health(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = json.dumps({"status": "ok", "version": __version__, "env": "dev"}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        pass


@pytest.fixture
def there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Path, Deployment, str]]:
    """The gateway "there" answers on its own port; the forward listens on the deployment's."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    ssh = fakes.install(tmp_path / "bin", "ssh") / "ssh"
    monkeypatch.setenv("FAKE_STATE", str(tmp_path / "fake"))
    monkeypatch.setenv("FAKE_FORWARD_TO", str(server.server_address[1]))
    dep = Deployment(
        "me@box",
        fakes.free_port(),
        "/home/me/.local/bin/enzo",
        "Linux",
        "x86_64",
        __version__,
        "2026-11-27T00:00:00+00:00",
    )
    state = tmp_path / "state"
    try:
        yield state, dep, str(ssh)
    finally:
        tunnel.close(state)
        server.shutdown()
        server.server_close()


def spawned(tmp_path: Path) -> int:
    return sum("-N" in line for line in (tmp_path / "fake" / "ssh.log").read_text().splitlines())


def test_one_forward_is_shared_and_the_last_app_to_close_stops_it(
    there: tuple[Path, Deployment, str], tmp_path: Path
) -> None:
    state, dep, ssh = there
    assert tunnel.attach(state, dep, ssh=ssh)["version"] == __version__
    line = (tmp_path / "fake" / "ssh.log").read_text().splitlines()[0]
    assert f"-L 127.0.0.1:{dep.port}:127.0.0.1:{dep.port}" in line  # loopback on both ends
    assert "BatchMode=yes" in line  # keys or the agent only: never a password prompt
    other = subprocess.Popen(["sleep", "30"])  # noqa: S607 - another open app
    try:
        (tunnel.folder(state) / "clients" / str(other.pid)).touch()
        assert tunnel.attach(state, dep, ssh=ssh)["version"] == __version__
        assert spawned(tmp_path) == 1  # reused, not a second forward
        tunnel.release(state)
        assert launcher.health(dep.port) is not None  # the other app still uses it
    finally:
        other.kill()
        other.wait()
    tunnel.release(state)
    assert launcher.health(dep.port) is None
    assert not (tunnel.folder(state) / tunnel.PID).exists()


def test_a_dropped_forward_comes_back_while_an_app_is_open(
    there: tuple[Path, Deployment, str], tmp_path: Path
) -> None:
    state, dep, ssh = there
    tunnel.attach(state, dep, ssh=ssh)
    pid = int((tunnel.folder(state) / tunnel.PID).read_text())
    os.kill(pid, 9)  # the laptop slept and the connection died
    with tunnel.Keeper(state, dep, ssh=ssh, every_s=0.1):
        assert launcher_back(dep.port)
    assert spawned(tmp_path) == 2
    tunnel.release(state)


def launcher_back(port: int, seconds: float = 10) -> bool:
    import time  # noqa: PLC0415

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if launcher.health(port) is not None:
            return True
        time.sleep(0.1)
    return False


def test_a_port_another_program_holds_is_named_with_the_fix(
    there: tuple[Path, Deployment, str],
) -> None:
    state, dep, ssh = there
    import socket  # noqa: PLC0415

    with socket.create_server(("127.0.0.1", dep.port)), pytest.raises(tunnel.TunnelError) as err:
        tunnel.attach(state, dep, ssh=ssh)
    assert f"Port {dep.port} on this computer is used by another program" in str(err.value)
    assert "--port 8790" in str(err.value)


def test_a_refused_key_says_how_to_set_up_key_login(
    there: tuple[Path, Deployment, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    state, dep, ssh = there
    monkeypatch.setenv("FAKE_SSH_ERROR", "me@box: Permission denied (publickey).")
    with pytest.raises(SshError, match="ssh-copy-id me@box"):
        tunnel.attach(state, dep, ssh=ssh)


def test_enzo_not_answering_there_is_reported_and_the_forward_is_not_left_behind(
    there: tuple[Path, Deployment, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    state, dep, ssh = there
    monkeypatch.setenv("FAKE_FORWARD_TO", str(fakes.free_port()))  # nothing listens there
    monkeypatch.setattr(tunnel, "CONNECT_S", 2.0)
    with pytest.raises(tunnel.TunnelError, match="isn't answering"):
        tunnel.attach(state, dep, ssh=ssh)
    assert not (tunnel.folder(state) / tunnel.PID).exists()
    assert not launcher.port_taken(dep.port)


@pytest.mark.parametrize(
    ("stderr", "says"),
    [
        ("Permission denied (publickey,password).", "never asks for a password"),
        ("Host key verification failed.", "Connect once with `ssh me@box`"),
        ("@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @", "ssh-keygen -R box"),
        ("ssh: Could not resolve hostname box: Name or service not known", "Can't find"),
        ("ssh: connect to host box port 22: Connection refused", "refuses SSH"),
        ("ssh: connect to host box port 22: Connection timed out", "Can't reach box"),
        ("channel 2: open failed: administratively prohibited", "AllowTcpForwarding"),
        ("kex_exchange_identification: read: Connection reset", "Connection reset"),
    ],
)
def test_ssh_errors_in_plain_words(stderr: str, says: str) -> None:
    assert says in explain(stderr, "me@box")


@pytest.mark.parametrize("bad", ["-oProxyCommand=sh", "me@box; rm", "", "me@'box'", "a\nb"])
def test_only_a_machine_name_reaches_ssh(bad: str) -> None:
    with pytest.raises(SshError):
        check_destination(bad)
    with pytest.raises(SshError):
        Remote(bad).argv()


def test_the_machine_check_runs_in_sh_and_reads_back() -> None:
    done = subprocess.run(
        ["/bin/sh", "-s"],
        input=probe.SCRIPT,
        capture_output=True,
        text=True,
        check=True,
    )
    host = probe.parse(done.stdout)
    assert host.os == "Linux"
    assert host.home == os.environ["HOME"]
    assert host.disk_gb is not None
    assert host.disk_gb > 0


LINUX = """os=Linux
arch=x86_64
user=me
home=/home/me
disk_kb=41943040
mem_kb=2097152
distro=Ubuntu 24.04 LTS
curl=1
glibc=1
systemd=1
systemd_user=1
linger=no
"""


def test_a_usual_linux_server_passes_and_the_rest_say_what_stops_them() -> None:
    good = probe.parse(LINUX)
    assert probe.problems(good) == []
    assert good.summary == "Ubuntu 24.04 LTS, x86_64, 40 GB free"
    alpine = probe.parse(LINUX.replace("glibc=1\n", "").replace("systemd=1\n", ""))
    assert [p.split(":")[0].split(" (")[0] for p in probe.problems(alpine)] == [
        "It uses musl",
        "It doesn't use systemd, which Enzo needs to stay on there.",
    ]
    tiny = probe.parse(LINUX.replace("disk_kb=41943040", "disk_kb=1048576"))
    assert probe.problems(tiny) == ["Only 1 GB is free there; Enzo needs 2 GB."]
    mac = probe.parse("os=Darwin\narch=arm64\ncurl=1\nsleep=10\nuser=root\n")
    assert "automatic login" in probe.problems(mac)[0]
    notes = probe.notes(mac, __version__)
    assert any("never needs root" in n for n in notes)
    assert any("goes to sleep after 10 minutes" in n for n in notes)
    assert probe.problems(probe.parse("os=FreeBSD\n")) == [
        "It runs FreeBSD: Enzo runs on Linux and macOS."
    ]
