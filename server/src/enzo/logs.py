"""Enzo's own log: one JSON line per step, so a turn can be read back (#48, spec §1.4).

    {"ts": "…", "level": "info", "event": "brain.call", "conversation": "e74f…",
     "turn": "t_9f…", "provider": "openai", "model": "gpt-6.2-sol", "in": 1596, "out": 29, …}

Lines name what happened, never what was said: no message text, no tool arguments or results,
no keys or tokens (spec §11.3). The conversation and turn come from context variables, set once
per run, so every line inside it carries them (and the same `turn_id` is on the ledger rows).

An installed Enzo writes ``~/.local/state/enzo/logs/gateway.log``, rotated at 5 MB with three old
files kept (`enzo logs` shows it); a developer's gateway writes the same lines to stderr.
"""

import io
import json
import logging
import os
import re
import sys
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterator
from contextvars import ContextVar
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TextIO

from enzo.paths import private_dir

LOG_DIR = "logs"
LOG_FILE = "gateway.log"
#: What the gateway printed before its log was set up (a crash at start), kept per start.
STARTUP_LOG = "startup.log"
MAX_BYTES = 5 * 1024 * 1024
BACKUPS = 3

#: The conversation and the run (one send or resume) that the current code works for.
conversation_var: ContextVar[str | None] = ContextVar("enzo_conversation", default=None)
turn_var: ContextVar[str | None] = ContextVar("enzo_turn", default=None)

#: Loggers whose lines go to Enzo's log: Enzo's own, and the HTTP server's.
LOGGERS = ("enzo", "uvicorn", "uvicorn.error", "uvicorn.access")

#: Record fields that are not the line's own: logging's, and uvicorn's coloured copy of the message.
_STANDARD = set(logging.makeLogRecord({}).__dict__) | {
    "message",
    "asctime",
    "taskName",
    "color_message",
}


def new_turn_id() -> str:
    return f"t_{uuid.uuid4().hex[:12]}"


def log_dir(state: Path) -> Path:
    return state / LOG_DIR


def _http(record: logging.LogRecord) -> dict[str, object] | None:
    """Uvicorn's access line as fields: method, path (never its query) and status."""
    args = record.args
    if record.name != "uvicorn.access" or not isinstance(args, tuple) or len(args) != 5:
        return None
    _client, method, path, _version, status = args
    return {"event": "http", "method": method, "path": str(path).split("?", 1)[0], "status": status}


class QuietHealth(logging.Filter):
    """Health checks answered OK are routine (the apps ask often): they are not logged."""

    def filter(self, record: logging.LogRecord) -> bool:
        http = _http(record)
        if http is None:
            return True
        return not (str(http["path"]).endswith("/healthz") and http["status"] == 200)


class JsonLines(logging.Formatter):
    """A record as one JSON object: time, level, event, its context and its extra fields."""

    def format(self, record: logging.LogRecord) -> str:
        line: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
        }
        http = _http(record)
        if http is not None:
            line.update(http)
        else:
            line["event"] = record.getMessage()
            if not record.name.startswith("enzo"):
                line["logger"] = record.name
        for key, var in (("conversation", conversation_var), ("turn", turn_var)):
            value = var.get()
            if value is not None:
                line[key] = value
        for key, value in record.__dict__.items():
            if key not in _STANDARD and not key.startswith("_"):
                line[key] = value
        if record.exc_info:
            line["error"] = self.formatException(record.exc_info)
        return json.dumps(line, default=str, ensure_ascii=False)


class _PrivateFile(RotatingFileHandler):
    """A log file only its owner can read, as is each new one after a rotation."""

    def _open(self) -> io.TextIOWrapper:
        def private(path: str, flags: int) -> int:
            return os.open(path, flags, 0o600)

        handle = open(  # noqa: SIM115 - the handler owns and closes it
            self.baseFilename, self.mode, encoding=self.encoding, errors=self.errors, opener=private
        )
        assert isinstance(handle, io.TextIOWrapper)  # noqa: S101 - text mode always gives one
        return handle


def configure(*, file: Path | None = None, stream: TextIO | None = sys.stderr) -> None:
    """Send Enzo's and the HTTP server's logs to ``stream`` and, if given, a rotated ``file``.

    Safe to call again: handlers from an earlier call are replaced, never doubled.
    """
    handlers: list[logging.Handler] = []
    if stream is not None:
        handlers.append(logging.StreamHandler(stream))
    if file is not None:
        private_dir(file.parent)
        handlers.append(
            _PrivateFile(file, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8")
        )
    formatter = JsonLines()
    for handler in handlers:
        handler.setFormatter(formatter)
        handler._enzo = True  # type: ignore[attr-defined]  # ours, replaced on the next call
    for name in LOGGERS:
        logger = logging.getLogger(name)
        for old in [h for h in logger.handlers if getattr(h, "_enzo", False)]:
            logger.removeHandler(old)
            old.close()
        for handler in handlers:
            logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, QuietHealth) for f in access.filters):
        access.addFilter(QuietHealth())


# Reading it back (`enzo logs`).

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_SHOWN_FIRST = ("ts", "level", "event", "error")
_MARK = {"warning": "!", "error": "✗", "critical": "✗"}


def readable(raw: str) -> str:
    """One log line for people: local time, a mark for problems, the event, then its fields.

    Escape codes are dropped: lines carry error text from outside (a provider's reply), and it
    must never drive the terminal that shows it.
    """
    try:
        line = json.loads(raw)
    except ValueError:
        line = None
    if not isinstance(line, dict):
        return _CONTROL.sub("", raw.rstrip("\n"))  # not one of ours: as it is
    try:
        when = datetime.fromisoformat(str(line.get("ts"))).astimezone().strftime("%m-%d %H:%M:%S")
    except ValueError:
        when = "?"
    mark = _MARK.get(str(line.get("level")), " ")
    fields = [f"{k}={_value(k, v)}" for k, v in line.items() if k not in _SHOWN_FIRST]
    out = " ".join([when, mark, str(line.get("event", "")), *fields])
    error = line.get("error")
    if error:
        out += "\n" + "\n".join(f"    {row}" for row in str(error).splitlines())
    return _CONTROL.sub("", out)


def _value(key: str, value: object) -> str:
    if key == "conversation" and isinstance(value, str):
        return value[:8]  # enough to tell chats apart; `--json` has the whole id
    if isinstance(value, str) and value and " " not in value:
        return value
    return json.dumps(value, ensure_ascii=False)


def last_lines(path: Path, count: int) -> list[str]:
    with path.open(encoding="utf-8", errors="replace") as handle:
        return list(deque(handle, maxlen=count))


def follow(
    path: Path, *, lines: int = 0, poll_s: float = 0.5, stop: Callable[[], bool] = lambda: False
) -> Iterator[str]:
    """The last ``lines`` lines, then each line as it is written, across rotations."""
    handle: TextIO | None = None
    pending = ""
    first = True
    try:
        while not stop():
            if handle is None:
                try:
                    handle = path.open(encoding="utf-8", errors="replace")
                except FileNotFoundError:
                    time.sleep(poll_s)
                    continue
                if first:
                    first = False
                    tail = list(deque(handle, maxlen=lines)) if lines else []
                    if not lines:
                        handle.seek(0, os.SEEK_END)
                    if tail and not tail[-1].endswith("\n"):
                        pending = tail.pop()  # still being written
                    yield from tail
            chunk = handle.readline()
            if chunk:
                pending += chunk
                if pending.endswith("\n"):
                    yield pending
                    pending = ""
                continue
            if _rotated(path, handle):
                handle.close()
                handle = None  # the next file starts from its first line
                continue
            time.sleep(poll_s)
    finally:
        if handle is not None:
            handle.close()


def _rotated(path: Path, handle: TextIO) -> bool:
    try:
        return path.stat().st_ino != os.fstat(handle.fileno()).st_ino
    except FileNotFoundError:
        return False  # between the rename and the new file: keep reading the old one
