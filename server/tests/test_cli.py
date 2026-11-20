"""`enzo` opens a new chat; `enzo -c` / `enzo --continue` the last one (#57)."""

import contextlib
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from enzo import __version__, cli


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


@pytest.fixture
def uv_tool(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Enzo installed as a uv tool, with uv itself faked."""
    runs: list[list[str]] = []
    monkeypatch.setattr("enzo.cli.shutil.which", lambda _name: "/home/u/.local/bin/uv")
    monkeypatch.setattr("enzo.cli.sys.prefix", "/home/u/.local/share/uv/tools/enzo-agent")

    def call(argv: list[str]) -> int:
        runs.append(argv)
        return 0

    monkeypatch.setattr("enzo.cli.subprocess.call", call)
    return runs


def test_update_when_already_the_latest_says_so(
    uv_tool: list[list[str]], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#56: uv said "Nothing to upgrade", then Enzo said "Updated."."""
    monkeypatch.setattr("enzo.cli.installed_version", lambda: __version__)
    assert cli.main(["update"]) == 0
    assert uv_tool == [["/home/u/.local/bin/uv", "tool", "upgrade", "enzo-agent"]]
    assert capsys.readouterr().out == f"Enzo {__version__} is already the latest.\n"


def test_update_names_the_new_version(
    uv_tool: list[list[str]], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("enzo.cli.installed_version", lambda: "9.9.9")
    assert cli.main(["update"]) == 0
    assert capsys.readouterr().out.startswith(f"Updated Enzo {__version__} → 9.9.9. ")


def test_a_failed_update_claims_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("enzo.cli.shutil.which", lambda _name: "/home/u/.local/bin/uv")
    monkeypatch.setattr("enzo.cli.sys.prefix", "/home/u/.local/share/uv/tools/enzo-agent")
    monkeypatch.setattr("enzo.cli.subprocess.call", lambda _argv: 2)
    assert cli.main(["update"]) == 2
    assert capsys.readouterr().out == ""


def test_the_version_on_disk_is_read_fresh() -> None:
    assert cli.installed_version() == __version__  # this checkout, installed editable
