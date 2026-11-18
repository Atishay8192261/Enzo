"""MCP servers as Enzo tools (spec §7.2.3 connectors, ADR 0012).

An MCP server lists tools with a JSON Schema each. Every one becomes an Enzo tool and goes through
the same path as Enzo's own tools: the schema is checked before anything runs, errors come back as
data, the call is traced, and the result is fenced as untrusted (§12.4: issue bodies, comments
and logs are outside text, never orders).

Approval (§12.3): a tool needs the user's approval unless the server marks it read-only. That is
the safe default; unknown means "ask".

Failures (§8.3): a rate limit stops that whole connector for a while (no retry, no workaround), and
a rejected token stops it until the user reconnects. Both say so plainly.
"""

import json
import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx2
import mcp_types as types
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from pydantic import BaseModel

from enzo.brain.models import TOOL_NAME_PATTERN
from enzo.tools.registry import JsonArgs, Prepared, Tool

log = logging.getLogger("enzo.connectors.mcp")

#: How long a connector stays stopped after a rate limit (§8.3: stop the scope, do not retry).
RATE_LIMIT_PAUSE_S = 15 * 60

type ClientFactory = Callable[[], Client]
#: Calls another tool of the same server, by its server-side name, for a ``prepare`` step.
type CallFn = Callable[[str, dict[str, Any]], Awaitable[str]]
type PrepareFn = Callable[[dict[str, Any], CallFn], Awaitable[tuple[dict[str, Any], str | None]]]


class McpResult(BaseModel):
    """What the brain sees: the server's text, and its structured data when it sends some."""

    text: str
    data: dict[str, Any] | None = None


class ConnectorError(Exception):
    """A plain-language failure the brain can relay (§8.3)."""


class ConnectorStoppedError(ConnectorError):
    """The connector is paused: rate limited, or its token was rejected."""


@dataclass(frozen=True, slots=True)
class McpServer:
    """How Enzo uses one MCP server."""

    #: Prefix for tool names and the account it uses, e.g. "github".
    name: str
    #: Shown to the user, e.g. "GitHub".
    label: str
    #: Server-side tool names to expose. Keep it to what Enzo needs: fewer tools, better choices.
    allow: frozenset[str] | None = None
    #: Per server-side tool: make a gated call exact before asking (e.g. pin a merge to a commit).
    prepare: Mapping[str, PrepareFn] = field(default_factory=dict)
    #: Per server-side tool: the action in plain words for the approval card.
    summaries: Mapping[str, Callable[[dict[str, Any]], str]] = field(default_factory=dict)


@asynccontextmanager
async def http_transport(url: str, headers: Mapping[str, str]) -> AsyncIterator[Any]:
    """Streamable HTTP with Enzo's headers (token, toolsets). The HTTP client lives as long as the
    session; redirects are handled by the SDK, which only follows same-origin ones."""
    timeout = httpx2.Timeout(30.0, read=300.0)
    async with (
        httpx2.AsyncClient(headers=dict(headers), timeout=timeout) as http,
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
    ) -> None:
        self.server = server
        self._connect = connect
        self._clock = clock
        self._stopped_until = 0.0
        self._stopped_why = ""

    async def tools(self) -> list[Tool[Any, Any]]:
        """The server's allowed tools as Enzo tools. Fails loudly if the server cannot be listed."""
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

    async def call(self, name: str, arguments: dict[str, Any]) -> McpResult:
        """Run one server-side tool. One attempt: no retry, ever (§8.3)."""
        self._check_running()
        try:
            async with self._connect() as client:
                result = await client.call_tool(name, arguments)
        except Exception as exc:
            self._notice_failure(str(exc))
            msg = f"could not reach {self.server.label} ({type(exc).__name__})"
            raise ConnectorError(msg) from exc
        text = _text(result)
        if result.is_error:
            self._notice_failure(text)
            raise ConnectorError(f"{self.server.label} said: {text[:500]}")
        return McpResult(text=text, data=result.structured_content)

    async def _call_text(self, name: str, arguments: dict[str, Any]) -> str:
        return (await self.call(name, arguments)).text

    def _check_running(self) -> None:
        if self._clock() < self._stopped_until:
            raise ConnectorStoppedError(self._stopped_why)

    def _notice_failure(self, text: str) -> None:
        lowered = text.lower()
        label = self.server.label
        # Error phrases, not bare numbers: "PR #429" or "issue 401" must not stop anything.
        if "rate limit" in lowered or "too many requests" in lowered:
            self._stop(
                RATE_LIMIT_PAUSE_S,
                f"{label} is rate limiting Enzo: all {label} actions are paused for "
                f"{RATE_LIMIT_PAUSE_S // 60} minutes. Nothing was retried; report what is done.",
            )
        elif any(
            p in lowered for p in ("bad credentials", "401 unauthorized", "requires authentication")
        ):
            self._stop(
                float("inf"),
                f"{label} rejected the token: reconnect with /login {self.server.name}. "
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
                exact, detail = await _fn(args.as_dict(), self._call_text)
                return Prepared(JsonArgs(**exact), detail)

        summarize = self.server.summaries.get(remote.name)

        def summary(args: JsonArgs) -> str:
            plain = summarize(args.as_dict()) if summarize else _target(args.as_dict())
            return f"{self.server.label}: {title}" + (f" ({plain})" if plain else "")

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
            prepare=prepare,
            schema=dict(remote.input_schema),
        )


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
