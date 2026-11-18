"""Files workspace: read, create, edit and delete files in one folder (spec §7.2.2 Core, §11.2).

The workspace (default ``~/Enzo``) is the user-visible folder for documents Enzo makes. Every path
is resolved and must stay inside it: ``..``, absolute paths and symlinks that lead out are refused.
Internal bookkeeping lives in the hidden ``.enzo`` folder inside it, which tools cannot touch:

- ``edit_file`` keeps the previous version in ``.enzo/history`` first, so an edit can be undone;
- ``delete_file`` needs the user's approval (§12.3) and moves the file to ``.enzo/trash``.

Text files only. File contents are outside data, fenced as untrusted (§12.4): a file that says
"ignore your instructions" is just text.
"""

import contextlib
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from enzo.tools.registry import Tool, tool

HIDDEN = ".enzo"
MAX_READ_BYTES = 5_000_000
MAX_WRITE_CHARS = 1_000_000


class FileToolError(Exception):
    """A plain-language failure the brain can relay."""


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


_PATH = Field(
    min_length=1,
    max_length=500,
    description="A path inside the workspace, like notes/today.md (no absolute paths).",
)


class ListParams(_Params):
    path: str = Field(default=".", max_length=500, description="Folder to list; . is the top.")


class Entry(BaseModel):
    name: str
    kind: Literal["file", "folder"]
    size: int | None
    modified: str


class Listing(BaseModel):
    path: str
    entries: list[Entry]


class ReadFileParams(_Params):
    path: str = _PATH
    max_chars: int = Field(default=20_000, ge=100, le=100_000, description="Longest text returned.")


class FileText(BaseModel):
    path: str
    text: str
    truncated: bool


class CreateParams(_Params):
    path: str = _PATH
    content: str = Field(max_length=MAX_WRITE_CHARS, description="The whole text of the new file.")


class Written(BaseModel):
    path: str
    chars: int


class EditParams(_Params):
    path: str = _PATH
    old_text: str = Field(
        min_length=1, description="Text to replace; must appear exactly once in the file."
    )
    new_text: str = Field(max_length=MAX_WRITE_CHARS, description="The replacement text.")


class Edited(BaseModel):
    path: str
    previous_version: str  # inside the hidden folder, for undo


class DeleteParams(_Params):
    path: str = _PATH


class Deleted(BaseModel):
    path: str
    recoverable_from: str


def file_tools(root: Path) -> list[Tool[Any, Any]]:
    """The workspace tools, confined to ``root``."""
    workspace = Workspace(root)

    @tool(
        name="list_files",
        description="List the files and folders in the user's Enzo workspace folder.",
    )
    async def list_files(params: ListParams) -> Listing:
        return workspace.list(params.path)

    @tool(
        name="read_file",
        description="Read a text file from the workspace.",
        untrusted_output=True,
    )
    async def read_file(params: ReadFileParams) -> FileText:
        return workspace.read(params.path, params.max_chars)

    @tool(
        name="create_file",
        description="Create a new text file in the workspace. Fails if the file already exists.",
    )
    async def create_file(params: CreateParams) -> Written:
        return workspace.create(params.path, params.content)

    @tool(
        name="edit_file",
        description=(
            "Replace one exact piece of text in a workspace file. The previous version is kept, "
            "so the edit can be undone."
        ),
    )
    async def edit_file(params: EditParams) -> Edited:
        return workspace.edit(params.path, params.old_text, params.new_text)

    @tool(
        name="delete_file",
        description="Delete a file from the workspace (it stays recoverable). Needs approval.",
        requires_approval=True,
        summary=_delete_summary,
    )
    async def delete_file(params: DeleteParams) -> Deleted:
        return workspace.delete(params.path)

    return [list_files, read_file, create_file, edit_file, delete_file]


def _delete_summary(params: DeleteParams) -> str:
    return f"Delete {params.path} from your Enzo folder (it stays recoverable)"


class Workspace:
    def __init__(self, root: Path) -> None:
        self.root = root

    def resolve(self, relative: str, *, folder_ok: bool = False) -> Path:
        """The real path of ``relative``, refused unless it stays inside the workspace."""
        if Path(relative).is_absolute() or relative.startswith("~"):
            msg = f"{relative} is not inside the workspace; use a path like notes/file.md"
            raise FileToolError(msg)
        root = self._root()
        target = (root / relative).resolve()
        if target != root and not target.is_relative_to(root):
            msg = f"{relative} leads outside the workspace; Enzo only works inside it"
            raise FileToolError(msg)
        if target == root and not folder_ok:
            msg = "that is the workspace folder itself, not a file"
            raise FileToolError(msg)
        if target != root and target.relative_to(root).parts[0] == HIDDEN:
            msg = f"{relative} is Enzo's internal folder; it cannot be used directly"
            raise FileToolError(msg)
        return target

    def list(self, relative: str) -> Listing:
        folder = self.resolve(relative, folder_ok=True)
        if not folder.is_dir():
            msg = f"{relative} is not a folder in the workspace"
            raise FileToolError(msg)
        entries = [
            _entry(child)
            for child in sorted(folder.iterdir(), key=lambda p: p.name.lower())
            if not (folder == self._root() and child.name == HIDDEN)
        ]
        return Listing(path=self._show(folder), entries=entries)

    def read(self, relative: str, max_chars: int) -> FileText:
        path = self._existing_file(relative)
        if path.stat().st_size > MAX_READ_BYTES:
            msg = f"{relative} is larger than {MAX_READ_BYTES // 1_000_000} MB; it was not read"
            raise FileToolError(msg)
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            msg = f"{relative} is not a text file; only text files can be read"
            raise FileToolError(msg) from exc
        return FileText(
            path=self._show(path), text=text[:max_chars], truncated=len(text) > max_chars
        )

    def create(self, relative: str, content: str) -> Written:
        path = self.resolve(relative)
        if path.exists():
            msg = f"{relative} already exists; use edit_file to change it"
            raise FileToolError(msg)
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, content)
        return Written(path=self._show(path), chars=len(content))

    def edit(self, relative: str, old_text: str, new_text: str) -> Edited:
        path = self._existing_file(relative)
        text = path.read_text(encoding="utf-8")
        count = text.count(old_text)
        if count != 1:
            found = "does not appear" if count == 0 else f"appears {count} times"
            msg = f"the text to replace {found} in {relative}; it must appear exactly once"
            raise FileToolError(msg)
        backup = self._internal("history", relative, suffix=_stamp())
        backup.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(backup, text)
        _atomic_write(path, text.replace(old_text, new_text, 1))
        return Edited(path=self._show(path), previous_version=self._show(backup))

    def delete(self, relative: str) -> Deleted:
        path = self._existing_file(relative)
        target = self._internal("trash", relative, prefix=_stamp())
        target.parent.mkdir(parents=True, exist_ok=True)
        path.replace(target)
        return Deleted(path=relative, recoverable_from=self._show(target))

    def _existing_file(self, relative: str) -> Path:
        path = self.resolve(relative)
        if not path.is_file():
            msg = f"{relative} is not a file in the workspace"
            raise FileToolError(msg)
        return path

    def _root(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root.resolve()

    def _internal(self, area: str, relative: str, *, prefix: str = "", suffix: str = "") -> Path:
        name = self.resolve(relative).relative_to(self._root())
        base = self._root() / HIDDEN / area
        if prefix:
            return base / prefix / name
        return base / name.parent / f"{name.name}.{suffix}"

    def _show(self, path: Path) -> str:
        shown = path.relative_to(self._root()).as_posix()
        return shown or "."


def _entry(path: Path) -> Entry:
    info = path.stat()
    modified = datetime.fromtimestamp(info.st_mtime, UTC).isoformat(timespec="seconds")
    if path.is_dir():
        return Entry(name=path.name, kind="folder", size=None, modified=modified)
    return Entry(name=path.name, kind="file", size=info.st_size, modified=modified)


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")


def _atomic_write(path: Path, text: str) -> None:
    """Write fully or not at all: a crash never leaves half a file."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
