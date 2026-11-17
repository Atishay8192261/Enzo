"""Compose the layered prompt in the order spec §3 step 3 fixes."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from enzo.brain.models import Message


@dataclass(frozen=True, slots=True)
class MemorySnippet:
    """A retrieved fact with its provenance (spec §6.5: keep a source reference, date events)."""

    text: str
    source: str
    recorded_on: date


def compose_prompt(
    *,
    system: str,
    developer: str | None,
    memory: Sequence[MemorySnippet],
    history: Sequence[Message],
    user_message: str | Message | None,
) -> list[Message]:
    """(1) system, (2) developer, (3) retrieved memory, (4) history, (6) the user's message.

    (5) tool definitions travel as API parameters and (7) this turn's tool results are appended
    by the loop, so both keep their place in the order without being text here. With no user
    message, the prompt resumes a paused turn whose history already ends where it stopped.
    """
    if any(m.role == "system" for m in history):
        msg = "history must not contain system messages; pass instructions as system/developer"
        raise ValueError(msg)
    prompt = [Message.system(system)]
    if developer:
        prompt.append(Message.system(developer))
    if memory:
        prompt.append(Message.system(render_memory(memory)))
    prompt.extend(history)
    if isinstance(user_message, str):
        prompt.append(Message.user(user_message))
    elif user_message is not None:
        if user_message.role != "user":
            msg = "the new message must come from the user"
            raise ValueError(msg)
        prompt.append(user_message)
    return prompt


def render_memory(memory: Sequence[MemorySnippet]) -> str:
    lines = [
        f"- {m.text} (source: {m.source}, recorded {m.recorded_on.isoformat()})" for m in memory
    ]
    return (
        "Relevant memory from Enzo's records. Similarity is not truth: verify against the source "
        "before acting on it.\n" + "\n".join(lines)
    )
