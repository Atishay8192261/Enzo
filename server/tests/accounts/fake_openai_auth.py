"""A fake of OpenAI's sign-in server and model list, as its SIWC docs describe them (2026-11-18).

It checks what the real one checks: PKCE, the exact redirect URI and resource, the issued client
id, single-use codes, rotating refresh tokens. ID tokens are real JWTs signed with a key made for
the test run, served through discovery and JWKS like OpenAI's.
"""

import asyncio
import base64
import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx2
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from enzo.accounts.chatgpt import (
    AUTHORIZE_URL,
    DISCOVERY_URL,
    DYNAMIC_CLIENT,
    ISSUER,
    MODELS_URL,
    PLAN_SCOPE,
    RESOURCE,
    TOKEN_URL,
)
from tests.fakes import fake_key

JWKS_URL = f"{ISSUER}/.well-known/jwks.json"
REVOKE_URL = f"{ISSUER}/oauth/revoke"
PLAN_MODELS = ["gpt-6.2-sol", "gpt-6.2-astra"]


@dataclass
class Account:
    subject: str = "user-sub-1"
    email: str | None = "ada@example.com"
    #: Whether the user allows Enzo to use their ChatGPT plan on the consent screen.
    allows_plan: bool = True


@dataclass
class _Code:
    client_id: str
    challenge: str
    redirect_uri: str
    nonce: str
    account: Account
    scope: str


@dataclass
class FakeOpenAIAuth:
    key: rsa.RSAPrivateKey = field(
        default_factory=lambda: rsa.generate_private_key(public_exponent=65537, key_size=2048)
    )
    kid: str = "test-key-1"
    expires_in: int = 3600
    #: Force the next token-endpoint reply, e.g. (400, {"error": "invalid_grant"}).
    token_reply: tuple[int, dict[str, Any]] | None = None
    revoke_status: int = 200
    #: Change one claim of the next ID token (to prove each check rejects it).
    id_token_override: dict[str, Any] = field(default_factory=dict)
    sign_with: rsa.RSAPrivateKey | None = None
    algorithm: str = "RS256"
    requests: list[httpx2.Request] = field(default_factory=list)
    codes: dict[str, _Code] = field(default_factory=dict)
    refresh_tokens: dict[str, tuple[str, Account]] = field(default_factory=dict)
    access_tokens: set[str] = field(default_factory=set)
    revoked: list[str] = field(default_factory=list)
    issued: int = 0
    registrations: int = 0

    # ---------- the browser part: the user signs in on OpenAI's page ----------

    def browser(self, authorize_url: str, account: Account | None = None) -> dict[str, str]:
        """Follow the authorize URL as the user would; returns the callback's query."""
        account = account or Account()
        parts = urlsplit(authorize_url)
        assert f"{parts.scheme}://{parts.netloc}{parts.path}" == AUTHORIZE_URL
        q = {k: v[0] for k, v in parse_qs(parts.query, strict_parsing=True).items()}
        assert q["response_type"] == "code"
        assert q["resource"] == RESOURCE
        assert q["code_challenge_method"] == "S256"
        assert q["ext_agent_host_id"].startswith("urn:uuid:")
        assert q["redirect_uri"].startswith("http://127.0.0.1:")
        assert q["redirect_uri"].endswith("/auth/callback")
        assert set(q["scope"].split()) >= {"openid", "offline_access", PLAN_SCOPE}
        new = q["client_id"] == DYNAMIC_CLIENT
        assert ("agent_name_hint" in q) is new  # the app's name only on first registration
        if new:
            self.registrations += 1
            client_id = f"oaiapp_{account.subject}"
        else:
            client_id = q["client_id"]
        scope = q["scope"] if account.allows_plan else "openid profile email offline_access"
        code = fake_key(f"code-{len(self.codes)}")
        self.codes[code] = _Code(
            client_id, q["code_challenge"], q["redirect_uri"], q["nonce"], account, scope
        )
        back = {"code": code, "state": q["state"], "scope": scope}
        if new:
            back["client_id"] = client_id
        return back

    # ---------- the server part ----------

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        routes = {
            DISCOVERY_URL: self._discovery,
            JWKS_URL: self._jwks,
            TOKEN_URL: self._token,
            REVOKE_URL: self._revoke,
            MODELS_URL: self._models,
        }
        route = routes.get(str(request.url).split("?")[0])
        return route(request) if route else httpx2.Response(404, json={"detail": "not found"})

    def _discovery(self, _request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200, json={"issuer": ISSUER, "jwks_uri": JWKS_URL, "revocation_endpoint": REVOKE_URL}
        )

    def _jwks(self, _request: httpx2.Request) -> httpx2.Response:
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        return httpx2.Response(200, json={"keys": [{**jwk, "kid": self.kid, "use": "sig"}]})

    def _revoke(self, request: httpx2.Request) -> httpx2.Response:
        form = _form(request)
        self.revoked.append(form["token"])
        self.refresh_tokens.pop(form["token"], None)
        return httpx2.Response(self.revoke_status)

    def _models(self, request: httpx2.Request) -> httpx2.Response:
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if token not in self.access_tokens:
            return httpx2.Response(401, json={"detail": "Unauthorized"})
        models = [{"slug": m, "display_name": m, "visibility": "list"} for m in PLAN_MODELS]
        return httpx2.Response(200, json={"models": models})

    def _token(self, request: httpx2.Request) -> httpx2.Response:
        if self.token_reply is not None:
            status, body = self.token_reply
            self.token_reply = None
            return httpx2.Response(status, json=body)
        form = _form(request)
        assert form["resource"] == RESOURCE
        assert form["client_id"] != DYNAMIC_CLIENT  # never used for a token exchange
        grants = {"authorization_code": self._code_grant, "refresh_token": self._refresh_grant}
        grant = grants.get(form["grant_type"])
        if grant is None:
            return httpx2.Response(400, json={"error": "unsupported_grant_type"})
        return grant(form)

    def _code_grant(self, form: dict[str, str]) -> httpx2.Response:
        code = self.codes.pop(form["code"], None)  # single use
        if code is None:
            return httpx2.Response(400, json={"error": "invalid_grant"})
        verifier = form["code_verifier"].encode()
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier).digest()).rstrip(b"=")
        ok = (
            challenge.decode() == code.challenge
            and form["redirect_uri"] == code.redirect_uri
            and form["client_id"] == code.client_id
        )
        if not ok:
            return httpx2.Response(400, json={"error": "invalid_grant"})
        return httpx2.Response(
            200, json=self._tokens(code.client_id, code.account, code.scope, code.nonce)
        )

    def _refresh_grant(self, form: dict[str, str]) -> httpx2.Response:
        known = self.refresh_tokens.pop(form["refresh_token"], None)  # rotates
        if known is None or known[0] != form["client_id"]:
            return httpx2.Response(400, json={"error": "invalid_grant"})
        client_id, account = known
        body = self._tokens(client_id, account, f"openid email offline_access {PLAN_SCOPE}")
        del body["id_token"]
        return httpx2.Response(200, json=body)

    def _tokens(
        self, client_id: str, account: Account, scope: str, nonce: str | None = None
    ) -> dict[str, Any]:
        self.issued += 1
        access, refresh = fake_key(f"access-{self.issued}"), fake_key(f"refresh-{self.issued}")
        self.access_tokens.add(access)
        self.refresh_tokens[refresh] = (client_id, account)
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": ISSUER,
            "sub": account.subject,
            "aud": [client_id],
            "iat": now,
            "exp": now + 3600,
            "email": account.email,
            "email_verified": True,
            "nonce": nonce,
            **self.id_token_override,
        }
        self.id_token_override = {}
        key: Any = self.sign_with or self.key
        if self.algorithm == "HS256":
            key = "a-shared-secret-that-must-never-be-accepted"
        id_token = jwt.encode(claims, key, algorithm=self.algorithm, headers={"kid": self.kid})
        return {
            "access_token": access,
            "refresh_token": refresh,
            "id_token": id_token,
            "token_type": "Bearer",
            "expires_in": self.expires_in,
            "scope": scope,
            "earliest_refresh_at": now + 60,
        }

    def calls_to(self, url: str) -> list[httpx2.Request]:
        return [r for r in self.requests if str(r.url).split("?")[0] == url]


def _form(request: httpx2.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


def client_for(fake: Callable[[httpx2.Request], httpx2.Response]) -> httpx2.AsyncClient:
    """Each reply takes a moment, like a real network: concurrent callers really overlap."""

    async def slow(request: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(0.01)
        return fake(request)

    return httpx2.AsyncClient(transport=httpx2.MockTransport(slow))
