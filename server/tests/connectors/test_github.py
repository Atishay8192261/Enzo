"""GitHub's own pieces: the REST tools, the token check, and tools following the account."""

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx2
import pytest
from pydantic import BaseModel

from enzo.accounts.service import AccountsService
from enzo.brain import AuthError, ProviderServerError, RateLimitError, ToolCall
from enzo.brain.config import load_config
from enzo.brain.toolcall import ValidatedCall
from enzo.connectors.github import API_VERSION, check_github_token, github_tools, rest_tools
from enzo.gateway.app import Connectors
from enzo.gateway.connections import SERVICES
from enzo.tools import ToolRegistry
from tests.accounts.vaults import file_store
from tests.connectors.fake_github import client_for, fake_github
from tests.fakes import fake_key

TOKEN = fake_key("github")
type Handler = Callable[[httpx2.Request], httpx2.Response]


class FakeRest:
    """api.github.com for one repo: default branch main, a feature branch, releases."""

    def __init__(self) -> None:
        self.feature_sha = "1111111111111111"
        self.requests: list[httpx2.Request] = []
        self.status: int | None = None

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self.status is not None:
            return httpx2.Response(self.status, json={"message": "nope"})
        path, method = request.url.path, request.method
        if path == "/repos/octo/site" and method == "GET":
            return httpx2.Response(200, json={"default_branch": "main"})
        if path.endswith("/git/ref/heads/feature") and method == "GET":
            return httpx2.Response(200, json={"object": {"sha": self.feature_sha}})
        if path.endswith("/git/refs/heads/feature") and method == "DELETE":
            return httpx2.Response(204)
        if path.endswith("/releases") and method == "POST":
            return httpx2.Response(
                201, json={"html_url": "https://github.com/octo/site/releases/v1"}
            )
        return httpx2.Response(404, json={"message": "Not Found"})


def call(name: str, **args: Any) -> ToolCall:
    return ToolCall(id="c1", name=name, arguments_json=json.dumps(args))


def args_of(registry: ToolRegistry, c: ToolCall) -> BaseModel:
    checked = registry.validate(c)
    assert isinstance(checked, ValidatedCall)
    return checked.args


async def rest(fake: FakeRest) -> tuple[ToolRegistry, httpx2.AsyncClient]:
    http = httpx2.AsyncClient(transport=httpx2.MockTransport(fake))
    return ToolRegistry(rest_tools(lambda: TOKEN, http)), http


async def test_deleting_a_branch_is_pinned_and_rechecked() -> None:
    fake = FakeRest()
    registry, http = await rest(fake)
    async with http:
        t = registry.get("github_delete_branch")
        assert t is not None
        assert t.requires_approval
        assert t.prepare is not None
        args = args_of(
            registry, call("github_delete_branch", owner="octo", repo="site", branch="feature")
        )
        prepared = await t.prepare(args)
        assert prepared.args.model_dump()["expected_sha"] == fake.feature_sha
        assert prepared.detail == "Deletes branch feature of octo/site, now at commit 111111111111."
        pinned = call("github_delete_branch", **prepared.args.model_dump())
        assert (await registry.execute(pinned)).status == "ok"
        assert fake.requests[-1].method == "DELETE"
        headers = fake.requests[-1].headers
        assert (headers["authorization"], headers["x-github-api-version"]) == (
            f"Bearer {TOKEN}",
            API_VERSION,
        )
        fake.feature_sha = "2222222222222222"  # pushed to after approval
        moved = await registry.execute(pinned)
        assert moved.status == "failed"
        assert "moved after you approved" in moved.result.content


async def test_the_default_branch_is_never_deleted() -> None:
    fake = FakeRest()
    registry, http = await rest(fake)
    async with http:
        t = registry.get("github_delete_branch")
        assert t is not None
        assert t.prepare is not None
        args = args_of(
            registry, call("github_delete_branch", owner="octo", repo="site", branch="main")
        )
        with pytest.raises(Exception, match="default branch of octo/site; Enzo never deletes it"):
            await t.prepare(args)
    assert not [r for r in fake.requests if r.method == "DELETE"]


async def test_a_release_is_published_with_approval() -> None:
    fake = FakeRest()
    registry, http = await rest(fake)
    async with http:
        t = registry.get("github_create_release")
        assert t is not None
        assert t.requires_approval
        args = args_of(registry, call("github_create_release", owner="octo", repo="site", tag="v1"))
        assert t.summary is not None
        assert t.summary(args) == "GitHub: publish release v1 (octo/site)"
        out = await registry.execute(
            call("github_create_release", owner="octo", repo="site", tag="v1")
        )
    assert out.status == "ok"
    body = json.loads(fake.requests[-1].content)
    assert (body["tag_name"], body["name"], body["draft"]) == ("v1", "v1", False)


@pytest.mark.parametrize(
    ("status", "message"),
    [(401, "rejected the token: reconnect with /login github"), (429, "rate limiting")],
)
async def test_rest_failures_say_what_to_do(status: int, message: str) -> None:
    fake = FakeRest()
    fake.status = status
    registry, http = await rest(fake)
    async with http:
        out = await registry.execute(call("github_create_release", owner="o", repo="r", tag="v1"))
    assert out.status == "failed"
    assert message in out.result.content


@pytest.mark.parametrize(
    ("status", "headers", "error"),
    [
        (200, {}, None),
        (401, {}, AuthError),
        (403, {"x-ratelimit-remaining": "0"}, RateLimitError),
        (429, {}, RateLimitError),
        (500, {}, ProviderServerError),
    ],
)
async def test_token_check(
    status: int, headers: dict[str, str], error: type[Exception] | None
) -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(status, json={"login": "octo"}, headers=headers)

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http:
        if error is None:
            await check_github_token(TOKEN, http_client=http)
        else:
            with pytest.raises(error):
                await check_github_token(TOKEN, http_client=http)
    assert str(seen[0].url) == "https://api.github.com/user"


async def test_github_tools_follow_the_account(tmp_path: Path) -> None:
    server, _ = fake_github()
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(FakeRest())) as http:
        assert await github_tools(lambda: None, http) == []  # not connected: no tools
        tools = await github_tools(lambda: TOKEN, http, connect=lambda: client_for(server))
    names = {t.name for t in tools}
    assert {"github_merge_pull_request", "github_delete_branch", "github_create_release"} <= names


async def test_connectors_swap_tools_when_the_account_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, _ = fake_github()

    async def check(_key: str) -> None:
        return None

    services = {"github": replace(SERVICES["github"], check=check)}
    accounts = AccountsService(load_config(), file_store(tmp_path), check_key, services=services)
    registry = ToolRegistry()

    async def tools_with_fake(
        token: Callable[[], str | None], http: httpx2.AsyncClient
    ) -> list[Any]:
        return await github_tools(token, http, connect=lambda: client_for(server))

    monkeypatch.setattr("enzo.gateway.app.github_tools", tools_with_fake)
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(FakeRest())) as http:
        hub = Connectors(registry, accounts, http)
        await hub.refresh("github")
        assert not [s for s in registry.specs() if s.name.startswith("github_")]
        await accounts.connect_api_key("github", TOKEN)
        await hub.refresh("github")
        assert registry.get("github_merge_pull_request") is not None
        accounts.disconnect("github")
        await hub.refresh("github")
        assert registry.get("github_merge_pull_request") is None
        await hub.close()


async def check_key(_cfg: Any, _key: str) -> None:
    return None
