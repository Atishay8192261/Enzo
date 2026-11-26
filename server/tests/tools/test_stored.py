"""Long tool results are stored whole; the context keeps the start and a pointer (spec §5.2)."""

import json
import stat
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict

from enzo.brain.models import ToolCall
from enzo.context import conversation_var
from enzo.tools import ToolRegistry, tool
from enzo.tools.stored import (
    OutputStore,
    ReadStoredParams,
    StoredTextError,
    read_slice,
    stored_id,
    stored_tools,
)


def make_store(root: Path) -> OutputStore:
    return OutputStore(root, tool_inline_chars=1_000, paste_inline_chars=2_000, excerpt_chars=200)


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")
    size: int


class Page(BaseModel):
    text: str


@tool(name="fetch", description="Fetch a page.", untrusted_output=True)
async def fetch(args: Args) -> Page:
    return Page(text="".join(f"line {i}. " for i in range(args.size)))


@tool(name="count", description="Count.")
async def count(args: Args) -> Page:
    return Page(text="n" * args.size)


def call(name: str, size: int, cid: str = "c1") -> ToolCall:
    return ToolCall(id=cid, name=name, arguments_json=json.dumps({"size": size}))


@pytest.fixture
def chat() -> Iterator[str]:
    chat_id = str(uuid.uuid4())
    token = conversation_var.set(chat_id)
    yield chat_id
    conversation_var.reset(token)


async def test_a_long_result_is_stored_and_shortened_with_a_pointer(
    tmp_path: Path, chat: str
) -> None:
    keep = make_store(tmp_path)
    registry = ToolRegistry([fetch, *stored_tools(keep)], retain=keep)
    outcome = await registry.execute(call("fetch", 2_000))
    content = outcome.result.content
    whole = Page(text="".join(f"line {i}. " for i in range(2_000))).model_dump_json()
    assert len(content) < 700
    assert content.startswith('<untrusted-data source="fetch"')  # still fenced as outside data
    assert whole[:200] in content
    sid = stored_id(whole)
    assert f'id "{sid}" and offset 200' in content
    assert keep.text(sid) == whole
    # Private: the folder is the user's alone, the files too.
    folder = tmp_path / chat
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert stat.S_IMODE((folder / f"{sid}.txt").stat().st_mode) == 0o600


async def test_short_results_and_slices_of_stored_text_stay_whole(
    tmp_path: Path, chat: str
) -> None:
    keep = make_store(tmp_path)
    registry = ToolRegistry([fetch, count, *stored_tools(keep)], retain=keep)
    short = await registry.execute(call("count", 100))
    assert short.result.content == Page(text="n" * 100).model_dump_json()
    big = await registry.execute(call("fetch", 2_000))
    sid = next(w for w in big.result.content.split('"') if w.startswith("s_"))
    read = ToolCall(id="r1", name="read_stored_output", arguments_json=json.dumps({"id": sid}))
    sliced = await registry.execute(read)
    assert sliced.status == "ok"
    assert "[Enzo kept this whole" not in sliced.result.content  # never shortened again
    assert len(sliced.result.content) > 10_000  # all 12,000 characters asked for


async def test_reading_on_by_offset_and_by_search(tmp_path: Path, chat: str) -> None:
    keep = make_store(tmp_path)
    text = "".join(f"CLAUSE-{i}: rule number {i}. " for i in range(1_000))
    sid = keep.keep(text, source="a pasted contract", untrusted=False).id
    first = read_slice(keep, ReadStoredParams(id=sid, max_chars=500))
    assert (first.offset, first.next_offset, first.text) == (0, 500, text[:500])
    assert first.source == "a pasted contract"
    nxt = read_slice(keep, ReadStoredParams(id=sid, offset=500, max_chars=500))
    assert nxt.text == text[500:1000]
    found = read_slice(keep, ReadStoredParams(id=sid, find="clause-777:", max_chars=500))
    assert found.found
    assert "CLAUSE-777: rule number 777." in found.text
    missing = read_slice(keep, ReadStoredParams(id=sid, find="CLAUSE-5000"))
    assert (missing.found, missing.text, missing.next_offset) == (False, "", None)
    end = read_slice(keep, ReadStoredParams(id=sid, offset=len(text) - 10))
    assert end.next_offset is None


async def test_stored_text_is_per_chat_and_ids_are_checked(tmp_path: Path) -> None:
    keep = make_store(tmp_path)
    token = conversation_var.set(str(uuid.uuid4()))
    sid = keep.keep("secret plan " * 300, source="x", untrusted=False).id
    conversation_var.reset(token)
    token = conversation_var.set(str(uuid.uuid4()))
    try:
        with pytest.raises(StoredTextError, match="no stored text"):
            keep.text(sid)  # another chat cannot read it
        with pytest.raises(StoredTextError, match="not a stored-text id"):
            keep.text("../../etc/passwd")
    finally:
        conversation_var.reset(token)


async def test_storing_the_same_text_twice_gives_the_same_pointer(
    tmp_path: Path, chat: str
) -> None:
    keep = make_store(tmp_path)
    text = "same words " * 500
    assert keep.shorten(text, source="a", untrusted=False) == keep.shorten(
        text, source="a", untrusted=False
    )
    assert len(list((tmp_path / chat).glob("*.txt"))) == 1
