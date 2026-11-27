"""MCP servers as Enzo tools (spec §7.2.3 connectors, ADR 0012).

An MCP server lists tools with a JSON Schema each. Every one becomes an Enzo tool and goes through
the same path as Enzo's own tools: the schema is checked before anything runs, errors come back as
data, the call is traced, and the result is fenced as untrusted (§12.4: issue bodies, emails,
comments and logs are outside text, never orders).

Approval (§12.3): a tool needs the user's approval unless the server marks it read-only. That is
the safe default; unknown means "ask".

Failures (§8.3): a rate limit stops that whole connector for a while (no retry, no workaround), and
a rejected token stops it until the user reconnects. Both say so plainly. They are read from the
real HTTP status when the server sends one (the MCP SDK turns an HTTP error into a generic message),
and otherwise from error phrases, never from bare numbers in text.
"""

import json
import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

import httpx2
import mcp_types as types
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from pydantic import BaseModel

from enzo.brain.models import TOOL_NAME_PATTERN
from enzo.tools.registry import JsonArgs, Outgoing, Prepared, Tool

log = logging.getLogger("enzo.connectors.mcp")

#: How long a connector stays stopped after a rate limit (§8.3: stop the scope, do not retry).
RATE_LIMIT_PAUSE_S = 15 * 60

type ClientFactory = Callable[[], Client]


class McpResult(BaseModel):
    """What the brain sees: the server's text, and its structured data when it sends some."""

    text: str
    data: dict[str, Any] | None = None

    def payload(self) -> Any:  # noqa: ANN401 - whatever the server sent
        """The structured data, else the text read as JSON (servers send one or both)."""
        if self.data is not None:
            return self.data
        return json.loads(self.text)


@dataclass(frozen=True, slots=True)
class Ready:
    """A prepared call: its exact arguments, the card's detail, and text it sends besides them."""

    args: dict[str, Any]
    detail: str | None
    #: Text the call also sends that its arguments do not hold: a forwarded email's own words.
    sends: tuple[str, ...] = ()


#: Calls another tool of the same server, by its server-side name, for a ``prepare`` step.
type CallFn = Callable[[str, dict[str, Any]], Awaitable[McpResult]]
type PrepareFn = Callable[
    [dict[str, Any], CallFn], Awaitable[tuple[dict[str, Any], str | None] | Ready]
]

#: Arguments that name what a write acts on (a repository, a message id), not text it sends.
_STRUCTURAL = re.compile(
    r"(?:^|_)(?:ids?|sha|ref|owner|repo|branch|base|head|method|state|labels?|"
    r"format|level|visibility)$|(?:Id|Ids|Sha|Number|Format|Level|Method)$"
)


class ConnectorError(Exception):
    """A plain-language failure the brain can relay (§8.3)."""


class ConnectorStoppedError(ConnectorError):
    """The connector is paused: rate limited, or its token was rejected."""


@dataclass(frozen=True, slots=True)
class McpServer:
    """How Enzo uses one MCP server."""

    #: Prefix for tool names, e.g. "github".
    name: str
    #: Shown to the user, e.g. "GitHub".
    label: str
    #: Server-side tool names to expose. Keep it to what Enzo needs: fewer tools, better choices.
    allow: frozenset[str] | None = None
    #: Per server-side tool: make a gated call exact before asking (e.g. pin a merge to a commit).
    prepare: Mapping[str, PrepareFn] = field(default_factory=dict)
    #: Per server-side tool: the whole action in plain words for the approval card. Without one,
    #: the card says "<label>: <tool title> (<what it acts on>)".
    summaries: Mapping[str, Callable[[dict[str, Any]], str]] = field(default_factory=dict)
    #: Per server-side tool: the step in plain words while it runs, e.g. "Searching your email".
    activities: Mapping[str, Callable[[dict[str, Any]], str]] = field(default_factory=dict)
    #: Per server-side tool: parameters Enzo never offers (e.g. HTML that the card can't show).
    drop: Mapping[str, frozenset[str]] = field(default_factory=dict)
    #: Refuse arguments the schema does not list, so nothing unshown rides along (§12.3).
    strict: bool = False
    #: The command that reconnects this server's account, e.g. "/login google".
    login: str | None = None
    #: Phrases in this server's errors that mean "rate limited", besides the usual ones.
    rate_limit_phrases: tuple[str, ...] = ()
    #: Per server-side tool: the arguments that carry text to the receiver, each with what it
    #: fills (None: free text, like an email's body; "Location": a field asking for a place).
    #: A write without an entry has all its text checked for private details (ADR 0029).
    outbound: Mapping[str, Mapping[str, str | None]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class HttpFailure:
    """An HTTP error answer seen while a call ran: its status and the server's words."""

    status: int
    text: str


#: The HTTP failures of the call running in this task (each call sets its own list).
_HTTP_FAILURES: ContextVar[list[HttpFailure] | None] = ContextVar("mcp_http_failures", default=None)


async def _note_http_failure(response: httpx2.Response) -> None:
    seen = _HTTP_FAILURES.get()
    if seen is None or response.status_code < httpx2.codes.BAD_REQUEST:
        return
    await response.aread()  # an error body is small; the SDK reads the cached copy after us
    seen.append(HttpFailure(response.status_code, _error_words(response)))


def _error_words(response: httpx2.Response) -> str:
    """The server's own error message: JSON-RPC result or error, a Google API error, or text."""
    challenge = response.headers.get("www-authenticate", "")
    scope_error = "insufficient_scope" if 'error="insufficient_scope"' in challenge else ""
    try:
        body = response.json()
    except ValueError:
        body = None
    words = ""
    if isinstance(body, dict):
        result = body.get("result")
        error = body.get("error")
        if isinstance(result, dict) and isinstance(result.get("content"), list):
            words = " ".join(
                str(c.get("text", "")) for c in result["content"] if isinstance(c, dict)
            )
        elif isinstance(error, dict):
            words = str(error.get("message", ""))
            status = error.get("status")
            if isinstance(status, str):
                words = f"{status}: {words}"
    if not words:
        words = response.text[:300]
    return " ".join(f"{scope_error} {words}".split())[:500]


@asynccontextmanager
async def http_transport(
    url: str,
    headers: Mapping[str, str],
    *,
    auth: httpx2.Auth | None = None,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> AsyncIterator[Any]:
    """Streamable HTTP with Enzo's headers (token, toolsets) or an auth flow that adds a fresh
    token to each request. The HTTP client lives as long as the session; redirects are handled
    by the SDK, which only follows same-origin ones. ``transport`` is for tests (a fake server)."""
    timeout = httpx2.Timeout(30.0, read=300.0)
    async with (
        httpx2.AsyncClient(
            headers=dict(headers),
            timeout=timeout,
            auth=auth,
            event_hooks={"response": [_note_http_failure]},
            transport=transport,
        ) as http,
        streamable_http_client(url, http_client=http) as streams,
    ):
        yield streams


class McpConnector:
    def __init__(
        self,
        server: McpServer,
        connect: ClientFactory,
        *,
        clock: Callable[[], float] = time.monotonic,
        ready: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """``ready`` runs before each use, e.g. to renew a sign-in; it raises ConnectorError."""
        self.server = server
        self._connect = connect
        self._clock = clock
        self._ready = ready
        self._stopped_until = 0.0
        self._stopped_why = ""

    async def tools(self) -> list[Tool[Any, Any]]:
        """The server's allowed tools as Enzo tools. Fails loudly if the server cannot be listed."""
        if self._ready is not None:
            await self._ready()
        listed: list[types.Tool] = []
        async with self._connect() as client:
            cursor: str | None = None
            while True:
                page = await client.list_tools(cursor=cursor)
                listed.extend(page.tools)
                cursor = page.next_cursor
                if not cursor:
                    break
        wanted = [t for t in listed if self.server.allow is None or t.name in self.server.allow]
        tools = [t for t in (self._wrap(r) for r in wanted) if t is not None]
        log.info("mcp tools", extra={"server": self.server.name, "tools": [t.name for t in tools]})
        return tools

    @property
    def stopped(self) -> str | None:
        """Why this connector is paused right now, or None while it runs."""
        return self._stopped_why if self._clock() < self._stopped_until else None

    async def call(self, name: str, arguments: dict[str, Any]) -> McpResult:
        """Run one server-side tool. One attempt: no retry, ever (§8.3)."""
        self._check_running()
        if self._ready is not None:
            await self._ready()
        seen: list[HttpFailure] = []
        mark = _HTTP_FAILURES.set(seen)
        try:
            async with self._connect() as client:
                result = await client.call_tool(name, arguments)
        except Exception as exc:
            self._notice_failure(str(exc), seen)
            if seen:
                last = seen[-1]
                msg = f"{self.server.label} refused the request (HTTP {last.status}): {last.text}"
                raise ConnectorError(msg) from exc
            msg = f"could not reach {self.server.label} ({type(exc).__name__})"
            raise ConnectorError(msg) from exc
        finally:
            _HTTP_FAILURES.reset(mark)
        text = _text(result)
        if result.is_error:
            self._notice_failure(text, seen)
            raise ConnectorError(f"{self.server.label} said: {text[:500]}")
        return McpResult(text=text, data=result.structured_content)

    def _check_running(self) -> None:
        if self._clock() < self._stopped_until:
            raise ConnectorStoppedError(self._stopped_why)

    def _notice_failure(self, text: str, http: Sequence[HttpFailure] = ()) -> None:
        lowered = " ".join([text, *(f.text for f in http)]).lower()
        statuses = {f.status for f in http}
        label = self.server.label
        login = self.server.login or f"/login {self.server.name}"
        # Error phrases or the real HTTP status, never bare numbers in text: "PR #429" or
        # "issue 401" must not stop anything.
        limited = ("rate limit", "too many requests", *self.server.rate_limit_phrases)
        if httpx2.codes.TOO_MANY_REQUESTS in statuses or any(p in lowered for p in limited):
            self._stop(
                RATE_LIMIT_PAUSE_S,
                f"{label} is rate limiting Enzo: all {label} actions are paused for "
                f"{RATE_LIMIT_PAUSE_S // 60} minutes. Nothing was retried; report what is done.",
            )
        elif httpx2.codes.UNAUTHORIZED in statuses or any(
            p in lowered for p in ("bad credentials", "401 unauthorized", "requires authentication")
        ):
            self._stop(
                float("inf"),
                f"{label} rejected the sign-in or token: reconnect with {login}. "
                f"{label} actions are paused until then.",
            )

    def _stop(self, seconds: float, why: str) -> None:
        self._stopped_until = self._clock() + seconds
        self._stopped_why = why
        log.warning("connector stopped", extra={"server": self.server.name, "why": why})

    def _wrap(self, remote: types.Tool) -> Tool[Any, Any] | None:
        name = f"{self.server.name}_{remote.name}"
        if not re.fullmatch(TOOL_NAME_PATTERN, name):
            log.warning("skipped tool with an unusable name", extra={"tool": remote.name})
            return None
        notes = remote.annotations
        read_only = bool(notes and notes.read_only_hint)
        title = (notes.title if notes else None) or remote.title or remote.name.replace("_", " ")
        description = f"{self.server.label}: {remote.description or title}"

        async def handler(args: JsonArgs, _name: str = remote.name) -> McpResult:
            return await self.call(_name, args.as_dict())

        prepare_fn = self.server.prepare.get(remote.name)
        prepare = None
        if prepare_fn is not None:

            async def prepare(args: JsonArgs, _fn: PrepareFn = prepare_fn) -> Prepared[JsonArgs]:
                ready = await _fn(args.as_dict(), self.call)
                if not isinstance(ready, Ready):
                    ready = Ready(*ready)
                sends = tuple(Outgoing(t, "message") for t in ready.sends)
                return Prepared(JsonArgs(**ready.args), ready.detail, sends=sends)

        summarize = self.server.summaries.get(remote.name)

        def summary(args: JsonArgs) -> str:
            if summarize is not None:
                return summarize(args.as_dict())
            plain = _target(args.as_dict())
            return f"{self.server.label}: {title}" + (f" ({plain})" if plain else "")

        describe = self.server.activities.get(remote.name)
        activity = None
        if describe is not None:

            def activity(args: JsonArgs, _fn: Callable[[dict[str, Any]], str] = describe) -> str:
                return _fn(args.as_dict())

        fields = self.server.outbound.get(remote.name)

        def outbound(args: JsonArgs) -> list[Outgoing]:
            data = args.as_dict()
            if fields is None:  # every bit of text it sends, but not what it acts on
                return [Outgoing(t, "message") for t in _texts(data)]
            return [
                Outgoing(t, "field" if fill else "message", field=fill)
                for key, fill in fields.items()
                for t in _texts(data.get(key))
            ]

        return Tool(
            name=name,
            description=description,
            params=JsonArgs,
            returns=McpResult,
            handler=handler,
            requires_approval=not read_only,
            timeout_s=120,
            untrusted_output=True,
            summary=summary,
            activity=activity,
            prepare=prepare,
            schema=self._schema(remote),
            # A write sends text to others: it is checked for private details (§12.5).
            outbound=None if read_only else outbound,
        )

    def _schema(self, remote: types.Tool) -> dict[str, Any]:
        schema = dict(remote.input_schema)
        dropped = self.server.drop.get(remote.name, frozenset())
        if not dropped and not self.server.strict:
            return schema
        properties = schema.get("properties", {})
        schema["properties"] = {k: v for k, v in properties.items() if k not in dropped}
        if "required" in schema:
            schema["required"] = [r for r in schema["required"] if r not in dropped]
        schema["additionalProperties"] = False  # a dropped field must not come back unseen
        return schema


def _texts(value: object, key: str = "") -> list[str]:
    """The text in an argument, however nested; never what a write acts on (ids, repos)."""
    if key and _STRUCTURAL.search(key):
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [t for k, v in value.items() for t in _texts(v, str(k))]
    if isinstance(value, list):
        return [t for v in value for t in _texts(v)]
    return []


def _target(args: dict[str, Any]) -> str:
    """The thing acted on, from the usual argument names: owner/repo #number."""
    where = "/".join(str(args[k]) for k in ("owner", "repo") if k in args)
    for key in ("pullNumber", "issue_number", "pull_number", "number"):
        if key in args:
            return f"{where} #{args[key]}".strip()
    return where


def _text(result: types.CallToolResult) -> str:
    parts = [c.text for c in result.content if isinstance(c, types.TextContent)]
    if parts:
        return "\n".join(parts)
    if result.structured_content is not None:
        return json.dumps(result.structured_content)
    return ""
