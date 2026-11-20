"""Release smoke test: the compiled terminal app starts and draws a screen.

`--version` alone would not prove the native renderer (OpenTUI's libopentui) loads, so the binary
runs in a pseudo-terminal against a gateway that isn't there, and must draw its offline message
("Can't reach Enzo at 127.0.0.1:9").
Standard library only. Usage: python smoke_tui.py <binary> <version>
"""

import os
import pty
import select
import subprocess
import sys
import time

binary, version = sys.argv[1], sys.argv[2]
MESSAGE = b"reach Enzo at"

shown = subprocess.run([binary, "--version"], capture_output=True, text=True, check=True).stdout
if shown.strip() != f"enzo-tui {version}":
    sys.exit(f"--version printed {shown.strip()!r}, expected 'enzo-tui {version}'")

pid, fd = pty.fork()
if pid == 0:
    os.environ.update(ENZO_GATEWAY_URL="http://127.0.0.1:9", TERM="xterm-256color")
    os.execv(binary, [binary])

seen = b""
deadline = time.monotonic() + 30
while time.monotonic() < deadline and MESSAGE not in seen:
    ready, _, _ = select.select([fd], [], [], 0.5)
    if ready:
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            break
        seen += chunk
        if b"\x1b[6n" in chunk:  # cursor position query: answer like a terminal
            os.write(fd, b"\x1b[1;1R")
os.kill(pid, 9)
os.waitpid(pid, 0)
if MESSAGE not in seen:
    sys.exit(f"the terminal app drew no screen; it wrote: {seen[-400:]!r}")
print(f"ok: {binary} {version} draws its screen")
