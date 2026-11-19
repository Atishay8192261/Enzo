"""The installed product (#23): the terminal app download, the gateway serving the web app, and
migrations shipped inside the package."""

import asyncio
import hashlib
import json
from pathlib import Path

import httpx2
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from enzo import __version__, tui
from enzo.config import Settings
from enzo.gateway.app import create_app
from enzo.paths import data_dir, state_dir
from enzo.store.migrate import upgrade
from tests.pg import create_database, drop_database

BINARY = b"\x7fELF pretend terminal app"
ASSET = tui.Asset(
    name="enzo-tui-linux-x64", sha256=hashlib.sha256(BINARY).hexdigest(), size=len(BINARY)
)


@pytest.fixture
def released(monkeypatch: pytest.MonkeyPatch) -> None:
    """This package as a release would ship it, on linux-x64."""
    monkeypatch.setattr(tui, "release_assets", lambda: {"linux-x64": ASSET})
    monkeypatch.setattr(tui, "current_platform", lambda: "linux-x64")
    monkeypatch.delenv("ENZO_TUI", raising=False)


def github(body: bytes, seen: list[str]) -> httpx2.Client:
    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(str(request.url))
        if "/releases/download/" in str(request.url):  # GitHub redirects to its file host
            return httpx2.Response(302, headers={"location": "https://objects.example/enzo-tui"})
        return httpx2.Response(200, content=body)

    return httpx2.Client(transport=httpx2.MockTransport(handler))


@pytest.mark.usefixtures("released")
def test_the_terminal_app_is_downloaded_once_and_verified(tmp_path: Path) -> None:
    seen: list[str] = []
    with github(BINARY, seen) as http:
        binary = tui.ensure(tmp_path, http)
        assert binary.read_bytes() == BINARY
        assert binary.stat().st_mode & 0o777 == 0o700
        assert tmp_path.stat().st_mode & 0o777 == 0o700  # the data folder is private too
        assert seen[0] == (
            f"https://github.com/Atishay8192261/Enzo/releases/download/v{__version__}/enzo-tui-linux-x64"
        )
        tui.ensure(tmp_path, http)
    assert len(seen) == 2  # one download (with its redirect), then the verified copy is reused


@pytest.mark.usefixtures("released")
def test_a_download_that_does_not_match_the_release_is_refused(tmp_path: Path) -> None:
    with (
        github(b"something else entirely", []) as http,
        pytest.raises(tui.TuiError, match="does not match this release"),
    ):
        tui.ensure(tmp_path, http)
    assert not list((tmp_path / "bin").iterdir())  # nothing half-written is left to run


@pytest.mark.usefixtures("released")
def test_a_tampered_copy_on_disk_is_replaced(tmp_path: Path) -> None:
    target = tui.path(tmp_path)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"changed after download")
    with github(BINARY, []) as http:
        assert tui.ensure(tmp_path, http).read_bytes() == BINARY


def test_a_copy_not_built_as_a_release_says_what_to_do(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ENZO_TUI", raising=False)
    monkeypatch.setattr(tui, "release_assets", lambda: None)
    with github(BINARY, []) as http, pytest.raises(tui.TuiError, match=r"scripts/dev\.sh"):
        tui.ensure(tmp_path, http)


def test_release_metadata_must_match_this_version(monkeypatch: pytest.MonkeyPatch) -> None:
    stale = json.dumps({"version": "0.0.1", "tui": {}})

    class Files:
        def __truediv__(self, _name: str) -> "Files":
            return self

        def read_text(self, encoding: str) -> str:
            return stale

    monkeypatch.setattr("enzo.tui.resources.files", lambda _pkg: Files())
    assert tui.release_assets() is None


def test_unsupported_platforms_are_named(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("enzo.tui.platform.system", lambda: "Windows")
    monkeypatch.setattr("enzo.tui.platform.machine", lambda: "AMD64")
    with pytest.raises(tui.TuiError, match="on Windows, use WSL"):
        tui.current_platform()


def test_folders_follow_xdg_and_enzo_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ENZO_DATA_DIR", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert data_dir() == tmp_path / "xdg" / "enzo"
    monkeypatch.setenv("ENZO_STATE_DIR", str(tmp_path / "s"))
    assert state_dir() == tmp_path / "s"


async def test_the_gateway_answers_under_api_and_serves_the_web_app(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    web = tmp_path / "enzo"
    (web / "webapp").mkdir(parents=True)
    (web / "webapp" / "index.html").write_text("<!doctype html><title>Enzo</title>")
    monkeypatch.setattr("enzo.gateway.app.resources.files", lambda _pkg: web)
    app = create_app(Settings(env="dev"))
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://127.0.0.1:8787") as c:
        assert (await c.get("/api/healthz")).json()["status"] == "ok"  # the web app's path
        assert (await c.get("/healthz")).json()["status"] == "ok"  # the terminal app's path
        page = await c.get("/")
        assert page.status_code == 200
        assert "<title>Enzo</title>" in page.text
    async with httpx2.AsyncClient(transport=transport, base_url="http://evil.example") as c:
        assert (await c.get("/healthz")).status_code == 400  # DNS rebinding is refused


def test_migrations_ship_inside_the_package() -> None:
    name, url = create_database()
    try:
        upgrade(url)  # no alembic.ini: the installed path, before any event loop runs

        async def head() -> object:
            engine = create_async_engine(url)
            async with engine.connect() as conn:
                found = (
                    await conn.execute(text("SELECT version_num FROM alembic_version"))
                ).scalar()
            await engine.dispose()
            return found

        assert asyncio.run(head()) == "0003"
    finally:
        drop_database(name)
