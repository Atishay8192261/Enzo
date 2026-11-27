"""Web tools: search and read pages as text (spec §7.2.2 Browser, §7.2.4 text-fetch mode).

- ``web_search`` uses Tavily. Without a key it uses Tavily's keyless mode (free, rate-limited);
  a key connected with ``/login tavily`` raises the limits. Only the query leaves the machine.
- ``read_page`` fetches the page itself (the URL is not sent to a third party) and keeps only the
  main text. It refuses private and internal addresses, so content met along the way cannot point
  Enzo at the user's router or a cloud metadata service (SSRF). A failed fetch says only that the
  fetch failed, never that the site is down (§7.2.4).

Both return outside content, fenced as untrusted data (§12.4).
"""

import asyncio
import ipaddress
import re
import socket
import urllib.request
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import unquote_plus, urljoin, urlsplit

import httpx2
from pydantic import BaseModel, ConfigDict, Field
from urllib3.exceptions import DependencyWarning

from enzo.tools.registry import Outgoing, Tool, plain_words, tool

with warnings.catch_warnings():
    # trafilatura probes urllib3's optional SOCKS support on import, and urllib3 warns when
    # PySocks is absent. Enzo only parses HTML with trafilatura (it never fetches through it).
    warnings.filterwarnings("ignore", "SOCKS support in urllib3", DependencyWarning)
    import trafilatura

TAVILY_SEARCH = "https://api.tavily.com/search"
USER_AGENT = "Enzo/0.1 (personal AI agent; reads pages on its user's request)"
MAX_PAGE_BYTES = 3_000_000
MAX_REDIRECTS = 5
TEXT_TYPES = ("text/html", "application/xhtml+xml", "text/plain")

type KeyLookup = Callable[[], str | None]


class WebToolError(Exception):
    """A plain-language failure the brain can relay. Never claims the site itself is down."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        #: The site's HTTP status when it answered with an error (a watch stops on 429, §8.3).
        self.status = status


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchParams(_Params):
    query: str = Field(
        min_length=1,
        max_length=400,
        description=(
            "What to search the web for: the topic only. It goes to a search company, so never "
            "put the user's or their family's names, address, contact details, ID numbers or a "
            "private detail about them in it unless they asked: 'alcohol-free restaurants "
            "Austin', not 'restaurant for my pregnant wife'."
        ),
    )
    max_results: int = Field(default=5, ge=1, le=10, description="How many results to return.")
    topic: Literal["general", "news", "finance"] = Field(
        default="general", description="Use news for current events, finance for markets."
    )
    time_range: Literal["day", "week", "month", "year"] | None = Field(
        default=None, description="Only results published within this period."
    )


class SearchHit(BaseModel):
    title: str
    url: str
    snippet: str


class SearchResults(BaseModel):
    query: str
    results: list[SearchHit]


class ReadParams(_Params):
    url: str = Field(min_length=1, max_length=2000, description="The http(s) address to read.")
    max_chars: int = Field(
        default=12_000,
        ge=500,
        le=1_000_000,
        description=(
            "Longest text to return; the rest is cut. A long result is kept whole by Enzo: you "
            "get its start and an id to read on."
        ),
    )


class Page(BaseModel):
    url: str  # after redirects
    title: str | None
    text: str
    truncated: bool


def web_tools(*, tavily_key: KeyLookup, http: httpx2.AsyncClient) -> list[Tool[Any, Any]]:
    """The web tools, bound to the user's Tavily key (if any) and a shared HTTP client."""

    @tool(
        name="web_search",
        description=(
            "Search the web for current information: news, prices, facts, documentation. "
            "Returns titles, addresses and short snippets; cite the address of what you use. "
            "Read a result in full with read_page."
        ),
        timeout_s=30,
        untrusted_output=True,
        activity=_search_activity,
        # The query leaves the computer: private details in it ask first (§12.5, ADR 0029).
        summary=lambda p: f"Search the web for “{plain_words(p.query, 160)}”",
        outbound=lambda p: [Outgoing(p.query, "search")],
    )
    async def web_search(params: SearchParams) -> SearchResults:
        return await search(params, http=http, key=tavily_key())

    @tool(
        name="read_page",
        description=(
            "Read a web page as plain text (no login, no clicks). Use it to read a search "
            "result or an address the user gave. A failure means only that this fetch failed."
        ),
        timeout_s=30,
        untrusted_output=True,
        activity=_read_activity,
        # The address goes to the site: one that carries a private detail asks first.
        summary=lambda p: f"Open {plain_words(p.url, 200)} to read it",
        outbound=lambda p: [Outgoing(address_words(p.url), "address")],
    )
    async def read_page(params: ReadParams) -> Page:
        return await fetch_page(params, http=http)

    return [web_search, read_page]


def _search_activity(params: SearchParams) -> str:
    return f"Searching the web for “{plain_words(params.query, 60)}”"


def address_words(url: str) -> str:
    """What a web address tells the site beyond its name: its path and query, as words
    ("/collect?address=12+Oak+Street" is "collect address 12 Oak Street")."""
    parts = urlsplit(url)
    raw = " ".join((parts.path, parts.query, parts.fragment))
    return " ".join(re.sub(r"[/?&=#;_]+", " ", unquote_plus(raw)).split())


def _read_activity(params: ReadParams) -> str:
    return f"Reading {page_name(params.url)}"


def page_name(url: str) -> str:
    """An address as people say it: ``https://www.bbc.com/news/`` is ``bbc.com/news``."""
    rest = url.split("://", 1)[-1].split("#", 1)[0].split("?", 1)[0]
    rest = rest.removeprefix("www.").rstrip("/")
    return plain_words(rest, 60) or url


async def search(
    params: SearchParams, *, http: httpx2.AsyncClient, key: str | None
) -> SearchResults:
    body: dict[str, object] = {
        "query": params.query,
        "max_results": params.max_results,
        "topic": params.topic,
        "search_depth": "basic",  # 1 credit; "advanced" costs 2 (docs.tavily.com, read 2026-11-18)
    }
    if params.time_range:
        body["time_range"] = params.time_range
    # A connected key takes precedence; keyless mode is free but rate-limited.
    headers = {"Authorization": f"Bearer {key}"} if key else {"X-Tavily-Access-Mode": "keyless"}
    try:
        response = await http.post(TAVILY_SEARCH, json=body, headers=headers)
    except httpx2.HTTPError as exc:
        msg = f"could not reach the search service ({type(exc).__name__})"
        raise WebToolError(msg) from exc
    if response.status_code == 401:
        msg = "the search service rejected the Tavily key: run /login tavily with a new key"
        raise WebToolError(msg)
    if response.status_code in (429, 432, 433, 434):
        msg = "web search limit reached" + (
            "" if key else ": connect a free Tavily key with /login tavily for more searches"
        )
        raise WebToolError(msg)
    if response.status_code != 200:
        msg = f"the search service failed (HTTP {response.status_code})"
        raise WebToolError(msg)
    data = response.json()
    hits = [
        SearchHit(title=r.get("title") or r["url"], url=r["url"], snippet=r.get("content") or "")
        for r in data.get("results", [])
        if isinstance(r, dict) and isinstance(r.get("url"), str)
    ]
    return SearchResults(query=params.query, results=hits)


async def fetch_page(params: ReadParams, *, http: httpx2.AsyncClient) -> Page:
    got = await fetch_body(params.url, http=http)
    return _to_page(got.url, got.kind, got.body, params.max_chars)


@dataclass(frozen=True, slots=True)
class Fetched:
    """A page as fetched: its address after redirects, its content type and its bytes."""

    url: str
    kind: str
    body: bytes


async def fetch_body(
    start: str, *, http: httpx2.AsyncClient, private_ok: Callable[[str], bool] | None = None
) -> Fetched:
    """Fetch a text page, checking every redirect hop (SSRF guard).

    ``private_ok``: an address inside a private network that the user named themselves (a watch
    on a page on this computer, approved on its card) may be fetched. Every other hop is checked.
    """
    url = start
    for _ in range(MAX_REDIRECTS + 1):
        if private_ok is None or not private_ok(url):
            await check_public(url)  # every hop: a redirect must not lead inside the network
        request = http.build_request("GET", url, headers={"User-Agent": USER_AGENT})
        try:
            response = await http.send(request, stream=True)
        except httpx2.HTTPError as exc:
            reason = type(exc).__name__
            msg = f"could not fetch {url} ({reason}); this fetch failed, the site may be fine"
            raise WebToolError(msg) from exc
        try:
            if response.is_redirect and "location" in response.headers:
                url = urljoin(url, response.headers["location"])
                continue
            if response.status_code != 200:
                msg = f"fetching {url} returned HTTP {response.status_code}; this fetch failed"
                raise WebToolError(msg, status=response.status_code)
            kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
            if kind not in TEXT_TYPES:
                msg = (
                    f"{url} is {kind or 'an unknown type'}, not a text page; it cannot be read here"
                )
                raise WebToolError(msg)
            body = await _read_capped(response)
        finally:
            await response.aclose()
        return Fetched(url, kind, body)
    msg = f"{start} redirected more than {MAX_REDIRECTS} times; this fetch failed"
    raise WebToolError(msg)


async def check_public(url: str) -> None:
    """Refuse non-web schemes and addresses inside private networks (SSRF guard).

    Without a proxy, the host is resolved here and every address must be public. When a proxy is
    configured for the host, the proxy resolves the name, so only literal IPs and names this
    machine can resolve are checked; ADR 0011 records that limit.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        msg = f"only http and https pages can be read, not {parts.scheme or 'this address'}"
        raise WebToolError(msg)
    if not parts.hostname or parts.username or parts.password:
        msg = "that address has no host, or carries a login; it cannot be read"
        raise WebToolError(msg)
    host = parts.hostname
    private = await private_addresses(host, parts.port or (443 if parts.scheme == "https" else 80))
    if private is None:
        if _proxied(host):
            return  # the proxy resolves it (see docstring)
        msg = f"could not find the address of {host}; this fetch failed"
        raise WebToolError(msg)
    if private:
        where = ", ".join(private)
        msg = f"{host} points inside a private network ({where}); Enzo does not read those"
        raise WebToolError(msg)


async def private_addresses(host: str, port: int) -> list[str] | None:
    """The addresses of ``host`` inside a private network (loopback, LAN, link-local, cloud
    metadata), sorted; empty when all are public; None when the name does not resolve here."""
    literal = _ip(host.strip("[]"))
    addresses = {literal} if literal is not None else await _resolve(host, port)
    if addresses is None:
        return None
    return sorted(str(a) for a in addresses if not a.is_global)


type IP = ipaddress.IPv4Address | ipaddress.IPv6Address


def _ip(host: str) -> IP | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


async def _resolve(
    host: str, port: int
) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address] | None:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return None
    return {ipaddress.ip_address(info[4][0]) for info in infos}


def _proxied(host: str) -> bool:
    proxies = urllib.request.getproxies()
    return bool(proxies.get("https") or proxies.get("http")) and not urllib.request.proxy_bypass(
        host
    )


async def _read_capped(response: httpx2.Response) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > MAX_PAGE_BYTES:
            msg = f"{response.url} is larger than {MAX_PAGE_BYTES // 1_000_000} MB; it was not read"
            raise WebToolError(msg)
        chunks.append(chunk)
    return b"".join(chunks)


def main_text(url: str, kind: str, body: bytes) -> tuple[str | None, str]:
    """A page's title and main text: as is for plain text, trafilatura's extraction for HTML."""
    if kind == "text/plain":
        return None, body.decode("utf-8", errors="replace")
    doc = trafilatura.bare_extraction(
        body, url=url, with_metadata=True, include_comments=False, include_tables=True
    )
    if doc is None or isinstance(doc, dict) or not doc.text:
        msg = f"{url} has no readable main text (it may need a browser to render)"
        raise WebToolError(msg)
    return doc.title, doc.text


def _to_page(url: str, kind: str, body: bytes, max_chars: int) -> Page:
    title, text = main_text(url, kind, body)
    truncated = len(text) > max_chars
    return Page(url=url, title=title, text=text[:max_chars], truncated=truncated)
