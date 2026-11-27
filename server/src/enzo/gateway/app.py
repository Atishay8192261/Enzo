"""The agent gateway: auth, health, brains, and the route groups (spec §2.3, §3)."""

import asyncio
import logging
import os
import secrets
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from importlib import resources
from typing import Annotated, Literal

import httpx2
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from enzo import __version__
from enzo.accounts.chatgpt import host_id
from enzo.accounts.google import GoogleClient
from enzo.accounts.service import AccountsService
from enzo.agent.prompt import install_note
from enzo.agent.service import ConversationService
from enzo.agent.toolset import core_tools
from enzo.brain.config import BrainsConfig, Tier, load_config
from enzo.brain.factory import build_brain, has_key
from enzo.brain.router import Router
from enzo.browser.session import Browsers
from enzo.browser.tools import browser_tools
from enzo.config import Settings
from enzo.connectors.github import github_tools
from enzo.connectors.google import GoogleConnectors
from enzo.gateway.chatgpt import RedactAuthQuery, chatgpt_routes
from enzo.gateway.connections import build_accounts, connection_routes
from enzo.gateway.conversations import ServiceDep, conversation_routes
from enzo.gateway.goals import goal_routes
from enzo.gateway.google import google_routes
from enzo.gateway.helpers import helper_routes
from enzo.gateway.memory import memory_routes
from enzo.gateway.privacy import privacy_routes
from enzo.gateway.scheduled import scheduled_routes
from enzo.gateway.watches import watch_routes
from enzo.goals import Goals
from enzo.hooks.sources import Sources
from enzo.hooks.store import Hooks
from enzo.hooks.tools import watch_tools
from enzo.hooks.watcher import Watcher
from enzo.memory import Memory
from enzo.paths import data_dir
from enzo.privacy import Discretion, Rules, privacy_tools
from enzo.schedule import Background, Bus, FinishedTasks, Runner, Schedule
from enzo.schedule.notify import Notify, desktop, silent
from enzo.schedule.tasks import TaskRunner
from enzo.semantic import LocalEmbedder, Off, SemanticIndex
from enzo.store.repository import Store
from enzo.subagents import HelperStore, Subagents, subagent_tools
from enzo.tools.registry import ToolRegistry
from enzo.tools.stored import OutputStore, stored_tools

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
    background: Background | None = None,
    helpers: Subagents | None = None,
    on_idle: Callable[[], None] | None = None,
) -> FastAPI:
    """Build the gateway. Tests inject a service; otherwise the real one is built at startup.

    ``on_idle``: called when no reminder is left to deliver (an installed gateway that was kept
    running for reminders may then stop).
    """
    cfg = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if service is not None:
            app.state.service = service
            if background is not None:
                app.state.background = background
            if helpers is not None:
                app.state.helpers = helpers
            yield
            return
        async with _running(app, cfg, accounts, on_idle):
            yield

    app = FastAPI(title="Enzo Gateway", version=__version__, lifespan=lifespan)
    # The web app calls /api/…: in development Vite strips the prefix, here the gateway does.
    app.add_middleware(StripApiPrefix)
    if service is not None:
        app.state.service = service  # available even when the ASGI server skips lifespan
    if background is not None:
        app.state.background = background
    if helpers is not None:
        app.state.helpers = helpers
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
    app.include_router(memory_routes(auth))
    app.include_router(privacy_routes(auth))
    app.include_router(goal_routes(auth))
    app.include_router(scheduled_routes(auth))
    app.include_router(helper_routes(auth))
    app.include_router(watch_routes(auth))
    app.include_router(connection_routes(auth))
    app.include_router(chatgpt_routes(auth, cfg.port))
    app.include_router(google_routes(auth, cfg.port))
    _finish(app, cfg)
    return app


@asynccontextmanager
async def _running(
    app: FastAPI,
    cfg: Settings,
    accounts: AccountsService | None,
    on_idle: Callable[[], None] | None,
) -> AsyncIterator[None]:
    """Everything a real gateway runs: the store, brains, tools, connectors and reminders."""
    store = Store.from_url(cfg.database_url)
    config = load_config(cfg.brains_config)
    # One client for tools and sign-in: no automatic redirects (read_page checks every hop).
    http = httpx2.AsyncClient(timeout=httpx2.Timeout(20.0), follow_redirects=False)
    acct, google, router = _accounts_and_brains(cfg, config, accounts, http)
    app.state.accounts = acct
    outputs, index = _context_stores(store, config, cfg)
    memory = Memory(store.engine, cfg.workspace_dir, index=index)
    # What is sent out is checked for private details; the user's discretion rules (ADR 0029).
    discretion = Discretion(Rules(store.engine), memory)
    schedule = Schedule(store.engine)
    goals = Goals(store.engine, cfg.workspace_dir, schedule)
    notify = desktop if cfg.notify else silent
    runner = Runner(schedule, Bus(), notify=notify, on_idle=on_idle)
    finished = FinishedTasks()
    # Enzo's own browser profile, in its private data folder (ADR 0027).
    browsers = _browsers(cfg)
    tools = ToolRegistry(
        [
            *core_tools(
                workspace=cfg.workspace_dir,
                memory=memory,
                schedule=schedule,
                finished=finished,
                goals=goals,
                http=http,
                tavily_key=lambda: acct.api_key("tavily"),
                on_set=runner.poke,
                index=index,
            ),
            *stored_tools(outputs),
            *browser_tools(browsers),
            *privacy_tools(discretion.rules),
        ],
        retain=outputs,
    )
    watcher = _watcher(store, http, tools, runner, cfg, notify)
    app.state.background = Background(schedule, runner.bus, runner, watcher)
    service = ConversationService(
        store,
        router,
        tools,
        developer=install_note(cfg.workspace_dir),
        memory=memory,
        goals=goals,
        index=index,
        outputs=outputs,
        hooks=[browsers],
        discretion=discretion,
    )
    app.state.service = service
    app.state.helpers = helpers = _helpers(cfg, service, runner.bus, notify)
    # Scheduled tasks run as agent turns: the runner hands each due one to the task runner.
    tasks = TaskRunner(schedule, service, runner.bus, finished, notify=notify)
    runner.connect(tasks.start, lambda: tasks.running > 0 or helpers.busy)
    watcher.connect(service, runner.poke)
    connectors = Connectors(tools, acct, http, google)
    await connectors.refresh("github")
    await connectors.refresh("google")
    acct.on_change(connectors.changed)
    runner.start()  # reminders due while Enzo was not running arrive now, marked late
    index.start()  # fetch the model if needed, then index what is new (the first time: all)
    await helpers.recover()  # helpers that were working pick up where they stopped
    try:
        yield
    finally:
        await helpers.close()
        await index.close()
        await runner.stop()
        await tasks.stop()
        await watcher.stop()
        await browsers.close_all()
        await connectors.close()
        await http.aclose()
        await store.close()


def _accounts_and_brains(
    cfg: Settings,
    config: BrainsConfig,
    accounts: AccountsService | None,
    http: httpx2.AsyncClient,
) -> tuple[AccountsService, GoogleConnectors, Router]:
    """The user's own accounts (sign-ins and keys) and the router over the brains they open."""
    acct = accounts or build_accounts(cfg, config)
    if acct.chatgpt is None:
        acct.enable_chatgpt(host_id(cfg.config_dir), http)
    if acct.google is None:
        acct.enable_google(http, configured=_google_client(cfg))
    google = GoogleConnectors(lambda: acct.google)
    acct.set_google_check(google.check)
    env = os.environ if cfg.use_env_keys else None  # opt-in only (#24)
    router = Router(
        config,
        build=lambda c: build_brain(c, env, keys=acct.auth),
        available=lambda c: acct.usable(c) or has_key(c, env),
    )
    acct.on_change(router.forget)
    return acct, google, router


def _context_stores(
    store: Store, config: BrainsConfig, cfg: Settings
) -> tuple[OutputStore, SemanticIndex]:
    """Long texts kept as files (ADR 0024) and the index that finds things by meaning (ADR 0030)."""
    # Long tool results and pastes are kept whole in Enzo's private data folder.
    ctx = config.context
    outputs = OutputStore(
        data_dir() / "context",
        tool_inline_chars=ctx.tool_inline_chars,
        paste_inline_chars=ctx.paste_inline_chars,
        excerpt_chars=ctx.excerpt_chars,
    )
    # Search by meaning: a model on this computer, fetched once in the background.
    embedder = LocalEmbedder(data_dir() / "models") if cfg.embeddings == "local" else Off()
    return outputs, SemanticIndex(store.engine, embedder, stored=outputs.root)


def _helpers(cfg: Settings, service: ConversationService, bus: Bus, notify: Notify) -> Subagents:
    """Long work handed to helpers, each in its own chat, and the brain's tools for it."""
    helpers = Subagents(
        service,
        HelperStore(service.store.engine),
        bus,
        max_working=cfg.helpers_max,
        steps=cfg.helper_steps,
        notify=notify,
    )
    for t in subagent_tools(helpers):
        service.registry.register(t)
    return helpers


def _browsers(cfg: Settings) -> Browsers:
    """Enzo's own browser profile, in its private data folder (ADR 0027)."""
    browsers = Browsers(
        data_dir() / "browser",
        executable=cfg.browser_path,
        headless=cfg.browser_headless,
        own_port=cfg.port,
    )
    browsers.recover()  # a browser left behind by a gateway that crashed
    return browsers


def _watcher(
    store: Store,
    http: httpx2.AsyncClient,
    tools: ToolRegistry,
    runner: Runner,
    cfg: Settings,
    notify: Notify,
) -> Watcher:
    """Watches check on the runner's clock with no model call; a change wakes a turn (ADR 0031).
    Their checks read through the connected accounts' tools, found by name when they run."""
    sources = Sources(http=http, tools=tools.get, own_port=cfg.port)
    watcher = Watcher(Hooks(store.engine), sources, runner.bus, notify=notify)
    for t in watch_tools(watcher, on_set=runner.poke):
        tools.register(t)
    runner.add(watcher)
    return watcher


def _google_client(cfg: Settings) -> GoogleClient | None:
    """A Google OAuth client from configuration (ADR 0026), when one is set."""
    if not cfg.google_client_id:
        return None
    return GoogleClient(client_id=cfg.google_client_id, client_secret=cfg.google_client_secret)


def _finish(app: FastAPI, cfg: Settings) -> None:
    """Log redaction, the host check, and the built-in web app."""
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, RedactAuthQuery) for f in access.filters):
        access.addFilter(RedactAuthQuery())
    if cfg.env != "test":
        # Only local names reach the gateway: a web page that rebinds its own domain to 127.0.0.1
        # (DNS rebinding) is refused instead of driving Enzo.
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(cfg.allowed_hosts))
    web = resources.files("enzo") / "webapp"
    if (web / "index.html").is_file():
        # An installed Enzo serves its web app itself (#23): routes above win, the rest is the app.
        app.mount("/", StaticFiles(directory=str(web), html=True), name="web")


async def _invalid_request(_request: Request, exc: RequestValidationError) -> JSONResponse:
    """Say which field is wrong and why, never echo what was sent: it may be a secret (§11.2)."""
    errors = [{k: v for k, v in e.items() if k != "input"} for e in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(errors)})


class Connectors:
    """Keeps each connector's tools in step with its account: they appear when it connects and
    go away when it disconnects. A connector that cannot be reached is logged, never fatal."""

    def __init__(
        self,
        registry: ToolRegistry,
        accounts: AccountsService,
        http: httpx2.AsyncClient,
        google: GoogleConnectors | None = None,
    ) -> None:
        self._registry = registry
        self._accounts = accounts
        self._http = http
        self._google = google
        self._tasks: set[asyncio.Task[None]] = set()

    async def refresh(self, name: str) -> None:
        if name == "google" and self._google is not None:
            self._registry.replace_group(name, await self._google.tools())  # never raises
            return
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


class StripApiPrefix:
    """``/api/brains`` is ``/brains``: the web app reaches the gateway the same way in development
    (through Vite) and when the gateway serves it."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] in ("http", "websocket") and (path == "/api" or path.startswith("/api/")):
            scope = {**scope, "path": path[4:] or "/", "raw_path": None}
        await self.app(scope, receive, send)
