"""Real-terminal smoke test for the Enzo CLI.

Runs the Ink app in a pseudo-terminal (it needs a TTY), types fast like a person or a paste, and
reads the screen. Needs a running gateway. Usage, from clients/cli:

    python3 -I scripts/tty-smoke.py . "$(mktemp -d)"
"""
import os, pty, re, select, sys, time

cli_dir, cfg = sys.argv[1], sys.argv[2]
pid, fd = pty.fork()
if pid == 0:
    os.chdir(cli_dir)
    os.environ.update(ENZO_CONFIG_DIR=cfg, FORCE_COLOR="0")
    os.execvp("pnpm", ["pnpm", "-s", "start"])

screen = ""
def read_until(pattern, timeout=20):
    global screen
    end = time.time() + timeout
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.2)
        if r:
            try:
                screen += os.read(fd, 65536).decode(errors="replace")
            except OSError:
                break
        if re.search(pattern, re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", screen)):
            return True
    return False

ok = read_until(r"Message Enzo")
print("prompt shown:", ok)
for ch in "hello enzo":        # type fast, like a paste-happy human
    os.write(fd, ch.encode())
os.write(fd, b"\r")
print("honest failure shown:", read_until(r"no API key: set ANTHROPIC_API_KEY"))
print("first keystroke kept:", "hello enzo" in re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", screen))
os.write(fd, b"/quit\r")
time.sleep(1.5)
done, status = os.waitpid(pid, os.WNOHANG)
print("exited on /quit:", done == pid)
print("remembered conversation:", open(os.path.join(cfg, "conversation")).read().strip()[:8] + "…")
