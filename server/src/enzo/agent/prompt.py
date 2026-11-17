"""Compose the layered prompt in the order spec §3 step 3 fixes."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from enzo.brain.models import Message, TextPart


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
    """(1) system, (2) developer, (4) history, (6) the user's message carrying (3) memory.

    Retrieved memory rides inside the new user message, after the history (ADR 0007, a deliberate
    change from spec §3's listed order): the conversation stays append-only, so provider prompt
    caches keep covering the whole history and reasoning replay never sees an edited prefix.
    (5) tool definitions travel as API parameters and (7) this turn's tool results are appended by
    the loop. With no user message, the prompt resumes a paused turn exactly where it stopped.
    """
    if any(m.role == "system" for m in history):
        msg = "history must not contain system messages; pass instructions as system/developer"
        raise ValueError(msg)
    prompt = [Message.system(system)]
    if developer:
        prompt.append(Message.system(developer))
    prompt.extend(history)
    if user_message is None:
        return prompt
    user = Message.user(user_message) if isinstance(user_message, str) else user_message
    if user.role != "user":
        msg = "the new message must come from the user"
        raise ValueError(msg)
    if memory:
        user = Message(role="user", parts=(*user.parts, TextPart(text=render_memory(memory))))
    prompt.append(user)
    return prompt


def render_memory(memory: Sequence[MemorySnippet]) -> str:
    lines = [
        f"- {m.text} (source: {m.source}, recorded {m.recorded_on.isoformat()})" for m in memory
    ]
    return (
        "Relevant memory from Enzo's records. Similarity is not truth: verify against the source "
        "before acting on it.\n" + "\n".join(lines)
    )
