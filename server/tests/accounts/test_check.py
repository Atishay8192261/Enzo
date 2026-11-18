"""Key checks go through the real SDKs; only the network is replaced."""

from collections.abc import Callable

import httpx2
import pytest

from enzo.accounts.check import check_api_key, check_tavily_key
from enzo.brain import (
    AuthError,
    ProviderConnectionError,
    ProviderServerError,
    RateLimitError,
)
from enzo.brain.config import BrainConfig, load_config
from tests.fakes import fake_key

CFG = load_config()
KEY = fake_key("check")

type Handler = Callable[[httpx2.Request], httpx2.Response]


class Recorder:
    def __init__(self, status: int, body: object) -> None:
        self.status, self.body = status, body
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return httpx2.Response(self.status, json=self.body)


async def check(cfg: BrainConfig, handler: Handler) -> None:
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http:
        await check_api_key(cfg, KEY, http_client=http)


ANTHROPIC_OK = {"data": [], "has_more": False, "first_id": None, "last_id": None}
ANTHROPIC_401 = {"type": "error", "error": {"type": "authentication_error", "message": "bad"}}
OPENAI_401 = {"error": {"message": "Incorrect API key", "type": "invalid_request_error"}}


async def test_anthropic_lists_one_model_with_the_key() -> None:
    rec = Recorder(200, ANTHROPIC_OK)
    await check(CFG.brains["claude-opus"], rec)
    (req,) = rec.requests
    assert (req.method, req.url.path, req.url.params["limit"]) == ("GET", "/v1/models", "1")
    assert req.headers["authorization"] == f"Bearer {KEY}"  # SDK 2.9 sends keys as bearer


async def test_anthropic_rejection_is_an_auth_error_after_one_attempt() -> None:
    rec = Recorder(401, ANTHROPIC_401)
    with pytest.raises(AuthError):
        await check(CFG.brains["claude-opus"], rec)
    assert len(rec.requests) == 1


@pytest.mark.parametrize(
    ("brain", "url"),
    [
        ("grok", "https://api.x.ai/v1/models"),
        ("kimi", "https://api.moonshot.ai/v1/models"),
    ],
)
async def test_openai_compatible_providers_list_models_at_their_own_endpoint(
    brain: str, url: str
) -> None:
    rec = Recorder(200, {"object": "list", "data": []})
    await check(CFG.brains[brain], rec)
    (req,) = rec.requests
    assert str(req.url) == url
    assert req.headers["authorization"] == f"Bearer {KEY}"


async def test_openai_rejection_is_an_auth_error_after_one_attempt() -> None:
    rec = Recorder(401, OPENAI_401)
    with pytest.raises(AuthError):
        await check(CFG.brains["grok"], rec)
    assert len(rec.requests) == 1


@pytest.mark.parametrize("brain", ["claude-opus", "grok"])
async def test_server_errors_are_not_retried(brain: str) -> None:
    rec = Recorder(503, {"error": {"type": "overloaded_error", "message": "overloaded"}})
    with pytest.raises(ProviderServerError):
        await check(CFG.brains[brain], rec)
    assert len(rec.requests) == 1


async def test_unreachable_provider_is_a_connection_error() -> None:
    def down(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused", request=request)

    with pytest.raises(ProviderConnectionError):
        await check(CFG.brains["claude-opus"], down)


@pytest.mark.parametrize(
    ("status", "error"),
    [(200, None), (401, AuthError), (429, RateLimitError), (500, ProviderServerError)],
)
async def test_tavily_key_check_reads_usage_without_spending_credits(
    status: int, error: type[Exception] | None
) -> None:
    rec = Recorder(status, {"key": {"usage": 0}})
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(rec)) as http:
        if error is None:
            await check_tavily_key(KEY, http_client=http)
        else:
            with pytest.raises(error):
                await check_tavily_key(KEY, http_client=http)
    (req,) = rec.requests
    assert (req.method, str(req.url)) == ("GET", "https://api.tavily.com/usage")
    assert req.headers["authorization"] == f"Bearer {KEY}"
