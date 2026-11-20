"""`enzo` opens a new chat; `enzo -c` / `enzo --continue` the last one (#57)."""

import contextlib
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from enzo import cli


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[list[str]]:
    """What `enzo` would run, with the download, the gateway and the app faked."""
    runs: list[list[str]] = []
    app = tmp_path / "enzo-tui"

    @contextlib.contextmanager
    def gateway() -> Iterator[SimpleNamespace]:
        yield SimpleNamespace(url="http://127.0.0.1:8787")

    def call(argv: list[str], **_kwargs: object) -> int:
        runs.append(argv)
        return 0

    monkeypatch.setattr("enzo.cli.tui.ensure", lambda *_a, **_k: app)
    monkeypatch.setattr("enzo.cli._gateway", gateway)
    monkeypatch.setattr("enzo.cli.subprocess.call", call)
    return runs


@pytest.mark.parametrize(
    ("argv", "flags"),
    [
        ([], []),
        (["chat"], []),
        (["-c"], ["--continue"]),
        (["--continue"], ["--continue"]),
        (["chat", "--continue"], ["--continue"]),
        (["chat", "-c"], ["--continue"]),
    ],
)
def test_a_new_chat_unless_asked_to_continue(
    started: list[list[str]], argv: list[str], flags: list[str]
) -> None:
    assert cli.main(argv) == 0
    (run,) = started
    assert run[1:] == flags
    assert run[0].endswith("enzo-tui")
