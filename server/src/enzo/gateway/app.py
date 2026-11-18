"""The agent gateway: auth, health, brains, and the route groups (spec §2.3, §3)."""

import asyncio
import logging
import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Literal

import httpx2
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from enzo import __version__
from enzo.accounts.service import AccountsService
from enzo.agent.service import ConversationService
from enzo.brain.config import Tier, load_config
from enzo.brain.factory import build_brain, has_key
from enzo.brain.router import Router
from enzo.config import Settings
from enzo.connectors.github import github_tools
from enzo.gateway.connections import build_accounts, connection_routes
from enzo.gateway.conversations import ServiceDep, conversation_routes
from enzo.store.repository import Store
from enzo.tools.files import file_tools
from enzo.tools.registry import ToolRegistry
from enzo.tools.web import web_tools

log = logging.getLogger("enzo.gateway")


class Health(BaseModel):
    """Liveness payload. Clients use it to confirm they reached a compatible gateway."""

    status: Literal["ok"]
    version: str
    env: str


class BrainOut(BaseModel):
    name: str
    provider: str
    model: str
    priced: bool
    tier: Tier
    recommended: bool
    note: str | None
    #: The provider is connected (or has an environment key), so this brain can answer now.
    available: bool


def create_app(
    settings: Settings | None = None,
    *,
    service: ConversationService | None = None,
    accounts: AccountsService | None = None,
) -> FastAPI:
    """Build the gateway. Tests inject a service; otherwise the real one is built at startup."""
    cfg = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if service is not None:
            app.state.service = service
            yield
            return
        store = Store.from_url(cfg.database_url)
        config = load_config(cfg.brains_config)
        acct = accounts or build_accounts(cfg, config)
        env = os.environ if cfg.use_env_keys else None  # opt-in only (#24)
        router = Router(
            config,
            build=lambda c: build_brain(c, env, keys=acct.api_key),
            available=lambda c: acct.connected(c.provider_name) or has_key(c, env),
        )
        acct.on_change(router.forget)
        app.state.accounts = acct
        # One client for the web tools: no automatic redirects (read_page checks every hop).
        http = httpx2.AsyncClient(timeout=httpx2.Timeout(20.0), follow_redirects=False)
        tools = ToolRegistry(
            [
                *web_tools(tavily_key=lambda: acct.api_key("tavily"), http=http),
                *file_tools(cfg.workspace_dir),
            ]
        )
        app.state.service = ConversationService(store, router, tools)
        connectors = Connectors(tools, acct, http)
        await connectors.refresh("github")
        acct.on_change(connectors.changed)
        try:
            yield
        finally:
            await connectors.close()
            await http.aclose()
            await store.close()

    app = FastAPI(title="Enzo Gateway", version=__version__, lifespan=lifespan)
    if service is not None:
        app.state.service = service  # available even when the ASGI server skips lifespan
    if accounts is not None:
        app.state.accounts = accounts
    app.exception_handler(RequestValidationError)(_invalid_request)

    def authorized(authorization: Annotated[str | None, Header()] = None) -> None:
        if cfg.api_token is None:
            return
        expected = f"Bearer {cfg.api_token.get_secret_value()}"
        if authorization is None or not secrets.compare_digest(authorization, expected):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing or wrong bearer token")

    auth = Depends(authorized)

    @app.get("/healthz")
    def healthz() -> Health:
        return Health(status="ok", version=__version__, env=cfg.env)

    @app.get("/brains", dependencies=[auth])
    def brains(s: ServiceDep) -> list[BrainOut]:
        router = s.router
        return [
            BrainOut(
                name=n,
                provider=b.provider_name,
                model=b.model,
                priced=b.pricing is not None,
                tier=b.tier,
                recommended=b.recommended,
                note=b.note,
                available=router.available(n),
            )
            for n, b in router.config.brains.items()
        ]

    app.include_router(conversation_routes(auth))
    app.include_router(connection_routes(auth))
    return app


async def _invalid_request(_request: Request, exc: RequestValidationError) -> JSONResponse:
    """Say which field is wrong and why, never echo what was sent: it may be a secret (§11.2)."""
    errors = [{k: v for k, v in e.items() if k != "input"} for e in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(errors)})


class Connectors:
    """Keeps each connector's tools in step with its account: they appear when it connects and
    go away when it disconnects. A connector that cannot be reached is logged, never fatal."""

    def __init__(
        self, registry: ToolRegistry, accounts: AccountsService, http: httpx2.AsyncClient
    ) -> None:
        self._registry = registry
        self._accounts = accounts
        self._http = http
        self._tasks: set[asyncio.Task[None]] = set()

    async def refresh(self, name: str) -> None:
        if name != "github":
            return
        try:
            loaded = await github_tools(lambda: self._accounts.api_key("github"), self._http)
        except Exception as exc:  # unreachable or rejected: keep running without these tools
            log.warning("connector unavailable", extra={"connector": name, "error": str(exc)})
            loaded = []
        self._registry.replace_group(name, loaded)

    def changed(self, name: str) -> None:
        task = asyncio.get_running_loop().create_task(self.refresh(name))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
