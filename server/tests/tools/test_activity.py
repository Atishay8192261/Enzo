"""Each tool step reads as plain words in the chat (#40), never as a tool name (§10.3)."""

import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path

import httpx2
import pytest
from pydantic import BaseModel, ConfigDict

from enzo.brain import ToolCall
from enzo.tools import ToolRegistry, tool
from enzo.tools.files import file_tools
from enzo.tools.registry import GENERIC_ACTIVITY, MAX_ACTIVITY, plain_words
from enzo.tools.web import web_tools


def call(name: str, **args: object) -> ToolCall:
    return ToolCall(id="c1", name=name, arguments_json=json.dumps(args))


@pytest.fixture
async def core(tmp_path: Path) -> AsyncIterator[ToolRegistry]:
    async with httpx2.AsyncClient() as http:
        yield ToolRegistry([*file_tools(tmp_path), *web_tools(tavily_key=lambda: None, http=http)])


@pytest.mark.parametrize(
    ("name", "args", "words"),
    [
        (
            "web_search",
            {"query": "top headlines today"},
            "Searching the web for “top headlines today”",
        ),
        ("read_page", {"url": "https://www.bbc.com/news/?at=1#top"}, "Reading bbc.com/news"),
        ("read_page", {"url": "http://example.org"}, "Reading example.org"),
        ("list_files", {}, "Looking at your files"),
        ("list_files", {"path": "notes"}, "Looking in notes"),
        ("read_file", {"path": "plan.md"}, "Reading plan.md"),
        ("create_file", {"path": "notes/today.md", "content": "x"}, "Creating notes/today.md"),
        ("edit_file", {"path": "plan.md", "old_text": "a", "new_text": "b"}, "Editing plan.md"),
        ("delete_file", {"path": "plan.md"}, "Deleting plan.md"),
    ],
)
async def test_core_tools_say_what_they_do(
    core: ToolRegistry, name: str, args: dict[str, object], words: str
) -> None:
    assert core.activity(call(name, **args)) == words


async def test_a_call_that_cannot_be_described_still_gets_plain_words(core: ToolRegistry) -> None:
    assert core.activity(call("no_such_tool")) == GENERIC_ACTIVITY
    assert core.activity(call("read_file")) == GENERIC_ACTIVITY  # missing path
    bad = ToolCall(id="c1", name="read_file", arguments_json="{not json")
    assert core.activity(bad) == GENERIC_ACTIVITY


async def test_brain_written_words_cannot_draw_on_the_terminal(core: ToolRegistry) -> None:
    """Arguments come from the brain (and so from web pages it read): escape codes and line
    breaks would let them repaint the screen or fake another line."""
    words = core.activity(call("read_file", path="a\x1b[2J\x1b]8;;evil\x07b\nc\td"))
    assert words.isprintable()
    assert "\x1b" not in words
    assert "\n" not in words
    assert words.startswith("Reading a")


def test_long_words_are_cut_with_an_ellipsis() -> None:
    assert plain_words("x" * 200) == "x" * (MAX_ACTIVITY - 1) + "…"
    assert plain_words("short") == "short"
    assert plain_words("  two   spaces\n\nand lines ") == "two spaces and lines"
    query = plain_words("q" * 61, 60)
    assert len(query) == 60
    assert query.endswith("…")


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str


class Out(BaseModel):
    value: str


async def test_falls_back_to_the_approval_summary_then_to_generic_words() -> None:
    def summary(args: Args) -> str:
        return f"Send {args.value}"

    def broken(_args: Args) -> str:
        msg = "describer bug"
        raise RuntimeError(msg)

    @tool(name="gated", description="d", requires_approval=True, summary=summary)
    async def gated(args: Args) -> Out:
        return Out(value=args.value)

    @tool(name="plain", description="d")
    async def plain(args: Args) -> Out:
        return Out(value=args.value)

    @tool(name="buggy", description="d", activity=broken)
    async def buggy(args: Args) -> Out:
        return Out(value=args.value)

    registry = ToolRegistry([gated, plain, buggy])
    assert registry.activity(call("gated", value="the report")) == "Send the report"
    assert registry.activity(call("plain", value="x")) == GENERIC_ACTIVITY
    assert registry.activity(call("buggy", value="x")) == GENERIC_ACTIVITY


async def test_a_describer_bug_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    def broken(_args: Args) -> str:
        msg = "describer bug"
        raise RuntimeError(msg)

    @tool(name="buggy", description="d", activity=broken)
    async def buggy(args: Args) -> Out:
        return Out(value=args.value)

    with caplog.at_level(logging.ERROR):
        ToolRegistry([buggy]).activity(call("buggy", value="x"))
    assert "tool activity failed" in caplog.text
