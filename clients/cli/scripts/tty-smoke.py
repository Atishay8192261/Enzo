"""Real-terminal smoke test for the Enzo CLI.

Runs the app in a pseudo-terminal (it needs a TTY), types fast like a person or a paste, and reads
the screen through a terminal emulator (pyte), so cursor-addressed redraws are read like a user sees
them. Needs a running gateway. Usage, from clients/cli:

    uv run --no-project --with pyte==0.8.2 python -I scripts/tty-smoke.py . "$(mktemp -d)"
"""

import os
import pty
import select
import sys
import time

import pyte

COLS, ROWS = 100, 30
cli_dir, cfg = sys.argv[1], sys.argv[2]
term = pyte.Screen(COLS, ROWS)
stream = pyte.ByteStream(term)

pid, fd = pty.fork()
if pid == 0:
    os.chdir(cli_dir)
    os.environ.update(ENZO_CONFIG_DIR=cfg, COLUMNS=str(COLS), LINES=str(ROWS), TERM="xterm-256color")
    os.execvp("pnpm", ["pnpm", "-s", "start"])

import fcntl  # noqa: E402
import struct  # noqa: E402
import termios  # noqa: E402

fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))


KEY = "fake-smoke-000000000000"  # never a real key; must never be drawn
leaks: list[str] = []


def screen_text() -> str:
    return "\n".join(term.display)


def pump(timeout: float = 0.2) -> None:
    r, _, _ = select.select([fd], [], [], timeout)
    if r:
        try:
            data = os.read(fd, 65536)
        except OSError:
            return
        stream.feed(data)
        if KEY in screen_text() or KEY.encode() in data:
            leaks.append(screen_text())
        if b"\x1b[6n" in data:  # cursor position request: answer like a real terminal
            os.write(fd, f"\x1b[{term.cursor.y + 1};{term.cursor.x + 1}R".encode())


def wait_for(text: str, timeout: float = 20) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        pump()
        if text in screen_text():
            return True
    return False


results = {"prompt shown": wait_for("Message Enzo")}
for ch in "hello enzo":  # fast, like a paste-happy human
    os.write(fd, ch.encode())
os.write(fd, b"\r")
results["honest failure shown"] = wait_for("/login anthropic")  # how to connect, either way
results["typed line kept"] = "you: hello enzo" in screen_text()
for ch in "/login anthropic":
    os.write(fd, ch.encode())
os.write(fd, b"\r")
results["hidden key prompt"] = wait_for("Paste your anthropic API key (hidden)")
os.write(fd, b"\x1b[200~" + KEY.encode() + b"\n\x1b[201~")  # bracketed paste, as terminals send
results["paste counted"] = wait_for(f"{len(KEY)} characters")
os.write(fd, b"\r")
# A real provider rejects the fake key; offline, the gateway says it could not check it.
end = time.time() + 30
while time.time() < end and not any(
    t in screen_text() for t in ("rejected this key", "could not check the key")
):
    pump()
results["provider answered, nothing saved"] = "rejected this key" in screen_text() or (
    "could not check the key" in screen_text()
)
results["key never on screen"] = not leaks
for ch in "/models":
    os.write(fd, ch.encode())
os.write(fd, b"\r")
results["catalog by tier"] = wait_for("balanced (everyday default):")
os.write(fd, b"/quit\r")
end = time.time() + 5
exited = False
while time.time() < end and not exited:
    pump(0.1)
    done, _ = os.waitpid(pid, os.WNOHANG)
    exited = done == pid
results["exited on /quit"] = exited
saved = os.path.join(cfg, "conversation")
results["conversation remembered"] = os.path.exists(saved)

for name, ok in results.items():
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
if not all(results.values()):
    print("--- screen ---\n" + screen_text())
    sys.exit(1)
