"""Sign in with ChatGPT: the user's ChatGPT plan pays for GPT replies, no API key (ADR 0013).

OpenAI's documented flow for open-source, locally hosted apps ("ChatGPT plan usage",
developers.openai.com/siwc, read 2026-11-18):

1. A stable, opaque host id (``urn:uuid:…``) per installation, chosen before the first sign-in.
2. Authorization code + PKCE + OpenID Connect in the system browser. The callback is the gateway
   itself, ``http://127.0.0.1:<port>/auth/callback``. The first sign-in registers a client
   (``client_id=dynamic_agent_client``) and the callback returns the issued client id, which every
   later sign-in reuses.
3. The code is exchanged, the ID token verified (signature against OpenAI's JWKS, issuer,
   audience, expiry, nonce), and the granted scopes must include ``chatgpt.tokens.use.direct``.
4. Tokens live in the vault. Access tokens last an hour and are renewed shortly before expiry, one
   renewal at a time, because refresh tokens rotate.
5. Replies use the public Responses API with the access token (``OpenAIBrain`` in plan mode).
"""

import asyncio
import base64
import fcntl
import hashlib
import logging
import os
import re
import secrets
import time
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, urlencode

import httpx2
import jwt
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError

from enzo.accounts.store import ChatGPTCredential, CredentialStore
from enzo.accounts.vault import write_private
from enzo.brain.errors import (
    AuthError,
    ProviderConnectionError,
    ProviderServerError,
    ProviderTimeoutError,
)

log = logging.getLogger("enzo.accounts.chatgpt")

#: The account a ChatGPT sign-in connects: GPT models, paid by the plan instead of a key.
PROVIDER = "openai"
ISSUER = "https://auth.openai.com"
AUTHORIZE_URL = f"{ISSUER}/api/accounts/authorize"
TOKEN_URL = f"{ISSUER}/api/accounts/oauth/token"
DISCOVERY_URL = f"{ISSUER}/.well-known/openid-configuration"
RESOURCE = "https://api.openai.com/v1"
MODELS_URL = f"{RESOURCE}/models"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
SCOPES = ("openid", "profile", "email", "offline_access", "resource.invoke", PLAN_SCOPE)
DYNAMIC_CLIENT = "dynamic_agent_client"
AGENT_NAME = "Enzo"
CALLBACK_PATH = "/auth/callback"
USAGE_URL = "https://chatgpt.com/settings/usage"
#: An unfinished sign-in is forgotten after this long.
ATTEMPT_TTL_S = 10 * 60
#: Renew this long before the access token expires, so no request carries a dying token.
REFRESH_MARGIN = timedelta(minutes=2)
#: Refresh errors that mean the sign-in is over: sign in again (OpenAI's list).
UNUSABLE_REFRESH = frozenset(
    {
        "invalid_grant",
        "invalid_refresh_token",
        "token_expired",
        "refresh_token_expired",
        "refresh_token_invalidated",
        "refresh_token_reused",
    }
)
#: Asymmetric algorithms only: an ID token is never trusted on a shared secret.
_ALGORITHMS = frozenset({"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384"})
_RECORD = "chatgpt-registration"

SIGN_IN_AGAIN = "Your ChatGPT sign-in has ended. Sign in again: /login chatgpt"


class SignInError(Exception):
    """A sign-in attempt failed. The message is shown to the user as is."""


class Registration(BaseModel):
    """Kept across sign-outs, so a later sign-in reuses the same client (OpenAI's guidance)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    client_id: str
    subject: str
    email: str | None = None


@dataclass
class Attempt:
    """One sign-in in progress: secrets that only this gateway and the browser round trip see."""

    id: str
    state: str
    nonce: str
    verifier: str
    redirect_uri: str
    #: The issued client id for a returning account, else ``dynamic_agent_client``.
    client_id: str
    #: The account a returning sign-in must come back as; None registers a new one.
    registration: Registration | None
    started: float
    status: Literal["pending", "connected", "failed"] = "pending"
    message: str | None = None


def host_id(config_dir: Path) -> str:
    """This installation's ``ext_agent_host_id``: created once, then reused for every sign-in."""
    path = config_dir / "host-id"
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        value = ""
    if not value.startswith("urn:uuid:"):
        value = f"urn:uuid:{uuid.uuid4()}"
        write_private(path, value + "\n")
    return value


def challenge(verifier: str) -> str:
    """PKCE S256: base64url of the SHA-256 of the verifier, without padding."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class ChatGPTSignIn:
    """Signs in, keeps the tokens fresh, and signs out, for one ChatGPT account at a time."""

    def __init__(
        self,
        store: CredentialStore,
        host: str,
        http: httpx2.AsyncClient,
        *,
        on_change: Callable[[], None],
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._host = host
        self._http = http
        self._on_change = on_change
        self._clock = clock
        self._now = now
        self._attempts: dict[str, Attempt] = {}
        self._discovery: dict[str, Any] | None = None
        self._jwks: jwt.PyJWKSet | None = None
        self._renewing = asyncio.Lock()

    # ---------- signing in ----------

    def start(self, redirect_uri: str, *, new_account: bool = False) -> Attempt:
        """Begin a sign-in. A returning account reuses its client; ``new_account`` registers one."""
        self._forget_stale()
        registration = None if new_account else self.registration()
        attempt = Attempt(
            id=secrets.token_urlsafe(16),
            state=secrets.token_urlsafe(32),
            nonce=secrets.token_urlsafe(32),
            verifier=secrets.token_urlsafe(64),
            redirect_uri=redirect_uri,
            client_id=registration.client_id if registration else DYNAMIC_CLIENT,
            registration=registration,
            started=self._clock(),
        )
        self._attempts[attempt.id] = attempt
        return attempt

    def attempt(self, login_id: str) -> Attempt | None:
        self._forget_stale()
        return self._attempts.get(login_id)

    def authorize_url(self, attempt: Attempt) -> str:
        """OpenAI's sign-in page for this attempt. Carries no token, only one-time values."""
        params = {
            "response_type": "code",
            "client_id": attempt.client_id,
            "redirect_uri": attempt.redirect_uri,
            "scope": " ".join(SCOPES),
            "resource": RESOURCE,
            "state": attempt.state,
            "nonce": attempt.nonce,
            "code_challenge": challenge(attempt.verifier),
            "code_challenge_method": "S256",
            "ext_agent_host_id": self._host,
        }
        if attempt.registration is None:
            params["agent_name_hint"] = AGENT_NAME  # first registration only
        elif attempt.registration.email:
            params["login_hint"] = attempt.registration.email
        return f"{AUTHORIZE_URL}?{urlencode(params, quote_via=quote)}"

    async def finish(self, params: Mapping[str, str]) -> Attempt | None:
        """The browser came back. Returns the attempt with its outcome, or None for an unknown
        state (nothing is touched then). Each state is accepted once."""
        attempt = self._pending_for(params.get("state", ""))
        if attempt is None:
            return None
        try:
            label = await self._complete(attempt, params)
        except SignInError as exc:
            attempt.status, attempt.message = "failed", str(exc)
        except httpx2.HTTPError as exc:
            log.warning("chatgpt sign-in unreachable", extra={"error": type(exc).__name__})
            attempt.status = "failed"
            attempt.message = "Could not reach OpenAI to finish signing in. Check the connection."
        else:
            attempt.status, attempt.message = "connected", f"Signed in with ChatGPT as {label}."
        return attempt

    async def _complete(self, attempt: Attempt, params: Mapping[str, str]) -> str:
        if (error := params.get("error")) is not None:
            if error == "access_denied":
                msg = "The ChatGPT sign-in was cancelled, so nothing was connected."
                raise SignInError(msg)
            msg = f"ChatGPT sign-in failed ({_code(error)}). Nothing was connected."
            raise SignInError(msg)
        code = params.get("code")
        if not code:
            msg = "ChatGPT sign-in came back without a code. Start it again."
            raise SignInError(msg)
        client_id = self._client_for(attempt, params.get("client_id"))
        tokens = await self._exchange(attempt, client_id, code)
        claims = await self.verify_id_token(tokens["id_token"], client_id, attempt.nonce)
        subject: str = claims["sub"]
        if attempt.registration is not None and subject != attempt.registration.subject:
            msg = (
                "That is a different ChatGPT account from the one Enzo knows, so nothing changed. "
                "To use it instead, run /login chatgpt new."
            )
            raise SignInError(msg)
        email = claims.get("email") if isinstance(claims.get("email"), str) else None
        # The client exists now, whatever was granted: later sign-ins reuse it.
        registration = Registration(client_id=client_id, subject=subject, email=email)
        self._store.keep_record(_RECORD, registration.model_dump_json())
        scopes = tuple(sorted(str(tokens.get("scope", "")).split()))
        if PLAN_SCOPE not in scopes:
            msg = (
                "You signed in, but Enzo was not allowed to use your ChatGPT plan, so nothing was "
                "connected. Sign in again and allow it, or use an API key: /login openai."
            )
            raise SignInError(msg)
        credential = ChatGPTCredential(
            client_id=client_id,
            subject=subject,
            email=email,
            id_token=SecretStr(tokens["id_token"]),
            access_token=SecretStr(tokens["access_token"]),
            refresh_token=SecretStr(tokens["refresh_token"]),
            scopes=scopes,
            expires_at=self._now() + timedelta(seconds=int(tokens["expires_in"])),
        )
        models = await self.plan_models(tokens["access_token"])
        label = email or "your ChatGPT account"
        self._store.connect(PROVIDER, credential, label=label, models=models)
        self._on_change()
        return label

    def _client_for(self, attempt: Attempt, returned: str | None) -> str:
        if attempt.registration is None:
            if not returned or returned == DYNAMIC_CLIENT:
                msg = "ChatGPT did not finish registering Enzo. Start the sign-in again."
                raise SignInError(msg)
            return returned
        if returned and returned != attempt.client_id:
            msg = "ChatGPT answered for a different app registration, so nothing changed."
            raise SignInError(msg)
        return attempt.client_id

    async def _exchange(self, attempt: Attempt, client_id: str, code: str) -> dict[str, Any]:
        response = await self._http.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": code,
                "code_verifier": attempt.verifier,
                "redirect_uri": attempt.redirect_uri,
                "resource": RESOURCE,
            },
            headers={"Accept": "application/json"},
        )
        if response.status_code != httpx2.codes.OK:
            error = _oauth_error(response)
            _log_refusal("code exchange", response, error)
            if error == "invalid_grant":
                msg = "That sign-in expired before Enzo could finish it. Start it again."
                raise SignInError(msg)
            msg = f"OpenAI refused to finish the sign-in ({error or response.status_code})."
            raise SignInError(msg)
        body = _json(response)
        for name in ("access_token", "refresh_token", "id_token"):
            if not isinstance(body.get(name), str) or not body[name]:
                msg = f"OpenAI's sign-in reply had no {name.replace('_', ' ')}. Try again."
                raise SignInError(msg)
        if not isinstance(body.get("expires_in"), int):
            msg = "OpenAI's sign-in reply had no expiry. Try again."
            raise SignInError(msg)
        return body

    async def verify_id_token(self, token: str, client_id: str, nonce: str) -> dict[str, Any]:
        """Signature against OpenAI's published keys, then issuer, audience, expiry and nonce."""
        try:
            header = jwt.get_unverified_header(token)
            algorithm = header.get("alg")
            if algorithm not in _ALGORITHMS:
                msg = f"unexpected signing algorithm {algorithm!r}"
                raise jwt.InvalidAlgorithmError(msg)
            key = await self._signing_key(header.get("kid"))
            claims: dict[str, Any] = jwt.decode(
                token,
                key=key.key,
                algorithms=[algorithm],
                audience=client_id,
                issuer=ISSUER,
                leeway=5,
                options={"require": ["sub", "exp", "iat", "aud", "iss"]},
            )
        except jwt.PyJWTError as exc:
            log.warning("chatgpt id token rejected", extra={"reason": type(exc).__name__})
            msg = "Could not verify the ChatGPT sign-in, so nothing was connected."
            raise SignInError(msg) from exc
        if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
            msg = "The ChatGPT sign-in did not match this attempt, so nothing was connected."
            raise SignInError(msg)
        if not isinstance(claims.get("sub"), str) or not claims["sub"]:
            msg = "The ChatGPT sign-in named no account, so nothing was connected."
            raise SignInError(msg)
        return claims

    async def _signing_key(self, kid: object) -> jwt.PyJWK:
        # Keys rotate: an unfamiliar kid fetches the set once more before giving up.
        for fresh in (False, True):
            if self._jwks is None or fresh:
                discovery = await self._discover()
                response = await self._http.get(str(discovery["jwks_uri"]))
                response.raise_for_status()
                self._jwks = jwt.PyJWKSet.from_dict(_json(response))
            for key in self._jwks.keys:
                if key.key_id == kid:
                    return key
        msg = "OpenAI's signing key for this sign-in was not found"
        raise jwt.InvalidKeyError(msg)

    async def _discover(self) -> dict[str, Any]:
        if self._discovery is None:
            response = await self._http.get(DISCOVERY_URL)
            response.raise_for_status()
            discovery = _json(response)
            if discovery.get("issuer") != ISSUER or not discovery.get("jwks_uri"):
                msg = "OpenAI's sign-in configuration looked wrong, so nothing was connected."
                raise SignInError(msg)
            self._discovery = discovery
        return self._discovery

    async def plan_models(self, access_token: str) -> list[str] | None:
        """The models this ChatGPT account may use, or None when the list can't be read."""
        try:
            response = await self._http.get(
                MODELS_URL, headers={"Authorization": f"Bearer {access_token}"}
            )
        except httpx2.HTTPError:
            return None
        if response.status_code != httpx2.codes.OK:
            _log_refusal("model list", response, _oauth_error(response))
            return None
        models = _json(response).get("models")
        if not isinstance(models, list):
            return None
        return [m["slug"] for m in models if isinstance(m, dict) and isinstance(m.get("slug"), str)]

    def registration(self) -> Registration | None:
        raw = self._store.record(_RECORD)
        if raw is None:
            return None
        try:
            return Registration.model_validate_json(raw)
        except ValidationError:
            return None

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

    def credential(self) -> ChatGPTCredential | None:
        credential = self._store.credential(PROVIDER)
        return credential if isinstance(credential, ChatGPTCredential) else None

    async def access_token(self) -> str:
        """A valid access token, renewed shortly before it expires. One renewal at a time, also
        across processes (the gateway and `enzo eval` share the vault): refresh tokens rotate,
        so a second renewal with the same one would end the sign-in."""
        async with self._renewing:
            credential = self._signed_in()
            if self._fresh(credential):
                return credential.access_token.get_secret_value()
            async with held(self._store.lock_path("chatgpt-renewal")):
                credential = self._signed_in()  # another process may have renewed it meanwhile
                if self._fresh(credential):
                    return credential.access_token.get_secret_value()
                renewed = await self._renew(credential)
                return renewed.access_token.get_secret_value()

    def _signed_in(self) -> ChatGPTCredential:
        credential = self.credential()
        if credential is None:
            raise AuthError(SIGN_IN_AGAIN, provider=PROVIDER, model="")
        return credential

    def _fresh(self, credential: ChatGPTCredential) -> bool:
        return credential.expires_at - self._now() > REFRESH_MARGIN

    async def _renew(self, credential: ChatGPTCredential) -> ChatGPTCredential:
        ctx = {"provider": PROVIDER, "model": ""}
        try:
            response = await self._http.post(
                TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "client_id": credential.client_id,
                    "refresh_token": credential.refresh_token.get_secret_value(),
                    "resource": RESOURCE,
                },
                headers={"Accept": "application/json"},
            )
        except httpx2.TimeoutException as exc:
            msg = "OpenAI did not answer in time while renewing the ChatGPT sign-in"
            raise ProviderTimeoutError(msg, **ctx) from exc
        except httpx2.HTTPError as exc:
            msg = "Could not reach OpenAI to renew the ChatGPT sign-in"
            raise ProviderConnectionError(msg, **ctx) from exc
        if response.status_code == httpx2.codes.OK:
            body = _json(response)
            if not isinstance(body.get("access_token"), str) or not isinstance(
                body.get("expires_in"), int
            ):
                msg = "OpenAI's renewal reply was incomplete"
                raise ProviderServerError(msg, **ctx)
            update: dict[str, Any] = {
                "access_token": SecretStr(body["access_token"]),
                "expires_at": self._now() + timedelta(seconds=body["expires_in"]),
            }
            if isinstance(body.get("refresh_token"), str):  # it rotates: keep the replacement
                update["refresh_token"] = SecretStr(body["refresh_token"])
            if isinstance(body.get("id_token"), str):
                update["id_token"] = SecretStr(body["id_token"])
            if isinstance(body.get("scope"), str):
                update["scopes"] = tuple(sorted(body["scope"].split()))
            renewed = credential.model_copy(update=update)
            self._store.replace(PROVIDER, renewed)
            return renewed
        error = _oauth_error(response)
        _log_refusal("renewal", response, error)
        if error in UNUSABLE_REFRESH or response.status_code == httpx2.codes.UNAUTHORIZED:
            # Over for good: forget the tokens (the registration stays), say how to come back.
            self._store.disconnect(PROVIDER)
            self._on_change()
            raise AuthError(SIGN_IN_AGAIN, **ctx)
        if response.status_code >= httpx2.codes.INTERNAL_SERVER_ERROR:
            msg = f"OpenAI could not renew the ChatGPT sign-in right now ({response.status_code})"
            raise ProviderServerError(msg, **ctx)
        msg = f"OpenAI refused to renew the ChatGPT sign-in ({error or response.status_code})"
        raise AuthError(msg, **ctx)

    async def sign_out(self) -> bool:
        """End the renewable session with OpenAI. True when OpenAI confirmed it. The caller then
        forgets the tokens either way; the registration stays for a later sign-in."""
        credential = self.credential()
        if credential is None:
            return True
        try:
            discovery = await self._discover()
            endpoint = discovery.get("revocation_endpoint")
            if not isinstance(endpoint, str):
                return False
            response = await self._http.post(
                endpoint,
                data={
                    "token": credential.refresh_token.get_secret_value(),
                    "token_type_hint": "refresh_token",
                    "client_id": credential.client_id,
                },
            )
        except (httpx2.HTTPError, SignInError):
            return False
        return response.status_code == httpx2.codes.OK


def _json(response: httpx2.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _oauth_error(response: httpx2.Response) -> str | None:
    """The OAuth or Responses error code in a refusal, when it has one."""
    body = _json(response)
    error = body.get("error")
    if isinstance(error, dict):
        error = error.get("code")
    return _code(error) if isinstance(error, str) else None


def _code(value: str) -> str:
    """Error codes come from outside: keep only what a code can contain, never free text."""
    return re.sub(r"[^a-z0-9_.-]", "", value.lower())[:64] or "unknown"


def _log_refusal(step: str, response: httpx2.Response, error: str | None) -> None:
    request_id = response.headers.get("openai-request-id") or response.headers.get("x-request-id")
    log.warning(
        "chatgpt %s refused",
        step,
        extra={"status": response.status_code, "error": error, "request_id": request_id},
    )


#: How long a renewal waits for another process's renewal to finish.
LOCK_WAIT_S = 30.0


@asynccontextmanager
async def held(path: Path, wait_s: float = LOCK_WAIT_S) -> AsyncIterator[None]:
    """An exclusive lock on ``path`` shared by every process (``flock``), waited for without
    blocking the event loop. It is released when the holder exits, even if it crashes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + wait_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    msg = (
                        "Another Enzo process is renewing the ChatGPT sign-in and has not finished"
                    )
                    raise ProviderTimeoutError(msg, provider=PROVIDER, model="") from None
                await asyncio.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
