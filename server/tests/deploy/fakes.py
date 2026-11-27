"""Fake service managers and a fake ssh, on PATH: the container has no systemd, launchd or users.

Each fake records its calls in ``$FAKE_STATE/calls.log``. With ``FAKE_RUN=1`` the service managers
really start what the unit (or plist) says, so a test sees the real gateway answer on its port.
The fake ssh runs the remote command here (``sh -c``), with stdin passed through, and serves
``-N -L`` forwards with a small TCP relay: everything but the SSH protocol itself.
"""

import os
import shutil
import socket
import sys
from pathlib import Path

COMMON = r"""
import json, os, plistlib, shlex, signal, subprocess, sys, time
from pathlib import Path
import psutil

STATE = Path(os.environ["FAKE_STATE"])
STATE.mkdir(parents=True, exist_ok=True)
with open(STATE / "calls.log", "a") as f:
    f.write(" ".join([Path(sys.argv[0]).name, *sys.argv[1:]]) + "\n")

def alive():
    try:
        pid = int((STATE / "pid").read_text())
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except Exception:
        return False

def launch(argv, extra):
    if os.environ.get("FAKE_RUN") != "1":
        (STATE / "pid").write_text(str(os.getpid()))
        return
    log = open(STATE / "service.out", "ab")
    p = subprocess.Popen(argv, env={**os.environ, **extra}, stdin=subprocess.DEVNULL,
                         stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    (STATE / "pid").write_text(str(p.pid))

def halt():
    try:
        pid = int((STATE / "pid").read_text())
    except Exception:
        return
    try:
        proc = psutil.Process(pid)
        if os.environ.get("FAKE_RUN") == "1":
            proc.terminate()
            proc.wait(25)
    except Exception:
        pass
    (STATE / "pid").unlink(missing_ok=True)
"""

SYSTEMCTL = (
    COMMON
    + r"""
args = sys.argv[1:]
assert args[0] == "--user", args
args = args[1:]
cmd = args[0]
config = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
unit = Path(config) / "systemd" / "user" / "enzo.service"

def start():
    if alive():
        return
    lines = unit.read_text().splitlines()
    argv = shlex.split(next(l for l in lines if l.startswith("ExecStart=")).split("=", 1)[1])
    extra = dict(l.split("=", 1)[1].split("=", 1) for l in lines if l.startswith("Environment="))
    launch(argv, extra)

if cmd == "show-environment":
    sys.exit(1 if os.environ.get("FAKE_NO_USER_MANAGER") else 0)
elif cmd in ("daemon-reload", "reset-failed"):
    pass
elif cmd == "enable":
    (STATE / "enabled").touch()
elif cmd == "disable":
    (STATE / "enabled").unlink(missing_ok=True)
    if "--now" in args:
        halt()
elif cmd == "start":
    start()
elif cmd == "restart":
    halt(); start()
elif cmd == "stop":
    halt()
elif cmd == "show":
    running = alive()
    active = os.environ.get("FAKE_ACTIVE") or ("active" if running else "inactive")
    pid = (STATE / "pid").read_text() if running else "0"
    print(f"ActiveState={active}\nSubState=running\nMainPID={pid}\nNRestarts=0")
    print("UnitFileState=" + ("enabled" if (STATE / "enabled").exists() else "disabled"))
    print("ActiveEnterTimestamp=Thu 2026-11-27 10:00:00 UTC")
else:
    sys.exit(f"fake systemctl: unknown {cmd}")
"""
)

LOGINCTL = (
    COMMON
    + r"""
args = sys.argv[1:]
linger = STATE / "linger"
if args[0] == "show-user":
    print(linger.read_text() if linger.exists() else "no")
elif args[0] == "enable-linger":
    if os.environ.get("FAKE_LINGER") != "allow":
        sys.exit("Could not enable linger: Access denied")
    linger.write_text("yes")
"""
)

SUDO = (
    COMMON
    + r"""
args = sys.argv[1:]
if args[0] != "-n" or os.environ.get("FAKE_SUDO") != "1":
    sys.exit("sudo: a password is required")
if args[1:3] == ["loginctl", "enable-linger"]:
    (STATE / "linger").write_text("yes")
"""
)

JOURNALCTL = (
    COMMON
    + r"""
print("enzo: Traceback (most recent call last)\nenzo: it broke on purpose")
"""
)

LAUNCHCTL = (
    COMMON
    + r"""
args = sys.argv[1:]
cmd = args[0]
loaded = STATE / "loaded"
if cmd == "print":
    target = args[1]
    if target.count("/") == 1:
        sys.exit(1 if os.environ.get("FAKE_NO_GUI") else 0)
    if not loaded.exists():
        sys.exit("Could not find service in domain")
    running = alive()
    print(f"{target} = {{")
    print("\tstate = " + ("running" if running else "not running"))
    if running:
        print("\tpid = " + (STATE / "pid").read_text())
    print("\truns = 1\n\tlast exit code = " + os.environ.get("FAKE_EXIT", "(never exited)") + "\n}")
elif cmd == "bootstrap":
    plist = plistlib.loads(Path(args[2]).read_bytes())
    loaded.write_text(args[2])
    launch(plist["ProgramArguments"], plist.get("EnvironmentVariables", {}))
elif cmd == "bootout":
    if not loaded.exists():
        sys.exit("Boot-out failed: 3: No such process")
    halt(); loaded.unlink()
elif cmd == "disable":
    (STATE / "disabled").touch()
elif cmd == "enable":
    (STATE / "disabled").unlink(missing_ok=True)
elif cmd == "kickstart":
    if not loaded.exists():
        sys.exit("Could not find service")
"""
)

SSH = r"""
import os, socket, subprocess, sys, threading
from pathlib import Path
STATE = Path(os.environ["FAKE_STATE"])
STATE.mkdir(parents=True, exist_ok=True)
args = sys.argv[1:]
with open(STATE / "ssh.log", "a") as f:
    f.write(" ".join(args) + "\n")
words, forward, i = [], None, 0
while i < len(args):
    a = args[i]
    if a in ("-o", "-p", "-i"):
        i += 2; continue
    if a == "-L":
        forward = args[i + 1]; i += 2; continue
    if a.startswith("-"):
        i += 1; continue
    words.append(a); i += 1
dest, command = words[0], words[1:]
if os.environ.get("FAKE_SSH_ERROR"):
    sys.stderr.write(os.environ["FAKE_SSH_ERROR"] + "\n"); sys.exit(255)
if forward is not None:
    lhost, lport, rhost, rport = forward.split(":")
    server = socket.create_server((lhost, int(lport)))
    def pipe(a, b):
        try:
            while data := a.recv(65536):
                b.sendall(data)
        except OSError:
            pass
        finally:
            for s in (a, b):
                try: s.shutdown(socket.SHUT_RDWR)
                except OSError: pass
    while True:
        client, _ = server.accept()
        try:
            upstream = socket.create_connection((rhost, int(rport)))
        except OSError:
            sys.stderr.write("channel 2: open failed: connect failed: Connection refused\n")
            sys.stderr.flush(); client.close(); continue
        threading.Thread(target=pipe, args=(client, upstream), daemon=True).start()
        threading.Thread(target=pipe, args=(upstream, client), daemon=True).start()
home = os.environ.get("FAKE_REMOTE_HOME") or os.environ["HOME"]
env = {**os.environ, "HOME": home}
sys.exit(subprocess.call(["sh", "-c", " ".join(command)], env=env, cwd=home))
"""


def install(bin_dir: Path, *names: str) -> Path:
    """Put the named fakes on ``bin_dir`` (created)."""
    bodies = {
        "systemctl": SYSTEMCTL,
        "loginctl": LOGINCTL,
        "sudo": SUDO,
        "journalctl": JOURNALCTL,
        "launchctl": LAUNCHCTL,
        "ssh": SSH,
    }
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        path = bin_dir / name
        path.write_text(f"#!{sys.executable}\n{bodies[name]}")
        path.chmod(0o755)
    return bin_dir


def enzo_command(bin_dir: Path) -> Path:
    """An `enzo` command that runs this checkout (what the service starts)."""
    path = bin_dir / "enzo"
    path.write_text(f'#!/bin/sh\nexec {sys.executable} -m enzo.cli "$@"\n')
    path.chmod(0o755)
    return path


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def calls(state: Path) -> list[str]:
    log = state / "calls.log"
    return log.read_text().splitlines() if log.exists() else []


def path_with(bin_dir: Path) -> str:
    keep = [str(bin_dir), os.path.dirname(sys.executable), "/usr/bin", "/bin"]
    return os.pathsep.join(keep) if shutil.which("sh") else str(bin_dir)
