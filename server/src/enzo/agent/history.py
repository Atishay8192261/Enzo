"""Keep a conversation's history valid for every provider (#39).

Providers reject a history where a tool call is not followed by its result ("No tool output found
for function call …"). That happened when the user sent a new message while an approval was still
open, and it broke the chat for good.

- ``close_open_calls``: when a new message arrives while calls at the end are still unanswered,
  those calls get a "not run" result first, so the history stays valid and nothing runs unasked.
- ``repair``: chats broken before this fix get the missing results when the prompt is built, so they
  work again. The stored transcript is not rewritten.
"""

from collections.abc import Sequence

from enzo.brain.models import Message, ToolCall, ToolResult

MOVED_ON = (
    "Not run: the user sent a new message instead of answering the approval. "
    "Ask again if it is still needed."
)
NEVER_ANSWERED = "Not run: this action was never answered, so it did not happen."


def open_calls(messages: Sequence[Message]) -> tuple[ToolCall, ...]:
    """Tool calls in the last message that have no results yet (a paused turn)."""
    if not messages or messages[-1].role != "assistant":
        return ()
    return messages[-1].tool_calls


def close_open_calls(messages: Sequence[Message]) -> Message | None:
    """The results that close the calls left open at the end, or None when there are none."""
    calls = open_calls(messages)
    if not calls:
        return None
    return Message.tool(*(_not_run(c, MOVED_ON) for c in calls))


def repair(messages: Sequence[Message]) -> list[Message]:
    """Give every tool call that is not the last message a result. Calls at the very end stay
    open: a paused turn resumes from them."""
    out: list[Message] = []
    i = 0
    while i < len(messages):
        message = messages[i]
        out.append(message)
        i += 1
        calls = message.tool_calls if message.role == "assistant" else ()
        if not calls or i == len(messages):
            continue
        following = messages[i] if messages[i].role == "tool" else None
        answered = (
            {p.call_id for p in following.parts if isinstance(p, ToolResult)}
            if following
            else set()
        )
        missing = [_not_run(c, NEVER_ANSWERED) for c in calls if c.id not in answered]
        if not missing:
            continue
        if following is not None:  # all of a reply's results belong in one message
            out.append(
                Message.tool(*(p for p in following.parts if isinstance(p, ToolResult)), *missing)
            )
            i += 1
        else:
            out.append(Message.tool(*missing))
    return out


def _not_run(call: ToolCall, why: str) -> ToolResult:
    return ToolResult(call_id=call.id, name=call.name, content=why, is_error=True)
