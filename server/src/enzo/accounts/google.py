"""Sign in with Google, for Gmail and Google Calendar (ADR 0026).

OAuth 2.0 for installed apps, as Google documents it for desktop programs:

1. An OAuth client of the "Desktop app" type. Whose client it is is configuration (ADR 0026): the
   user's own, pasted in the app (``/login google``), else ``ENZO_GOOGLE_CLIENT_ID`` /
   ``ENZO_GOOGLE_CLIENT_SECRET``, else one Enzo ships (none yet). An installed app's client secret
   is not a secret to Google; it is kept in the vault all the same.
2. Authorization code + PKCE (S256) in the system browser, with fresh ``state``, ``nonce`` and
   verifier per attempt. The callback is the gateway itself,
   ``http://127.0.0.1:<port>/auth/google/callback``: Google accepts any loopback port and path
   for a Desktop client (checked 2026-11-25). ``access_type=offline`` + ``prompt=consent`` give a
   refresh token every time.
3. Least privilege: only the scopes the chosen access needs, and Google's consent screen lets the
   user untick any of them, so what was granted is read back and decides which tools exist.
4. Tokens live in the vault; access tokens last an hour and are renewed shortly before expiry, one
   renewal at a time. A refused renewal (``invalid_grant``: revoked, or a 7-day "Testing" app)
   forgets them and says to sign in again; Google being unreachable erases nothing.
"""

import asyncio
import base64
import hashlib
import json
import logging
import re
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import quote, urlencode

import httpx2
import jwt
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from enzo.accounts.store import CredentialStore, GoogleCredential
from enzo.brain.errors import (
    AuthError,
    ProviderConnectionError,
    ProviderServerError,
    ProviderTimeoutError,
)

log = logging.getLogger("enzo.accounts.google")

PROVIDER = "google"
AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - an address, not a secret
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
#: Where the user sees and removes what Enzo may do with their Google account.
PERMISSIONS_URL = "https://myaccount.google.com/connections"
ISSUERS = ("https://accounts.google.com", "accounts.google.com")
CALLBACK_PATH = "/auth/google/callback"

_AUTH = "https://www.googleapis.com/auth/"
#: Read emails (not settings, not delete). Gmail's reads accept it (per-tool metadata, 2026-11-25).
GMAIL_READ = _AUTH + "gmail.messages.readonly"
#: Send email; replies and forwards too.
GMAIL_SEND = _AUTH + "gmail.send"
#: Save new drafts only.
GMAIL_DRAFT = _AUTH + "gmail.drafts.create"
CALENDAR_READ = _AUTH + "calendar.events.readonly"
CALENDAR_LIST = _AUTH + "calendar.calendarlist.readonly"
CALENDAR_FREEBUSY = _AUTH + "calendar.events.freebusy"
#: Create, change, delete and answer events (Google's MCP server accepts no narrower scope).
CALENDAR_WRITE = _AUTH + "calendar.events"
#: Only to name the account ("Connected as ada@example.com").
IDENTITY = ("openid", "email")

type Access = Literal["full", "read"]
#: What each choice asks Google for: the least each set of features needs.
ACCESS: dict[Access, tuple[str, ...]] = {
    "read": (*IDENTITY, GMAIL_READ, CALENDAR_READ, CALENDAR_LIST, CALENDAR_FREEBUSY),
    "full": (*IDENTITY, GMAIL_READ, GMAIL_SEND, GMAIL_DRAFT, CALENDAR_WRITE, CALENDAR_LIST),
}
SERVICE_SCOPES = frozenset(
    {GMAIL_READ, GMAIL_SEND, GMAIL_DRAFT, CALENDAR_READ, CALENDAR_LIST, CALENDAR_FREEBUSY}
    | {CALENDAR_WRITE}
)

#: Set this when Enzo ships its own verified OAuth client (Atishay's decision, ADR 0026).
BUNDLED_CLIENT: "GoogleClient | None" = None

ATTEMPT_TTL_S = 10 * 60
REFRESH_MARGIN = timedelta(minutes=2)
_RECORD = "google-client"

SIGN_IN_AGAIN = (
    "Google ended Enzo's access to your account (it was removed, or it expired: an OAuth app in "
    "Testing mode gives 7-day sign-ins). Sign in again: /login google"
)


class SignInError(Exception):
    """A sign-in attempt failed. The message is shown to the user as is."""


class NoClientError(Exception):
    """No OAuth client is set up yet: the user adds their own first (docs/connectors/google.md)."""


class ClientFileError(ValueError):
    """The pasted OAuth client is not one Enzo can use. The message says why, in plain words."""


class GoogleClient(BaseModel):
    """An OAuth client of the Desktop app type."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    client_id: str = Field(pattern=r"^[0-9A-Za-z._-]+\.apps\.googleusercontent\.com$")
    client_secret: SecretStr | None = None

    def secret_json(self) -> str:
        secret = self.client_secret.get_secret_value() if self.client_secret else None
        return json.dumps({"client_id": self.client_id, "client_secret": secret})


type ClientSource = Literal["yours", "configured", "enzo"]


def parse_client(text: str) -> GoogleClient:
    """The client file Google's console downloads (``{"installed": {...}}``)."""
    try:
        data = json.loads(text)
    except ValueError as exc:
        msg = "That is not the client file: paste the whole JSON file you downloaded from Google."
        raise ClientFileError(msg) from exc
    if isinstance(data, dict) and "web" in data:
        msg = (
            "That is a Web application client. Enzo needs a Desktop app client: create one in "
            "Google Cloud (Credentials, Create OAuth client, Desktop app) and paste its file."
        )
        raise ClientFileError(msg)
    installed = data.get("installed") if isinstance(data, dict) else None
    if not isinstance(installed, dict):
        msg = 'That file has no Desktop app client in it (no "installed" section).'
        raise ClientFileError(msg)
    try:
        return GoogleClient(
            client_id=str(installed.get("client_id", "")),
            client_secret=installed.get("client_secret") or None,
        )
    except ValidationError as exc:
        msg = "The client id in that file does not look like a Google OAuth client id."
        raise ClientFileError(msg) from exc


def challenge(verifier: str) -> str:
    """PKCE S256: base64url of the SHA-256 of the verifier, without padding."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def describe_access(scopes: frozenset[str] | set[str] | tuple[str, ...]) -> str:
    """What a grant lets Enzo do, in plain words: "Gmail (read, send) and Calendar (read)"."""
    granted = set(scopes)
    words = ((GMAIL_READ, "read"), (GMAIL_SEND, "send"), (GMAIL_DRAFT, "drafts"))
    gmail = [word for scope, word in words if scope in granted]
    calendar = []
    if granted & {CALENDAR_READ, CALENDAR_WRITE, CALENDAR_LIST, CALENDAR_FREEBUSY}:
        calendar.append("read")
    if CALENDAR_WRITE in granted:
        calendar.append("edit")
    parts = []
    if gmail:
        parts.append(f"Gmail ({', '.join(gmail)})")
    if calendar:
        parts.append(f"Google Calendar ({', '.join(calendar)})")
    return " and ".join(parts) or "nothing yet"


@dataclass
class Attempt:
    """One sign-in in progress: secrets only this gateway and the browser round trip see."""

    id: str
    state: str
    nonce: str
    verifier: str
    redirect_uri: str
    client: GoogleClient
    access: Access
    started: float
    status: Literal["pending", "connected", "failed"] = "pending"
    message: str | None = None


class GoogleSignIn:
    """Signs in, keeps the tokens fresh, and signs out, for one Google account at a time."""

    def __init__(
        self,
        store: CredentialStore,
        http: httpx2.AsyncClient,
        *,
        configured: GoogleClient | None = None,
        on_change: Callable[[], None] = lambda: None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._http = http
        self._configured = configured
        self._on_change = on_change
        self._clock = clock
        self._now = now
        self._attempts: dict[str, Attempt] = {}
        self._renewing = asyncio.Lock()

    # ---------- the OAuth client ----------

    def client(self) -> tuple[GoogleClient, ClientSource] | None:
        """The client to sign in with: the user's own first, then configuration, then Enzo's."""
        raw = self._store.record(_RECORD)
        if raw is not None:
            try:
                data = json.loads(raw)
                return GoogleClient(
                    client_id=data["client_id"], client_secret=data.get("client_secret")
                ), "yours"
            except (ValueError, KeyError, ValidationError):
                log.warning("stored google client unreadable")
        if self._configured is not None:
            return self._configured, "configured"
        if BUNDLED_CLIENT is not None:
            return BUNDLED_CLIENT, "enzo"
        return None

    async def set_client(self, text: str) -> GoogleClient:
        """Keep the user's own client. A sign-in made with another client ends first."""
        client = parse_client(text.strip())
        current = self.credential()
        if current is not None and current.client_id != client.client_id:
            await self.sign_out()
            self._store.disconnect(PROVIDER)
            self._on_change()
        self._store.keep_record(_RECORD, client.secret_json())
        return client

    # ---------- signing in ----------

    def start(self, redirect_uri: str, access: Access = "full") -> Attempt:
        found = self.client()
        if found is None:
            raise NoClientError
        self._forget_stale()
        attempt = Attempt(
            id=secrets.token_urlsafe(16),
            state=secrets.token_urlsafe(32),
            nonce=secrets.token_urlsafe(32),
            verifier=secrets.token_urlsafe(64),
            redirect_uri=redirect_uri,
            client=found[0],
            access=access,
            started=self._clock(),
        )
        self._attempts[attempt.id] = attempt
        return attempt

    def attempt(self, login_id: str) -> Attempt | None:
        self._forget_stale()
        return self._attempts.get(login_id)

    def authorize_url(self, attempt: Attempt) -> str:
        """Google's consent page for this attempt. Carries one-time values, never a token."""
        params = {
            "response_type": "code",
            "client_id": attempt.client.client_id,
            "redirect_uri": attempt.redirect_uri,
            "scope": " ".join(ACCESS[attempt.access]),
            "state": attempt.state,
            "nonce": attempt.nonce,
            "code_challenge": challenge(attempt.verifier),
            "code_challenge_method": "S256",
            "access_type": "offline",
            "prompt": "consent",
        }
        current = self.credential()
        if current is not None and current.email:
            params["login_hint"] = current.email
        return f"{AUTHORIZE_URL}?{urlencode(params, quote_via=quote)}"

    async def finish(self, params: Mapping[str, str]) -> Attempt | None:
        """The browser came back. None for an unknown state (nothing is touched then). Each state
        is accepted once."""
        attempt = self._pending_for(params.get("state", ""))
        if attempt is None:
            return None
        try:
            message = await self._complete(attempt, params)
        except SignInError as exc:
            attempt.status, attempt.message = "failed", str(exc)
        except httpx2.HTTPError as exc:
            log.warning("google sign-in unreachable", extra={"error": type(exc).__name__})
            attempt.status = "failed"
            attempt.message = "Could not reach Google to finish signing in. Check the connection."
        else:
            attempt.status, attempt.message = "connected", message
        return attempt

    async def _complete(self, attempt: Attempt, params: Mapping[str, str]) -> str:
        if (error := params.get("error")) is not None:
            if error == "access_denied":
                msg = "The Google sign-in was cancelled, so nothing was connected."
                raise SignInError(msg)
            msg = f"Google sign-in failed ({_code(error)}). Nothing was connected."
            raise SignInError(msg)
        code = params.get("code")
        if not code:
            msg = "Google sign-in came back without a code. Start it again: /login google"
            raise SignInError(msg)
        tokens = await self._exchange(attempt, code)
        claims = self._identity(tokens["id_token"], attempt)
        granted = frozenset(str(tokens.get("scope", "")).split())
        services = granted & SERVICE_SCOPES
        if not services:
            msg = (
                "You signed in, but did not allow Enzo to use Gmail or Calendar, so nothing was "
                "connected. Sign in again and tick the boxes: /login google"
            )
            raise SignInError(msg)
        email = claims.get("email") if isinstance(claims.get("email"), str) else None
        credential = GoogleCredential(
            client_id=attempt.client.client_id,
            subject=claims["sub"],
            email=email,
            access_token=SecretStr(tokens["access_token"]),
            refresh_token=SecretStr(tokens["refresh_token"]),
            scopes=tuple(sorted(granted)),
            expires_at=self._now() + timedelta(seconds=int(tokens["expires_in"])),
        )
        label = email or "your Google account"
        self._store.connect(PROVIDER, credential, label=label, scopes=sorted(services))
        self._on_change()
        asked = set(ACCESS[attempt.access]) & SERVICE_SCOPES
        note = ""
        if asked - services:
            note = f" You did not allow: {describe_access(asked - services)}."
        return f"Connected Google as {label}: {describe_access(services)}.{note}"

    async def _exchange(self, attempt: Attempt, code: str) -> dict[str, Any]:
        response = await self._http.post(
            TOKEN_URL,
            data=_with_secret(
                attempt.client,
                {
                    "grant_type": "authorization_code",
                    "client_id": attempt.client.client_id,
                    "code": code,
                    "code_verifier": attempt.verifier,
                    "redirect_uri": attempt.redirect_uri,
                },
            ),
            headers={"Accept": "application/json"},
        )
        if response.status_code != httpx2.codes.OK:
            error = _oauth_error(response)
            log.warning(
                "google code exchange refused",
                extra={"status": response.status_code, "error": error},
            )
            if error == "invalid_grant":
                msg = "That sign-in expired before Enzo could finish it. Start it again."
                raise SignInError(msg)
            if error in ("invalid_client", "unauthorized_client"):
                msg = (
                    "Google did not accept the OAuth client. Check it is a Desktop app client "
                    "and paste its file again (docs/connectors/google.md)."
                )
                raise SignInError(msg)
            msg = f"Google refused to finish the sign-in ({error or response.status_code})."
            raise SignInError(msg)
        body = _json(response)
        for name in ("access_token", "id_token"):
            if not isinstance(body.get(name), str) or not body[name]:
                msg = f"Google's sign-in reply had no {name.replace('_', ' ')}. Try again."
                raise SignInError(msg)
        if not isinstance(body.get("refresh_token"), str) or not body["refresh_token"]:
            msg = (
                "Google gave Enzo no lasting access (no refresh token), so nothing was connected. "
                "Sign in again: /login google"
            )
            raise SignInError(msg)
        if not isinstance(body.get("expires_in"), int):
            msg = "Google's sign-in reply had no expiry. Try again."
            raise SignInError(msg)
        return body

    def _identity(self, id_token: str, attempt: Attempt) -> dict[str, Any]:
        """Who signed in. The ID token came straight from Google's token endpoint over TLS, so
        TLS vouches for its issuer (OpenID Connect Core 3.1.3.7); its claims are still checked,
        and it is only used to name the account."""
        try:
            claims: dict[str, Any] = jwt.decode(
                id_token,
                options={
                    "verify_signature": False,
                    "verify_aud": True,
                    "verify_iss": True,
                    "verify_exp": True,
                    "require": ["sub", "aud", "iss", "exp"],
                },
                audience=attempt.client.client_id,
                issuer=ISSUERS,
                leeway=30,
            )
        except jwt.PyJWTError as exc:
            log.warning("google id token rejected", extra={"reason": type(exc).__name__})
            msg = "Could not read who signed in to Google, so nothing was connected."
            raise SignInError(msg) from exc
        if not secrets.compare_digest(str(claims.get("nonce", "")), attempt.nonce):
            msg = "The Google sign-in did not match this attempt, so nothing was connected."
            raise SignInError(msg)
        if not isinstance(claims.get("sub"), str) or not claims["sub"]:
            msg = "The Google sign-in named no account, so nothing was connected."
            raise SignInError(msg)
        return claims

    def _pending_for(self, state: str) -> Attempt | None:
        self._forget_stale()
        for attempt in self._attempts.values():
            if attempt.state and secrets.compare_digest(attempt.state, state):
                attempt.state = ""  # single use, from the first moment: a replay finds nothing
                return attempt
        return None

    def _forget_stale(self) -> None:
        cutoff = self._clock() - ATTEMPT_TTL_S
        for login_id in [i for i, a in self._attempts.items() if a.started < cutoff]:
            del self._attempts[login_id]

    # ---------- staying signed in ----------

    def credential(self) -> GoogleCredential | None:
        credential = self._store.credential(PROVIDER)
        return credential if isinstance(credential, GoogleCredential) else None

    def granted(self) -> frozenset[str]:
        """The Gmail and Calendar scopes the user allowed (from the index: no vault read)."""
        connection = self._store.connections().get(PROVIDER)
        if connection is None or connection.method != "google":
            return frozenset()
        return frozenset(connection.scopes or ())

    async def access_token(self, *, renew: bool = False) -> str:
        """A valid access token, renewed shortly before it expires (or now, with ``renew``: a
        status check proves the grant is still alive). One renewal at a time."""
        async with self._renewing:
            credential = self.credential()
            if credential is None:
                raise AuthError(
                    "Google is not connected: /login google", provider=PROVIDER, model=""
                )
            if not renew and credential.expires_at - self._now() > REFRESH_MARGIN:
                return credential.access_token.get_secret_value()
            renewed = await self._renew(credential)
            return renewed.access_token.get_secret_value()

    async def _renew(self, credential: GoogleCredential) -> GoogleCredential:
        ctx = {"provider": PROVIDER, "model": ""}
        found = self.client()
        if found is None or found[0].client_id != credential.client_id:
            self._end()
            raise AuthError(SIGN_IN_AGAIN, **ctx)
        try:
            response = await self._http.post(
                TOKEN_URL,
                data=_with_secret(
                    found[0],
                    {
                        "grant_type": "refresh_token",
                        "client_id": credential.client_id,
                        "refresh_token": credential.refresh_token.get_secret_value(),
                    },
                ),
                headers={"Accept": "application/json"},
            )
        except httpx2.TimeoutException as exc:
            msg = "Google did not answer in time while renewing the sign-in"
            raise ProviderTimeoutError(msg, **ctx) from exc
        except httpx2.HTTPError as exc:
            msg = "Could not reach Google to renew the sign-in"
            raise ProviderConnectionError(msg, **ctx) from exc
        if response.status_code == httpx2.codes.OK:
            body = _json(response)
            if not isinstance(body.get("access_token"), str) or not isinstance(
                body.get("expires_in"), int
            ):
                msg = "Google's renewal reply was incomplete"
                raise ProviderServerError(msg, **ctx)
            update: dict[str, Any] = {
                "access_token": SecretStr(body["access_token"]),
                "expires_at": self._now() + timedelta(seconds=body["expires_in"]),
            }
            if isinstance(body.get("refresh_token"), str):
                update["refresh_token"] = SecretStr(body["refresh_token"])
            if isinstance(body.get("scope"), str):
                update["scopes"] = tuple(sorted(body["scope"].split()))
            renewed = credential.model_copy(update=update)
            self._store.replace(PROVIDER, renewed)
            services = sorted(set(renewed.scopes) & SERVICE_SCOPES)
            if services != sorted(self.granted()):
                self._store.set_scopes(PROVIDER, services)
                self._on_change()  # the user took some access back: tools follow
            return renewed
        error = _oauth_error(response)
        log.warning(
            "google renewal refused", extra={"status": response.status_code, "error": error}
        )
        if error in ("invalid_grant", "invalid_client", "unauthorized_client"):
            self._end()  # over for good: forget the tokens, say how to come back
            raise AuthError(SIGN_IN_AGAIN, **ctx)
        if response.status_code >= httpx2.codes.INTERNAL_SERVER_ERROR:
            msg = f"Google could not renew the sign-in right now ({response.status_code})"
            raise ProviderServerError(msg, **ctx)
        msg = f"Google refused to renew the sign-in ({error or response.status_code})"
        raise AuthError(msg, **ctx)

    def _end(self) -> None:
        self._store.disconnect(PROVIDER)
        self._on_change()

    async def sign_out(self) -> bool:
        """Revoke Enzo's access at Google. True when Google confirmed it. The caller then forgets
        the tokens either way."""
        credential = self.credential()
        if credential is None:
            return True
        try:
            response = await self._http.post(
                REVOKE_URL, data={"token": credential.refresh_token.get_secret_value()}
            )
        except httpx2.HTTPError:
            return False
        return response.status_code == httpx2.codes.OK


def _with_secret(client: GoogleClient, data: dict[str, str]) -> dict[str, str]:
    if client.client_secret is not None:
        return {**data, "client_secret": client.client_secret.get_secret_value()}
    return data


def _json(response: httpx2.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _oauth_error(response: httpx2.Response) -> str | None:
    error = _json(response).get("error")
    return _code(error) if isinstance(error, str) else None


def _code(value: str) -> str:
    """Error codes come from outside: keep only what a code can contain, never free text."""
    return re.sub(r"[^a-z0-9_.-]", "", value.lower())[:64] or "unknown"
