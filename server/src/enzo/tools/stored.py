"""Long texts kept as files, out of the context (spec §5.2 selective retention, §5.3; ADR 0024).

A tool result longer than ``tool_inline_chars`` (a full page, a big file, a diff) is saved whole in
Enzo's private data folder, one folder per chat, and the context gets its start plus a pointer. A
pasted message longer than ``paste_inline_chars`` is treated the same way, and so is every long
part of the older turns a summary replaces. The brain reads on with ``read_stored_output`` (by
offset, or by searching), so Enzo answers from the stored text, not from a truncated prompt.

- Ids are the text's own hash, so storing is idempotent and a shortened message is byte-identical
  on every later turn (the prompt stays append-only: caches and replayed reasoning stay valid).
- Files are the user's alone (0700 folders, 0600 files). Their content is outside data, so the
  tool returns it fenced as untrusted (§12.4).
"""

import contextlib
import dataclasses
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from enzo.context import conversation_var
from enzo.paths import private_dir
from enzo.tools.fence import fence
from enzo.tools.registry import Tool, plain_words, tool

ID_PATTERN = r"^s_[0-9a-f]{16}$"
IDS = re.compile(r"\bs_[0-9a-f]{16}\b")
TOOL_NAME = "read_stored_output"
#: Files for work done outside any chat (tests, scripts) go here.
UNSCOPED = "unscoped"


class StoredTextError(Exception):
    """A stored text the brain asked for is not there (in this chat)."""


@dataclass(frozen=True, slots=True)
class Stored:
    id: str
    chars: int
    #: Where it came from, in plain words: "read_page", "the user's message".
    source: str
    #: Outside content (a page, a file, a tool result): returned fenced as data.
    untrusted: bool


def stored_id(text: str) -> str:
    return "s_" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class OutputStore:
    """Keeps long texts as files under ``root``/<chat id>/ and hands out pointers to them."""

    def __init__(
        self, root: Path, *, tool_inline_chars: int, paste_inline_chars: int, excerpt_chars: int
    ) -> None:
        self.root = root
        self.tool_inline_chars = tool_inline_chars
        self.paste_inline_chars = paste_inline_chars
        self.excerpt_chars = excerpt_chars

    def keep(self, text: str, *, source: str, untrusted: bool) -> Stored:
        """Store ``text`` (once: the same text keeps the same id) in the current chat's folder."""
        stored = Stored(stored_id(text), len(text), source, untrusted)
        folder = self._folder()
        body = folder / f"{stored.id}.txt"
        if not body.exists():
            meta = {"source": source, "chars": stored.chars, "untrusted": untrusted}
            _write(folder / f"{stored.id}.json", json.dumps(meta))
            _write(body, text)
        return stored

    def find(self, stored: str) -> Stored | None:
        try:
            meta = json.loads((self._folder() / f"{stored}.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return None
        return Stored(stored, int(meta["chars"]), str(meta["source"]), bool(meta["untrusted"]))

    def text(self, stored: str) -> str:
        if not re.fullmatch(ID_PATTERN, stored):
            msg = f"{stored!r} is not a stored-text id"
            raise StoredTextError(msg)
        try:
            return (self._folder() / f"{stored}.txt").read_text(encoding="utf-8")
        except FileNotFoundError:
            msg = f"there is no stored text {stored} in this chat"
            raise StoredTextError(msg) from None

    def shorten(self, text: str, *, source: str, untrusted: bool, shown: int | None = None) -> str:
        """The whole text stored; what stays in context: its start (fenced if outside content)
        and a pointer the brain can follow."""
        stored = self.keep(text, source=source, untrusted=untrusted)
        start = text[: shown or self.excerpt_chars]
        body = fence(start, source=source) if untrusted else start
        return f"{body}\n{pointer(stored, len(start))}"

    def shorten_result(self, content: str, *, source: str, untrusted: bool) -> str | None:
        """For the tool registry: a result too long for the context, shortened; else None."""
        if len(content) <= self.tool_inline_chars:
            return None
        return self.shorten(content, source=source, untrusted=untrusted)

    def _folder(self) -> Path:
        chat = conversation_var.get() or UNSCOPED
        if not re.fullmatch(r"[0-9a-f-]{1,64}|" + UNSCOPED, chat):  # never a path from elsewhere
            msg = f"unexpected chat id {chat!r}"
            raise StoredTextError(msg)
        return private_dir(private_dir(self.root) / chat)


def pointer(stored: Stored, shown: int) -> str:
    """The note after a shortened text: what was kept, and how to read the rest."""
    return (
        f"[Enzo kept this whole ({stored.chars:,} characters, from {stored.source}) as stored "
        f"text {stored.id}; only the first {shown:,} characters are above. To read on, call "
        f'{TOOL_NAME} with id "{stored.id}" and offset {shown}; to look for something in it, '
        "pass find.]"
    )


def _write(path: Path, text: str) -> None:
    """Written fully or not at all; mkstemp files are readable by the user only."""
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".stored-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp)
        raise


class ReadStoredParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(
        pattern=ID_PATTERN, description="The stored text's id, e.g. s_3fa2c1d4e5f60718."
    )
    offset: int = Field(default=0, ge=0, description="Where to start reading, in characters.")
    max_chars: int = Field(default=12_000, ge=500, le=40_000, description="How much to return.")
    find: str | None = Field(
        default=None,
        min_length=2,
        max_length=200,
        description=(
            "Words to look for (any case), from offset on. The text returned starts a little "
            "before the first place they appear."
        ),
    )


class StoredSlice(BaseModel):
    id: str
    source: str
    total_chars: int
    offset: int
    text: str
    #: Where the rest starts; null at the end.
    next_offset: int | None
    #: With find: whether the words were found (when not, text is empty).
    found: bool | None = None


#: With find, the text returned starts this far before the match, for context.
LEAD_IN = 300


def read_slice(store: OutputStore, params: ReadStoredParams) -> StoredSlice:
    stored = store.find(params.id)
    text = store.text(params.id)
    source = stored.source if stored else "a stored text"
    start = min(params.offset, len(text))
    found: bool | None = None
    if params.find is not None:
        at = text.casefold().find(params.find.casefold(), start)
        found = at >= 0
        if not found:
            return StoredSlice(
                id=params.id,
                source=source,
                total_chars=len(text),
                offset=start,
                text="",
                next_offset=None,
                found=False,
            )
        start = max(start, at - LEAD_IN)
    end = min(len(text), start + params.max_chars)
    return StoredSlice(
        id=params.id,
        source=source,
        total_chars=len(text),
        offset=start,
        text=text[start:end],
        next_offset=end if end < len(text) else None,
        found=found,
    )


def stored_tools(store: OutputStore) -> list[Tool[Any, Any]]:
    """The tool that reads stored texts back, a part at a time."""

    @tool(
        name=TOOL_NAME,
        description=(
            "Read a long text Enzo kept whole outside the conversation (a page, a file, a tool "
            "result, a pasted document, or one an earlier summary points to), by its id. Read on "
            "from an offset, or pass find to jump to where some words appear. Answer from what "
            "you read here, never from the shortened start alone."
        ),
        untrusted_output=True,
        activity=_activity,
    )
    async def read_stored_output(params: ReadStoredParams) -> StoredSlice:
        return read_slice(store, params)

    # Its results are already slices of stored text: never stored again.
    return [dataclasses.replace(read_stored_output, keep_whole=True)]


def _activity(params: ReadStoredParams) -> str:
    if params.find:
        return f"Looking for “{plain_words(params.find, 50)}” in a saved document"
    return "Reading a saved document"
