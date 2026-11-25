"""Sign in with Google (ADR 0026): the OAuth client, least-privilege consent with PKCE, tokens
only in the vault, renewal, revocation. Google is a fake (tests/connectors/fake_google.py)."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx2
import jwt
import pytest

from enzo.accounts.google import (
    ACCESS,
    CALENDAR_WRITE,
    GMAIL_READ,
    GMAIL_SEND,
    SIGN_IN_AGAIN,
    ClientFileError,
    GoogleSignIn,
    NoClientError,
    challenge,
    parse_client,
)
from enzo.accounts.store import GoogleCredential
from enzo.brain.errors import AuthError, ProviderConnectionError
from tests.accounts.vaults import file_store
from tests.connectors.fake_google import CALLBACK, CLIENT, CLIENT_FILE, ME, FakeGoogle, signed_in


def query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


def test_only_a_desktop_app_client_file_is_taken() -> None:
    assert parse_client(CLIENT_FILE).client_id == CLIENT.client_id
    web = json.dumps({"web": {"client_id": CLIENT.client_id, "client_secret": "x"}})
    with pytest.raises(ClientFileError, match="Desktop app client"):
        parse_client(web)
    with pytest.raises(ClientFileError, match="not the client file"):
        parse_client("GOCSPX-just-a-secret")
    odd = json.dumps({"installed": {"client_id": "not-google", "client_secret": "x"}})
    with pytest.raises(ClientFileError, match="does not look like"):
        parse_client(odd)


async def test_no_client_means_no_sign_in(tmp_path: Path) -> None:
    sign_in = GoogleSignIn(file_store(tmp_path), FakeGoogle().client())
    with pytest.raises(NoClientError):
        sign_in.start(CALLBACK)
    configured = GoogleSignIn(file_store(tmp_path / "b"), FakeGoogle().client(), configured=CLIENT)
    assert configured.client() == (CLIENT, "configured")
    configured.start(CALLBACK)  # a configured client (ENZO_GOOGLE_CLIENT_ID) is enough


async def test_consent_asks_for_the_least_with_pkce(tmp_path: Path) -> None:
    sign_in = GoogleSignIn(file_store(tmp_path), FakeGoogle().client())
    await sign_in.set_client(CLIENT_FILE)
    full = sign_in.start(CALLBACK, "full")
    q = query(sign_in.authorize_url(full))
    assert q["scope"].split() == list(ACCESS["full"])
    assert q["code_challenge"] == challenge(full.verifier)
    assert full.verifier not in sign_in.authorize_url(full)  # only its hash leaves
    assert (q["access_type"], q["prompt"], q["redirect_uri"]) == ("offline", "consent", CALLBACK)
    read = query(sign_in.authorize_url(sign_in.start(CALLBACK, "read")))["scope"].split()
    assert GMAIL_SEND not in read
    assert CALENDAR_WRITE not in read
    assert GMAIL_READ in read
    assert not any(s.endswith(("gmail.modify", "/auth/calendar", "mail.google.com/")) for s in read)


async def test_tokens_live_only_in_the_vault(tmp_path: Path) -> None:
    store = file_store(tmp_path)
    changed: list[str] = []
    sign_in = await signed_in(FakeGoogle(), store, on_change=lambda: changed.append("google"))
    credential = sign_in.credential()
    assert isinstance(credential, GoogleCredential)
    index = (tmp_path / "connections.json").read_text()
    assert credential.access_token.get_secret_value() not in index
    assert credential.refresh_token.get_secret_value() not in index
    assert "not-a-secret" not in index  # nor the client's secret
    connection = store.connections()["google"]
    assert (connection.method, connection.label) == ("google", ME)
    assert set(connection.scopes or ()) == set(ACCESS["full"]) - {"openid", "email"}
    assert changed == ["google"]


async def test_the_result_says_what_was_allowed_and_what_was_not(tmp_path: Path) -> None:
    fake = FakeGoogle()
    sign_in = GoogleSignIn(file_store(tmp_path), fake.client())
    await sign_in.set_client(CLIENT_FILE)
    attempt = sign_in.start(CALLBACK)
    back = fake.consent(sign_in.authorize_url(attempt), untick=frozenset({"gmail.send"}))
    done = await sign_in.finish(back)
    assert done is not None
    assert done.message == (
        f"Connected Google as {ME}: Gmail (read, drafts) and Google Calendar (read, edit). "
        "You did not allow: Gmail (send)."
    )
    assert GMAIL_SEND not in sign_in.granted()


async def test_a_state_works_once_and_strangers_change_nothing(tmp_path: Path) -> None:
    fake = FakeGoogle()
    sign_in = GoogleSignIn(file_store(tmp_path), fake.client())
    await sign_in.set_client(CLIENT_FILE)
    attempt = sign_in.start(CALLBACK)
    back = fake.consent(sign_in.authorize_url(attempt))
    assert await sign_in.finish({**back, "state": "guessed"}) is None
    assert sign_in.credential() is None
    assert (await sign_in.finish(back)) is not None
    assert await sign_in.finish(back) is None  # a replay finds nothing


@pytest.mark.parametrize(
    ("untick", "deny", "said"),
    [
        (frozenset(), True, "cancelled, so nothing was connected"),
        (
            frozenset(
                {
                    "gmail.messages.readonly",
                    "gmail.send",
                    "gmail.drafts.create",
                    "calendar.events",
                    "calendar.calendarlist.readonly",
                }
            ),
            False,
            "did not allow Enzo to use Gmail or Calendar",
        ),
    ],
)
async def test_nothing_connects_without_consent(
    tmp_path: Path, untick: frozenset[str], deny: bool, said: str
) -> None:
    fake = FakeGoogle()
    sign_in = GoogleSignIn(file_store(tmp_path), fake.client())
    await sign_in.set_client(CLIENT_FILE)
    attempt = sign_in.start(CALLBACK)
    done = await sign_in.finish(
        fake.consent(sign_in.authorize_url(attempt), untick=untick, deny=deny)
    )
    assert done is not None
    assert done.status == "failed"
    assert said in (done.message or "")
    assert sign_in.credential() is None


async def test_the_id_token_must_be_for_this_client_and_attempt(tmp_path: Path) -> None:
    sign_in = GoogleSignIn(file_store(tmp_path), FakeGoogle().client())
    await sign_in.set_client(CLIENT_FILE)
    attempt = sign_in.start(CALLBACK)
    claims = {
        "iss": "https://accounts.google.com",
        "aud": CLIENT.client_id,
        "sub": "1",
        "nonce": attempt.nonce,
        "exp": 4_000_000_000,
    }
    key = b"k" * 32
    assert sign_in._identity(jwt.encode(claims, key, algorithm="HS256"), attempt)["sub"] == "1"
    for wrong in ({"aud": "other.apps.googleusercontent.com"}, {"nonce": "x"}, {"iss": "evil"}):
        token = jwt.encode({**claims, **wrong}, key, algorithm="HS256")
        with pytest.raises(Exception, match="nothing was connected"):
            sign_in._identity(token, attempt)


async def test_tokens_renew_before_they_expire_one_at_a_time(tmp_path: Path) -> None:
    fake = FakeGoogle()
    store = file_store(tmp_path)
    sign_in = await signed_in(fake, store)
    first = await sign_in.access_token()
    assert fake.refreshes == 0  # fresh: used as is
    later = datetime.now(UTC) + timedelta(minutes=58, seconds=30)  # inside the 2-minute margin
    sign_in._now = lambda: later
    tokens = await asyncio.gather(*(sign_in.access_token() for _ in range(5)))
    assert fake.refreshes == 1  # five callers, one renewal
    assert len(set(tokens)) == 1
    assert tokens[0] != first
    assert fake.scopes_of(tokens[0]) == fake.scopes_of(first)


async def test_revoked_access_ends_the_sign_in(tmp_path: Path) -> None:
    fake = FakeGoogle()
    store = file_store(tmp_path)
    changed: list[str] = []
    sign_in = await signed_in(fake, store, on_change=lambda: changed.append("x"))
    fake.revoke_all()  # removed in the user's Google Account
    with pytest.raises(AuthError) as caught:
        await sign_in.access_token(renew=True)
    assert str(caught.value) == SIGN_IN_AGAIN
    assert "google" not in store.connections()
    assert sign_in.credential() is None  # tokens forgotten
    assert changed == ["x", "x"]  # connected, then ended: the tools follow both


async def test_google_unreachable_erases_nothing(tmp_path: Path) -> None:
    fake = FakeGoogle()
    store = file_store(tmp_path)
    sign_in = await signed_in(fake, store)

    def down(_request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("offline")

    sign_in._http = httpx2.AsyncClient(transport=httpx2.MockTransport(down))
    with pytest.raises(ProviderConnectionError):
        await sign_in.access_token(renew=True)
    assert sign_in.credential() is not None
    assert "google" in store.connections()


async def test_sign_out_revokes_at_google(tmp_path: Path) -> None:
    fake = FakeGoogle()
    sign_in = await signed_in(fake, file_store(tmp_path))
    assert await sign_in.sign_out()
    with pytest.raises(AuthError):
        await sign_in.access_token(renew=True)  # Google refuses the old grant now
    other = await signed_in(FakeGoogle(revoke_answers=503), file_store(tmp_path / "b"))
    assert not await other.sign_out()  # not confirmed: the app says how to remove it by hand


async def test_a_new_client_ends_the_old_sign_in(tmp_path: Path) -> None:
    fake = FakeGoogle()
    store = file_store(tmp_path)
    sign_in = await signed_in(fake, store)
    other = json.dumps({"installed": {"client_id": "99-other.apps.googleusercontent.com"}})
    await sign_in.set_client(other)
    assert "google" not in store.connections()
    assert fake._grants[0].revoked  # the old client's grant was revoked, not left behind
    assert sign_in.client() is not None
