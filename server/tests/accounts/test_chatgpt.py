"""Sign in with ChatGPT, end to end against a fake of OpenAI's sign-in server (ADR 0013)."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from enzo.accounts.chatgpt import (
    DYNAMIC_CLIENT,
    PLAN_SCOPE,
    SIGN_IN_AGAIN,
    TOKEN_URL,
    ChatGPTSignIn,
    challenge,
    host_id,
)
from enzo.accounts.service import AccountsService
from enzo.accounts.store import ChatGPTCredential, CredentialStore
from enzo.brain import AuthError, ProviderServerError
from enzo.brain.config import BrainConfig, load_config
from enzo.brain.factory import PlanLogin
from tests.accounts.fake_openai_auth import (
    PLAN_MODELS,
    REVOKE_URL,
    Account,
    FakeOpenAIAuth,
    client_for,
)
from tests.accounts.vaults import file_store

CALLBACK = "http://127.0.0.1:8787/auth/callback"


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


async def accept(_cfg: BrainConfig, _key: str) -> None:
    return None


def setup(
    tmp_path: Path, fake: FakeOpenAIAuth, clock: Clock | None = None
) -> tuple[ChatGPTSignIn, CredentialStore, list[str]]:
    store = file_store(tmp_path)
    changes: list[str] = []
    sign_in = ChatGPTSignIn(
        store,
        host_id(tmp_path),
        client_for(fake),
        on_change=lambda: changes.append("openai"),
        now=clock or Clock(),
    )
    return sign_in, store, changes


def query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


async def sign_in_once(
    sign_in: ChatGPTSignIn, fake: FakeOpenAIAuth, account: Account | None = None, **kw: Any
) -> str:
    attempt = sign_in.start(CALLBACK, **kw)
    finished = await sign_in.finish(fake.browser(sign_in.authorize_url(attempt), account))
    assert finished is attempt
    return f"{attempt.status}: {attempt.message}"


async def test_first_sign_in_registers_enzo_and_connects_the_plan(tmp_path: Path) -> None:
    fake = FakeOpenAIAuth()
    sign_in, store, changes = setup(tmp_path, fake)
    attempt = sign_in.start(CALLBACK)
    q = query(sign_in.authorize_url(attempt))
    assert (q["client_id"], q["agent_name_hint"]) == (DYNAMIC_CLIENT, "Enzo")
    assert q["ext_agent_host_id"] == host_id(tmp_path)  # the same host id every time
    assert q["code_challenge"] == challenge(attempt.verifier)
    assert q["redirect_uri"] == CALLBACK
    assert PLAN_SCOPE in q["scope"].split()

    await sign_in.finish(fake.browser(sign_in.authorize_url(attempt)))
    assert (attempt.status, attempt.message) == (
        "connected",
        "Signed in with ChatGPT as ada@example.com.",
    )
    credential = store.credential("openai")
    assert isinstance(credential, ChatGPTCredential)
    assert credential.client_id == "oaiapp_user-sub-1"  # the issued id, not the dynamic one
    assert PLAN_SCOPE in credential.scopes
    connection = store.connections()["openai"]
    assert (connection.method, connection.label) == ("chatgpt", "ada@example.com")
    assert connection.models == tuple(PLAN_MODELS)
    assert changes == ["openai"]
    assert not files_with_tokens(tmp_path)  # tokens only in the (encrypted) vault


def files_with_tokens(folder: Path) -> list[str]:
    return [
        p.name
        for p in folder.iterdir()
        if p.name != "vault.json" and ("access-" in p.read_text() or "refresh-" in p.read_text())
    ]


async def test_a_returning_sign_in_reuses_the_registration(tmp_path: Path) -> None:
    fake = FakeOpenAIAuth()
    sign_in, store, _ = setup(tmp_path, fake)
    await sign_in_once(sign_in, fake)
    await AccountsService(load_config(), store, accept, env=None).sign_out("openai")
    attempt = sign_in.start(CALLBACK)
    q = query(sign_in.authorize_url(attempt))
    assert q["client_id"] == "oaiapp_user-sub-1"
    assert "agent_name_hint" not in q  # only on the first registration
    assert q["login_hint"] == "ada@example.com"
    await sign_in.finish(fake.browser(sign_in.authorize_url(attempt)))
    assert attempt.status == "connected"
    assert fake.registrations == 1


async def test_a_different_account_on_a_returning_sign_in_changes_nothing(tmp_path: Path) -> None:
    fake = FakeOpenAIAuth()
    sign_in, store, _ = setup(tmp_path, fake)
    await sign_in_once(sign_in, fake)
    before = store.credential("openai")
    outcome = await sign_in_once(sign_in, fake, Account(subject="someone-else"))
    assert outcome.startswith("failed: That is a different ChatGPT account")
    assert "/login chatgpt new" in outcome
    assert store.credential("openai") == before
    # The way to add it: a new registration.
    assert (
        await sign_in_once(sign_in, fake, Account("someone-else"), new_account=True)
    ).startswith("connected")


async def test_declining_the_plan_connects_nothing_but_keeps_the_registration(
    tmp_path: Path,
) -> None:
    fake = FakeOpenAIAuth()
    sign_in, store, changes = setup(tmp_path, fake)
    outcome = await sign_in_once(sign_in, fake, Account(allows_plan=False))
    assert outcome.startswith("failed: You signed in, but Enzo was not allowed to use your")
    assert "openai" not in store.connections()
    assert changes == []
    registration = sign_in.registration()
    assert registration is not None
    assert registration.client_id == "oaiapp_user-sub-1"


async def test_cancelling_in_the_browser_connects_nothing(tmp_path: Path) -> None:
    fake = FakeOpenAIAuth()
    sign_in, store, _ = setup(tmp_path, fake)
    attempt = sign_in.start(CALLBACK)
    await sign_in.finish({"error": "access_denied", "state": attempt.state})
    assert (attempt.status, attempt.message) == (
        "failed",
        "The ChatGPT sign-in was cancelled, so nothing was connected.",
    )
    assert not fake.calls_to(TOKEN_URL)
    assert "openai" not in store.connections()


async def test_unknown_or_replayed_state_is_ignored(tmp_path: Path) -> None:
    fake = FakeOpenAIAuth()
    sign_in, _store, _ = setup(tmp_path, fake)
    attempt = sign_in.start(CALLBACK)
    back = fake.browser(sign_in.authorize_url(attempt))
    assert await sign_in.finish({**back, "state": "forged"}) is None
    assert attempt.status == "pending"
    assert (await sign_in.finish(back)) is attempt
    assert await sign_in.finish(back) is None  # a replayed callback finds nothing
    assert not fake.calls_to(TOKEN_URL)[1:]  # one exchange only


async def test_an_error_code_from_outside_is_never_echoed_as_free_text(tmp_path: Path) -> None:
    sign_in, _, _ = setup(tmp_path, FakeOpenAIAuth())
    attempt = sign_in.start(CALLBACK)
    await sign_in.finish({"error": "server_error<script>alert(1)</script>", "state": attempt.state})
    assert attempt.message == (
        "ChatGPT sign-in failed (server_errorscriptalert1script). Nothing was connected."
    )


@pytest.mark.parametrize(
    "tamper",
    [
        {"nonce": "another-attempt"},
        {"aud": ["oaiapp_someone-else"]},
        {"iss": "https://evil.example"},
        {"exp": 1_000_000_000},  # long expired
        {"sub": ""},
        "signed by another key",
        "HS256",
    ],
)
async def test_the_id_token_must_check_out_or_nothing_connects(
    tmp_path: Path, tamper: dict[str, Any] | str
) -> None:
    fake = FakeOpenAIAuth()
    if tamper == "signed by another key":
        fake.sign_with = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    elif tamper == "HS256":
        fake.algorithm = "HS256"
    else:
        assert isinstance(tamper, dict)
        fake.id_token_override = tamper
    sign_in, store, _ = setup(tmp_path, fake)
    outcome = await sign_in_once(sign_in, fake)
    assert outcome.startswith("failed: ")
    assert "nothing was connected" in outcome
    assert "openai" not in store.connections()


async def test_a_stolen_code_cannot_be_exchanged_without_the_verifier(tmp_path: Path) -> None:
    fake = FakeOpenAIAuth()
    sign_in, store, _ = setup(tmp_path, fake)
    attempt = sign_in.start(CALLBACK)
    back = fake.browser(sign_in.authorize_url(attempt))
    attempt.verifier = "an attacker's own verifier"  # the code was minted for another one
    await sign_in.finish(back)
    assert attempt.status == "failed"
    assert "openai" not in store.connections()


async def test_tokens_are_renewed_once_shortly_before_they_expire(tmp_path: Path) -> None:
    fake = FakeOpenAIAuth()
    clock = Clock()
    sign_in, store, _ = setup(tmp_path, fake, clock)
    await sign_in_once(sign_in, fake)
    first = await sign_in.access_token()
    assert len(fake.calls_to(TOKEN_URL)) == 1  # fresh: no renewal
    clock.now += timedelta(minutes=59)  # inside the two-minute margin
    renewed = await asyncio.gather(*(sign_in.access_token() for _ in range(5)))
    assert len(set(renewed)) == 1
    assert renewed[0] != first
    refreshes = fake.calls_to(TOKEN_URL)[1:]
    assert len(refreshes) == 1  # five callers, one renewal: refresh tokens rotate
    form = parse_qs(refreshes[0].content.decode())
    assert form["client_id"] == ["oaiapp_user-sub-1"]
    assert form["grant_type"] == ["refresh_token"]
    credential = store.credential("openai")
    assert isinstance(credential, ChatGPTCredential)
    assert credential.refresh_token.get_secret_value() in fake.refresh_tokens  # the replacement


async def test_an_ended_sign_in_says_so_and_forgets_the_tokens(tmp_path: Path) -> None:
    fake = FakeOpenAIAuth()
    clock = Clock()
    sign_in, store, changes = setup(tmp_path, fake, clock)
    await sign_in_once(sign_in, fake)
    clock.now += timedelta(hours=2)
    fake.token_reply = (400, {"error": "invalid_grant"})
    with pytest.raises(AuthError, match="Sign in again: /login chatgpt"):
        await sign_in.access_token()
    assert "openai" not in store.connections()
    assert changes == ["openai", "openai"]
    assert sign_in.registration() is not None  # kept for the next sign-in
    with pytest.raises(AuthError) as exc:
        await sign_in.access_token()
    assert str(exc.value) == SIGN_IN_AGAIN


async def test_an_outage_while_renewing_keeps_the_sign_in(tmp_path: Path) -> None:
    fake = FakeOpenAIAuth()
    clock = Clock()
    sign_in, store, _ = setup(tmp_path, fake, clock)
    await sign_in_once(sign_in, fake)
    clock.now += timedelta(hours=2)
    fake.token_reply = (503, {"error": "temporarily_unavailable"})
    with pytest.raises(ProviderServerError):
        await sign_in.access_token()
    assert "openai" in store.connections()  # never erased for a temporary failure
    assert await sign_in.access_token()  # and it renews once OpenAI is back


@pytest.mark.parametrize(("status", "expected"), [(200, True), (503, False)])
async def test_signing_out_ends_the_session_with_openai(
    tmp_path: Path, status: int, expected: bool
) -> None:
    fake = FakeOpenAIAuth()
    fake.revoke_status = status
    sign_in, store, _ = setup(tmp_path, fake)
    accounts = AccountsService(load_config(), store, accept)
    accounts.chatgpt = sign_in
    await sign_in_once(sign_in, fake)
    credential = store.credential("openai")
    assert isinstance(credential, ChatGPTCredential)
    assert await accounts.sign_out("openai") is expected
    (revoke,) = fake.calls_to(REVOKE_URL)
    form = parse_qs(revoke.content.decode())
    assert form == {
        "token": [credential.refresh_token.get_secret_value()],
        "token_type_hint": ["refresh_token"],
        "client_id": ["oaiapp_user-sub-1"],
    }
    assert store.credential("openai") is None  # forgotten here either way
    assert sign_in.registration() is not None


async def test_the_plan_pays_for_gpt_and_only_for_models_it_includes(tmp_path: Path) -> None:
    fake = FakeOpenAIAuth()
    sign_in, store, _ = setup(tmp_path, fake)
    config = load_config()
    accounts = AccountsService(config, store, accept)
    accounts.chatgpt = sign_in
    assert accounts.auth("openai") is None
    await sign_in_once(sign_in, fake)
    auth = accounts.auth("openai")
    assert isinstance(auth, PlanLogin)
    assert await auth.access_token() in fake.access_tokens
    usable = {n for n, c in config.brains.items() if accounts.usable(c)}
    assert usable == {"gpt-sol", "gpt-astra"}  # gpt-luna is not on this plan's list
    (row,) = [s for s in accounts.status() if s.provider == "openai"]
    assert (row.method, row.methods, row.label) == (
        "chatgpt",
        ("chatgpt", "api_key"),
        "ada@example.com",
    )


async def test_connecting_a_key_ends_the_chatgpt_session(tmp_path: Path) -> None:
    fake = FakeOpenAIAuth()
    sign_in, store, _ = setup(tmp_path, fake)
    accounts = AccountsService(load_config(), store, accept)
    accounts.chatgpt = sign_in
    await sign_in_once(sign_in, fake)
    await accounts.connect_api_key("openai", "fake-openai-000000000000")
    assert len(fake.calls_to(REVOKE_URL)) == 1
    assert accounts.auth("openai") == "fake-openai-000000000000"


def test_the_host_id_is_made_once(tmp_path: Path) -> None:
    first = host_id(tmp_path)
    assert first.startswith("urn:uuid:")
    assert host_id(tmp_path) == first
    assert (tmp_path / "host-id").stat().st_mode & 0o777 == 0o600
    assert json.dumps(first)  # plain text, no secret


async def test_a_returning_callback_for_another_client_changes_nothing(tmp_path: Path) -> None:
    fake = FakeOpenAIAuth()
    sign_in, store, _ = setup(tmp_path, fake)
    await sign_in_once(sign_in, fake)
    before = store.credential("openai")
    attempt = sign_in.start(CALLBACK)
    back = fake.browser(sign_in.authorize_url(attempt))
    await sign_in.finish({**back, "client_id": "oaiapp_someone-else"})
    assert (attempt.status, attempt.message) == (
        "failed",
        "ChatGPT answered for a different app registration, so nothing changed.",
    )
    assert store.credential("openai") == before


async def test_two_callbacks_at_once_exchange_the_code_once(tmp_path: Path) -> None:
    fake = FakeOpenAIAuth()
    sign_in, _, _ = setup(tmp_path, fake)
    attempt = sign_in.start(CALLBACK)
    back = fake.browser(sign_in.authorize_url(attempt))
    first, second = await asyncio.gather(sign_in.finish(back), sign_in.finish(back))
    assert (first, second) == (attempt, None)
    assert len(fake.calls_to(TOKEN_URL)) == 1


async def test_an_empty_state_never_matches_a_finished_attempt(tmp_path: Path) -> None:
    fake = FakeOpenAIAuth()
    sign_in, _, _ = setup(tmp_path, fake)
    await sign_in_once(sign_in, fake)  # its state is used up now
    attempt = sign_in.start(CALLBACK)
    back = fake.browser(sign_in.authorize_url(attempt))
    assert await sign_in.finish({**back, "state": ""}) is None
    assert await sign_in.finish({k: v for k, v in back.items() if k != "state"}) is None
