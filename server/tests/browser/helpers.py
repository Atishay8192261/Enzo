"""Helpers for browser tests: a brain that reads the page it got back, and page lookups."""

import json
import re
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from enzo.brain import Message, ToolCall, ToolResult
from enzo.brain.fake import ScriptedBrain
from enzo.brain.models import BrainResponse, ToolChoice, ToolSpec

type Step = Message | Callable[[str], Message]

_FENCE = re.compile(r"^<untrusted-data [^>]*>\n(?P<body>.*)\n</untrusted-data id=\"\w+\">$", re.S)


class PageBrain(ScriptedBrain):
    """A scripted brain whose steps may look at the latest page, like a model choosing a ref."""

    def __init__(self, steps: Iterable[Step]) -> None:
        super().__init__([])
        self._steps: deque[Step] = deque(steps)

    async def chat(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolSpec] = (),
        tool_choice: ToolChoice = "auto",
        max_output_tokens: int,
    ) -> BrainResponse:
        if self._steps:
            step = self._steps.popleft()
            self._script.append(step(latest_page(messages)) if callable(step) else step)
        return await super().chat(
            messages, tools=tools, tool_choice=tool_choice, max_output_tokens=max_output_tokens
        )


def results(messages: Sequence[Message]) -> list[ToolResult]:
    return [p for m in messages if m.role == "tool" for p in m.parts if isinstance(p, ToolResult)]


def unfence(content: str) -> str:
    """The data inside an untrusted fence (asserting it is fenced)."""
    found = _FENCE.match(content)
    assert found, f"not fenced as untrusted: {content[:120]}"
    return found["body"]


def latest_view(messages: Sequence[Message]) -> dict[str, Any]:
    """The last page a browser step returned."""
    for result in reversed(results(messages)):
        if result.name.startswith("browser_") and not result.is_error:
            if result.name == "browser_close":
                continue
            view: dict[str, Any] = json.loads(unfence(result.content))
            return view
    return {}


def latest_page(messages: Sequence[Message]) -> str:
    return str(latest_view(messages).get("page", ""))


def ref(page: str, role: str, name: str) -> str:
    """The ref of the element with this role and accessible name on the page."""
    found = re.search(rf'- {role} "{re.escape(name)}"[^\n]*?\[ref=(\w+)\]', page)
    assert found, f"no {role} {name!r} on the page:\n{page}"
    return found[1]


def call(name: str, cid: str, **args: Any) -> ToolCall:
    return ToolCall(id=cid, name=name, arguments_json=json.dumps(args))


def calls(*items: ToolCall) -> Message:
    return Message.assistant(tool_calls=items)


def click(role: str, name: str, cid: str) -> Callable[[str], Message]:
    """A step that clicks the element named so on the page the brain just got."""
    return lambda page: calls(
        call("browser_click", cid, ref=ref(page, role, name), element=f"{name} {role}")
    )


def type_into(name: str, text: str, cid: str, *, submit: bool = False) -> Callable[[str], Message]:
    return lambda page: calls(
        call(
            "browser_type",
            cid,
            ref=ref(page, "textbox", name),
            element=name,
            text=text,
            submit=submit,
        )
    )
