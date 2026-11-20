"""What the current code works for: the chat and the run (one send or resume) of this task.

Set once per run by the conversation service. Log lines carry them (#48), and tools that record
where something came from read them: a memory notes the chat and run it was learned in (§6.5).
"""

import uuid
from contextvars import ContextVar

conversation_var: ContextVar[str | None] = ContextVar("enzo_conversation", default=None)
turn_var: ContextVar[str | None] = ContextVar("enzo_turn", default=None)


def new_turn_id() -> str:
    return f"t_{uuid.uuid4().hex[:12]}"


def current_conversation() -> uuid.UUID | None:
    value = conversation_var.get()
    return uuid.UUID(value) if value else None
