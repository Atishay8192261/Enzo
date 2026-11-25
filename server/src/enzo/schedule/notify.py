"""A desktop notification, so a reminder reaches the user with no Enzo window open.

macOS: ``osascript`` (the text goes as arguments, never into the script). Linux: ``notify-send``
when the desktop has it. Elsewhere, or when it fails, nothing happens: the reminder is still in
the chat and on any open app.
"""

import asyncio
import logging
import shutil
import sys
from collections.abc import Awaitable, Callable

from enzo.tools.registry import plain_words

log = logging.getLogger("enzo.schedule")

type Notify = Callable[[str, str], Awaitable[bool]]

_SCRIPT = [
    "on run argv",
    "display notification (item 2 of argv) with title (item 1 of argv)",
    "end run",
]


def command(title: str, body: str, platform: str | None = None) -> list[str] | None:
    """The command that shows the notification here, or None when there is none."""
    title, body = plain_words(title, 60), plain_words(body, 200)  # no escape codes, one line
    if (platform or sys.platform) == "darwin" and shutil.which("osascript"):
        script = [arg for line in _SCRIPT for arg in ("-e", line)]
        return ["osascript", *script, title, body]
    if shutil.which("notify-send"):
        return ["notify-send", "--app-name=Enzo", title, body]
    return None


async def desktop(title: str, body: str) -> bool:
    argv = command(title, body)
    if argv is None:
        return False
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        async with asyncio.timeout(5):
            return await proc.wait() == 0
    except (OSError, TimeoutError):
        log.warning("notification failed", extra={"tool": argv[0]})
        return False


async def silent(_title: str, _body: str) -> bool:
    """No desktop notifications (``ENZO_NOTIFY=0``, and tests)."""
    return False
