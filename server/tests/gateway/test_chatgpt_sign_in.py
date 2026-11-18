"""Sign in with ChatGPT through the gateway: the browser round trip, then a reply the plan pays
for, then signing out. Real app and Postgres; OpenAI's servers are fakes (ADR 0013)."""

import functools
import json
import logging
from pathlib import Path
from typing import Any

import httpx2
import openai
import pytest
from fastapi import FastAPI

from enzo.accounts.chatgpt import AUTHORIZE_URL, ChatGPTSignIn, host_id
from enzo.accounts.service import AccountsService
from enzo.brain.config import BrainConfig, load_config
from enzo.config import Settings
from enzo.gateway.app import create_app
from enzo.gateway.chatgpt import RedactAuthQuery
from tests.accounts.fake_openai_auth import FakeOpenAIAuth, client_for
from tests.accounts.vaults import file_store
from tests.brain.test_openai_brain import TEXT, response, sse
from tests.gateway.test_api import events


async def accept(_cfg: BrainConfig, _key: str) -> None:
    return None


def plan_app(
    database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[FastAPI, FakeOpenAIAuth, list[dict[str, Any]]]:
    """The real app, with OpenAI's sign-in server and Responses API faked."""
    fake = FakeOpenAIAuth()
    accounts = AccountsService(load_config(), file_store(tmp_path), accept)
    accounts.chatgpt = ChatGPTSignIn(
        accounts._require_store(),
        host_id(tmp_path),
        client_for(fake),
        on_change=lambda: accounts._changed("openai"),
    )
    # GPT replies go to a fake Responses API that only accepts the plan's access tokens.
    seen: list[dict[str, Any]] = []

    def responses_api(request: httpx2.Request) -> httpx2.Response:
        token = request.headers["authorization"].removeprefix("Bearer ")
        if token not in fake.access_tokens:
            return httpx2.Response(401, json={"detail": "not a plan token"})
        seen.append(json.loads(request.content))
        return httpx2.Response(
            200, headers={"content-type": "text/event-stream"}, content=sse(response([TEXT]))
        )

    http = openai.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(responses_api))
    monkeypatch.setattr(
        "enzo.brain.factory.openai.AsyncOpenAI",
        functools.partial(openai.AsyncOpenAI, http_client=http),
    )
    app = create_app(Settings(env="test", database_url=database_url), accounts=accounts)
    return app, fake, seen


async def browser_round_trip(c: httpx2.AsyncClient, fake: FakeOpenAIAuth) -> str:
    """What the user does: open the link Enzo gives, sign in on OpenAI's page, come back."""
    started = (await c.post("/connections/openai/chatgpt")).json()
    login = str(started["login_id"])
    assert started["url"] == f"http://127.0.0.1:8787/auth/chatgpt/{login}"
    go = await c.get(f"/auth/chatgpt/{login}")
    assert go.status_code == 302
    assert go.headers["location"].startswith(AUTHORIZE_URL + "?")
    assert go.headers["referrer-policy"] == "no-referrer"
    pending = (await c.get(f"/connections/openai/chatgpt/{login}")).json()
    assert pending["status"] == "pending"
    back = fake.browser(go.headers["location"])
    page = await c.get("/auth/callback", params=back)
    assert page.status_code == 200
    assert "Signed in to Enzo" in page.text
    assert "ada@example.com" in page.text
    assert "access-" not in page.text  # no token ever reaches the page
    again = await c.get("/auth/callback", params=back)
    assert again.status_code == 400  # the same values a second time do nothing
    return login


async def test_sign_in_reply_on_the_plan_then_sign_out(
    database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, fake, seen = plan_app(database_url, tmp_path, monkeypatch)
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://enzo") as c,
    ):
        login = await browser_round_trip(c, fake)
        done = (await c.get(f"/connections/openai/chatgpt/{login}")).json()
        assert (done["status"], done["first_time"]) == ("connected", True)
        assert done["usage_url"] == "https://chatgpt.com/settings/usage"
        rows = {r["provider"]: r for r in (await c.get("/connections")).json()}
        assert (rows["openai"]["method"], rows["openai"]["label"]) == (
            "chatgpt",
            "ada@example.com",
        )
        assert rows["openai"]["methods"] == ["chatgpt", "api_key"]
        brains = {b["name"]: b["available"] for b in (await c.get("/brains")).json()}
        assert brains["gpt-sol"]
        assert not brains["gpt-luna"]  # not on this plan

        cid = (await c.post("/conversations", json={"kind": "main"})).json()["id"]
        await c.put(f"/conversations/{cid}/brain", json={"brain": "gpt-sol"})
        turn = events((await c.post(f"/conversations/{cid}/turns", json={"message": "hi"})).text)
        kind, end = turn[-1]
        assert kind == "done", turn
        assert (end["text"], end["model"], end["plan"], end["cost_usd"]) == (
            "Hello",
            "gpt-test",
            True,
            0,
        )
        assert "max_output_tokens" not in seen[0]

        out = await c.delete("/connections/openai")
        assert out.json() == {"remote_sign_out": "confirmed"}
        rows = {r["provider"]: r for r in (await c.get("/connections")).json()}
        assert not rows["openai"]["connected"]


async def test_a_cancelled_sign_in_shows_why(tmp_path: Path) -> None:
    accounts = AccountsService(load_config(), file_store(tmp_path), accept)
    fake = FakeOpenAIAuth()
    accounts.chatgpt = ChatGPTSignIn(
        accounts._require_store(), host_id(tmp_path), client_for(fake), on_change=lambda: None
    )
    app = create_app(Settings(env="test"), accounts=accounts)
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://enzo"
    ) as c:
        login = (await c.post("/connections/openai/chatgpt")).json()["login_id"]
        attempt = accounts.chatgpt.attempt(login)
        assert attempt is not None
        page = await c.get(
            "/auth/callback", params={"error": "access_denied", "state": attempt.state}
        )
        assert page.status_code == 400
        assert "cancelled" in page.text
        status = (await c.get(f"/connections/openai/chatgpt/{login}")).json()
        assert (status["status"], status["first_time"]) == ("failed", False)
        assert (await c.get("/connections/openai/chatgpt/unknown")).status_code == 404
        expired = await c.get("/auth/chatgpt/unknown")
        assert "expired" in expired.text


def test_access_logs_never_keep_the_sign_in_code() -> None:
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5", "GET", "/auth/callback?code=c0de&state=s7", "1.1", 200),
        None,
    )
    assert RedactAuthQuery().filter(record)
    assert record.getMessage() == '127.0.0.1:5 - "GET /auth/callback?[redacted] HTTP/1.1" 200'
    other = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5", "GET", "/brains?x=1", "1.1", 200),
        None,
    )
    RedactAuthQuery().filter(other)
    assert "/brains?x=1" in other.getMessage()
