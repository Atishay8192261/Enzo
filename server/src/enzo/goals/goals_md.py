"""``Goals.md``: a read-only copy of the user's goals, in their Enzo folder (spec §6.4, §11.2).

Read-only on purpose (ADR 0023): every change of a goal needs its evidence, which the chat and
``/goals`` record and a hand edit cannot. So the file is written with no write permission (an
editor warns before saving over it) and ends with a check of its own words. If it was changed by
hand anyway, Enzo keeps that copy aside, writes the file again and tells the user: their edit is
never silently lost, and the file never becomes a second truth.
"""

import hashlib
import os
import re
import tempfile
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from enzo.goals.store import Evidence, Goal

GOALS_FILE = "Goals.md"
BASIS_WORDS: dict[str, str] = {
    "user_confirmed": "you confirmed",
    "verified": "Enzo checked",
    "timeline_ended": "its date passed",
}
_CHECK = re.compile(rb"\n<!-- Written by Enzo; read-only\. Check: ([0-9a-f]{16}) -->\n\Z")


def render(
    open_goals: Sequence["Goal"],
    closed: Sequence["Goal"],
    *,
    shown_folder: Callable[[str], str],
) -> str:
    lines = [
        "# Your goals",
        "",
        "Enzo writes this file from its records of your goals. It is read-only: to change a goal,",
        "tell Enzo (“I mailed the form”, “I've given up on that”), or see them with /goals in",
        "the terminal app.",
        "",
        "## Open",
    ]
    if not open_goals:
        lines += ["", "No open goals."]
    for g in open_goals:
        lines += ["", f"### {g.title} (#{g.id})", ""]
        about = [f"Due {g.due_on:%a %-d %b %Y}" if g.due_on else "No due date"]
        if g.path:
            about.append(f"in {shown_folder(g.path)}")
        about.append(f"since {g.created_at:%-d %b %Y}")
        lines.append(" · ".join(about))
        if g.steps:
            lines.append("")
        for s in g.steps:
            mark = "[x]" if s.done else "[ ]"
            proof = f" — {_proof(s.evidence)}" if s.evidence else ""
            lines.append(f"- {mark} {s.text}{proof}")
    for status, heading in (("done", "Done"), ("dropped", "Given up")):
        group = [g for g in closed if g.status == status]
        if not group:
            continue
        lines += ["", f"## {heading}"]
        for g in group:
            lines += ["", f"### {g.title} (#{g.id})", ""]
            closing = g.closing
            when = f"{g.closed_at:%-d %b %Y}" if g.closed_at else ""
            lines.append(f"{heading} {when}" + (f" — {_proof(closing)}" if closing else ""))
    return "\n".join(lines) + "\n"


def _proof(e: "Evidence") -> str:
    how = BASIS_WORDS.get(e.basis or "", "recorded")
    when = e.happened_on or e.recorded_at.date()
    where = f" ({e.source})" if e.source else (f" (in “{e.chat}”)" if e.chat else "")
    return f"{how}, {when:%-d %b %Y}: {e.evidence or ''}{where}"


def _digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()[:16]


def sealed(body: str) -> bytes:
    """The file's words, then a check of them: a hand edit no longer matches it."""
    data = body.encode("utf-8")
    return data + f"\n<!-- Written by Enzo; read-only. Check: {_digest(data)} -->\n".encode()


def untouched(data: bytes) -> bool:
    found = _CHECK.search(data)
    return found is not None and found.group(1).decode() == _digest(data[: found.start()])


def write_read_only(path: Path, body: str) -> tuple[int, int, int]:
    """Write next to its place with no write permission, then swap it in."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".goals-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(sealed(body))
        Path(temp).chmod(0o400)  # the user's only, and an editor asks before saving over it
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise
    found = path.stat()
    return (found.st_mtime_ns, found.st_size, found.st_ino)


def set_aside(path: Path) -> str:
    """Keep the user's edited copy beside it; its name, to tell them."""
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H%M%S")
    target = path.with_name(f"Goals (your edit {stamp}).md")
    path.replace(target)
    target.chmod(0o600)
    return target.name
