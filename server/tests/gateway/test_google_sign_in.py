"""Sign in with Google through the gateway (ADR 0026): set up a client, the browser round trip, the
Gmail and Calendar tools appearing, a live check, revocation detected, sign-out. Real app and
Postgres; Google is a fake (tests/connectors/fake_google.py)."""

import asyncio
import json
import logging
from functools import partial
from pathlib import Path

import httpx2
import pytest
from fastapi import FastAPI

from enzo.accounts.google import AUTHORIZE_URL
from enzo.accounts.service import AccountsService
from enzo.brain.config import BrainConfig, load_config
from enzo.config import Settings
from enzo.connectors.google import GoogleConnectors
from enzo.gateway.app import create_app
from enzo.gateway.chatgpt import RedactAuthQuery
from enzo.gateway.connections import SERVICES
from enzo.tools.registry import ToolRegistry
from tests.accounts.vaults import file_store
from tests.connectors.fake_google import CLIENT_FILE, ME, FakeGoogle
from tests.fakes import fake_key


async def accept(_cfg: BrainConfig, _key: str) -> None:
    return None


def google_app(
    database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[FastAPI, FakeGoogle, AccountsService]:
    fake = FakeGoogle()
    accounts = AccountsService(load_config(), file_store(tmp_path), accept, services=SERVICES)
    accounts.enable_google(fake.client())
    monkeypatch.setattr(
        "enzo.gateway.app.GoogleConnectors",
        partial(GoogleConnectors, transport=fake.transport()),
    )
    app = create_app(Settings(env="test", database_url=database_url), accounts=accounts)
    return app, fake, accounts


def registry(app: FastAPI) -> ToolRegistry:
    found: ToolRegistry = app.state.service.registry
    return found


async def tools_named(app: FastAPI, prefix: str, *, present: bool) -> set[str]:
    """The registry follows the account in the background: wait for it."""
    for _ in range(100):
        names = {s.name for s in registry(app).specs() if s.name.startswith(prefix)}
        if bool(names) == present:
            return names
        await asyncio.sleep(0.02)
    msg = f"tools starting with {prefix!r} never became {'present' if present else 'absent'}"
    raise AssertionError(msg)


async def sign_in(c: httpx2.AsyncClient, fake: FakeGoogle) -> str:
    """What the user does: open the link Enzo gives, allow access on Google's page, come back."""
    started = (await c.post("/connections/google/sign-in", json={"access": "full"})).json()
    login = str(started["login_id"])
    assert started["url"] == f"http://127.0.0.1:8787/auth/google/{login}"
    go = await c.get(f"/auth/google/{login}")
    assert go.status_code == 302
    assert go.headers["location"].startswith(AUTHORIZE_URL + "?")
    assert go.headers["referrer-policy"] == "no-referrer"
    back = fake.consent(go.headers["location"])
    page = await c.get("/auth/google/callback", params=back)
    assert page.status_code == 200, page.text
    assert "Signed in to Enzo" in page.text
    assert ME in page.text
    assert "ya29." not in page.text  # no token ever reaches the page
    again = await c.get("/auth/google/callback", params=back)
    assert again.status_code == 400  # the same values a second time do nothing
    return login


async def test_connect_use_check_and_sign_out(
    database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, fake, _ = google_app(database_url, tmp_path, monkeypatch)
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://enzo") as c,
    ):
        rows = {r["provider"]: r for r in (await c.get("/connections")).json()}
        assert (rows["google"]["connected"], rows["google"]["methods"]) == (False, ["google"])

        # No OAuth client yet: the app is told to ask for the user's own.
        first = (await c.post("/connections/google/sign-in")).json()
        assert first["needs_client"] is True
        assert "docs/connectors/google.md" in first["message"]
        web = json.dumps({"web": {"client_id": "1-x.apps.googleusercontent.com"}})
        refused = await c.put("/connections/google/client", json={"client": web})
        assert refused.status_code == 422
        assert "Desktop app" in refused.json()["detail"]
        assert (
            await c.put("/connections/google/client", json={"client": CLIENT_FILE})
        ).status_code == 204

        login = await sign_in(c, fake)
        done = (await c.get(f"/connections/google/sign-in/{login}")).json()
        assert done["status"] == "connected"
        rows = {r["provider"]: r for r in (await c.get("/connections")).json()}
        assert (rows["google"]["method"], rows["google"]["label"]) == ("google", ME)
        assert rows["google"]["purpose"] == (
            "Gmail (read, send, drafts) and Google Calendar (read, edit)"
        )
        assert "gmail_send_message" in await tools_named(app, "gmail_", present=True)
        assert "calendar_create_event" in await tools_named(app, "calendar_", present=True)

        checked = (await c.post("/connections/google/check")).json()
        assert checked["ok"] is True
        assert checked["detail"].startswith("Gmail works. Google Calendar works.")

        key = await c.put("/connections/google", json={"api_key": fake_key("google")})
        assert key.status_code == 409
        assert "signing in" in key.json()["detail"]

        out = await c.delete("/connections/google")
        assert out.json() == {"remote_sign_out": "confirmed"}
        assert all(g.revoked for g in fake._grants)  # revoked at Google, not just forgotten here
        await tools_named(app, "gmail_", present=False)


async def test_a_revoked_connection_is_reported_and_its_tools_go(
    database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, fake, _ = google_app(database_url, tmp_path, monkeypatch)
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://enzo") as c,
    ):
        await c.put("/connections/google/client", json={"client": CLIENT_FILE})
        await sign_in(c, fake)
        await tools_named(app, "gmail_", present=True)
        fake.revoke_all()  # removed in the Google Account
        checked = (await c.post("/connections/google/check")).json()
        assert checked["ok"] is False
        assert "Sign in again: /login google" in checked["detail"]
        assert "works" not in checked["detail"]  # never claims what it could not check
        rows = {r["provider"]: r for r in (await c.get("/connections")).json()}
        assert rows["google"]["connected"] is False
        await tools_named(app, "gmail_", present=False)
        assert registry(app).get("google_status") is None


def test_browser_codes_never_reach_the_access_log() -> None:
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1", "GET", "/auth/google/callback?state=s&code=4/0Ab-secret", "1.1", 200),
        None,
    )
    RedactAuthQuery().filter(record)
    assert "4/0Ab-secret" not in record.getMessage()
