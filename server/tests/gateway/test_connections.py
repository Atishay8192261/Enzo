"""Connection endpoints over real HTTP into the app; only the provider check is faked."""

import base64
import logging
from collections.abc import AsyncIterator
from pathlib import Path

import httpx2
import pytest
from pydantic import SecretStr

from enzo.accounts.service import AccountsService
from enzo.accounts.store import CredentialStore
from enzo.accounts.vault import EncryptedFileVault, generate_key
from enzo.brain import AuthError, ProviderConnectionError, RateLimitError
from enzo.brain.config import BrainConfig, load_config
from enzo.config import Settings
from enzo.gateway.app import create_app
from tests.accounts.vaults import file_store
from tests.fakes import fake_key

KEY = fake_key("anthropic")


class Checker:
    def __init__(self) -> None:
        self.error: Exception | None = None

    async def __call__(self, cfg: BrainConfig, api_key: str) -> None:
        if self.error:
            raise self.error


@pytest.fixture
def checker() -> Checker:
    return Checker()


@pytest.fixture
def accounts(tmp_path: Path, checker: Checker) -> AccountsService:
    return AccountsService(load_config(), file_store(tmp_path), checker)


def http(accounts: AccountsService, token: str | None = None) -> httpx2.AsyncClient:
    settings = Settings(env="test", api_token=SecretStr(token) if token else None)
    app = create_app(settings, accounts=accounts)
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://enzo")


@pytest.fixture
async def c(accounts: AccountsService) -> AsyncIterator[httpx2.AsyncClient]:
    async with http(accounts) as client:
        yield client


async def status(c: httpx2.AsyncClient) -> dict[str, dict[str, object]]:
    r = await c.get("/connections")
    assert r.status_code == 200
    return {row["provider"]: row for row in r.json()}


async def test_connect_list_check_and_disconnect(
    c: httpx2.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    before = await status(c)
    assert not before["anthropic"]["connected"]
    assert "claude-sonnet-5-5" in before["anthropic"]["models"]  # type: ignore[operator]

    r = await c.put("/connections/anthropic", json={"api_key": KEY})
    assert r.status_code == 204
    row = (await status(c))["anthropic"]
    assert (row["connected"], row["method"], row["methods"]) == (True, "api_key", ["api_key"])
    assert row["connected_at"]
    assert set(row) == {"provider", "connected", "method", "connected_at", "methods", "models"}

    r = await c.post("/connections/anthropic/check")
    assert r.json() == {"ok": True, "detail": None}

    assert (await c.delete("/connections/anthropic")).status_code == 204
    assert not (await status(c))["anthropic"]["connected"]
    assert KEY not in caplog.text


async def test_a_rejected_key_is_422_and_not_saved(c: httpx2.AsyncClient, checker: Checker) -> None:
    checker.error = AuthError(f"invalid key {KEY}", provider="anthropic", model="m")
    r = await c.put("/connections/anthropic", json={"api_key": KEY})
    assert r.status_code == 422
    assert r.json()["detail"] == "anthropic rejected this key"
    assert KEY not in r.text  # even when the provider's error message quotes the key
    assert not (await status(c))["anthropic"]["connected"]


async def test_an_unreachable_provider_is_502(c: httpx2.AsyncClient, checker: Checker) -> None:
    checker.error = ProviderConnectionError("refused", provider="anthropic", model="m")
    r = await c.put("/connections/anthropic", json={"api_key": KEY})
    assert r.status_code == 502
    assert r.json()["detail"] == "could not check the key with anthropic (connection)"


async def test_a_revoked_key_reports_not_ok(c: httpx2.AsyncClient, checker: Checker) -> None:
    await c.put("/connections/xai", json={"api_key": KEY})
    checker.error = AuthError("revoked", provider="xai", model="m")
    r = await c.post("/connections/xai/check")
    assert r.status_code == 200
    assert r.json() == {"ok": False, "detail": "xai rejected this key"}
    checker.error = RateLimitError("slow down", provider="xai", model="m")
    r = await c.post("/connections/xai/check")
    assert r.json() == {"ok": False, "detail": "could not check the key with xai (rate_limit)"}


async def test_checking_an_unconnected_provider_is_409(c: httpx2.AsyncClient) -> None:
    r = await c.post("/connections/xai/check")
    assert r.status_code == 409
    assert r.json()["detail"] == "xai is not connected: run /login xai"


@pytest.mark.parametrize(
    "request_",
    [
        ("PUT", "/connections/ghost", {"api_key": KEY}),
        ("DELETE", "/connections/ghost", None),
        ("POST", "/connections/ghost/check", None),
    ],
)
async def test_unknown_providers_are_404(
    c: httpx2.AsyncClient, request_: tuple[str, str, object]
) -> None:
    method, url, body = request_
    r = await c.request(method, url, json=body)
    assert r.status_code == 404
    assert r.json()["detail"] == "unknown provider 'ghost'"


@pytest.mark.parametrize(
    "body",
    [
        {"api_key": "sk-short"[:7]},
        {"api_key": KEY + "x" * 600},
        {"key": KEY},
        {"api_key": KEY, "extra": KEY},
    ],
    ids=["too short", "too long", "wrong field", "extra field"],
)
async def test_bad_requests_never_echo_the_key(c: httpx2.AsyncClient, body: object) -> None:
    r = await c.put("/connections/anthropic", json=body)
    assert r.status_code == 422
    assert KEY not in r.text
    assert "sk-shor" not in r.text
    assert all("loc" in e and "msg" in e and "input" not in e for e in r.json()["detail"])


async def test_without_a_vault_connecting_is_503_with_the_fix() -> None:
    service = AccountsService(
        load_config(), None, Checker(), unavailable="no OS keychain: set ENZO_VAULT_KEY"
    )
    async with http(service) as c:
        assert not any(row["connected"] for row in (await status(c)).values())
        r = await c.put("/connections/anthropic", json={"api_key": KEY})
        assert r.status_code == 503
        assert r.json()["detail"] == "no OS keychain: set ENZO_VAULT_KEY"


async def test_connections_need_the_token(accounts: AccountsService) -> None:
    async with http(accounts, token="s3cret") as c:
        assert (await c.get("/connections")).status_code == 401
        r = await c.put("/connections/anthropic", json={"api_key": KEY})
        assert r.status_code == 401
        ok = await c.get("/connections", headers={"Authorization": "Bearer s3cret"})
        assert ok.status_code == 200


async def test_a_vault_opened_with_the_wrong_key_says_so(tmp_path: Path) -> None:
    async with http(AccountsService(load_config(), file_store(tmp_path), Checker())) as c:
        await c.put("/connections/xai", json={"api_key": KEY})
    wrong = EncryptedFileVault(tmp_path / "vault.json", base64.urlsafe_b64decode(generate_key()))
    store = CredentialStore(wrong, tmp_path / "connections.json")
    async with http(AccountsService(load_config(), store, Checker())) as c:
        r = await c.post("/connections/xai/check")
    assert r.status_code == 500
    assert "wrong ENZO_VAULT_KEY" in r.json()["detail"]
