"""What the current code works for: the chat and the run (one send or resume) of this task.

Set once per run by the conversation service. Log lines carry them (#48), and tools that record
where something came from read them: a memory notes the chat and run it was learned in (§6.5).
"""

import uuid
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass

conversation_var: ContextVar[str | None] = ContextVar("enzo_conversation", default=None)
turn_var: ContextVar[str | None] = ContextVar("enzo_turn", default=None)
#: The user's time zone, as their app reported it with the message (IANA, e.g. Europe/Paris).
timezone_var: ContextVar[str | None] = ContextVar("enzo_timezone", default=None)
#: The scheduled task this run is for, when a schedule started it (its tools need to know).
job_var: ContextVar[int | None] = ContextVar("enzo_job", default=None)
#: The words of the message this run answers, when the user (or their schedule) wrote it: a
#: helper's brief carries them, so the user's own limits reach it unchanged (ADR 0028).
request_var: ContextVar[str | None] = ContextVar("enzo_request", default=None)


def new_turn_id() -> str:
    return f"t_{uuid.uuid4().hex[:12]}"


def current_conversation() -> uuid.UUID | None:
    value = conversation_var.get()
    return uuid.UUID(value) if value else None


@dataclass(frozen=True, slots=True)
class BrowserInUse:
    """Enzo is using the browser for this chat, on ``site``; None: it closed (ADR 0027)."""

    site: str | None


#: Sends a live notice to the chat whose run this is, beside the turn's own events (the "Enzo is
#: using your browser" sign). Set per run by the conversation service; None outside a run.
notice_var: ContextVar[Callable[[BrowserInUse], None] | None] = ContextVar(
    "enzo_notice", default=None
)


def notify(notice: BrowserInUse) -> None:
    """Tell the chat whose run this is, if any (a test or a background caller has none)."""
    send = notice_var.get()
    if send is not None:
        send(notice)
