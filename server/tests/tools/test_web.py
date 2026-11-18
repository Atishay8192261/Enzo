"""Web tools against a fake network: the request on the wire, the SSRF guard, honest failures."""

import ipaddress
import json
from collections.abc import Callable

import httpx2
import pytest

from enzo.brain import ToolCall
from enzo.tools import ToolRegistry
from enzo.tools import web as w
from enzo.tools.web import ReadParams, SearchParams, WebToolError, check_public, fetch_page, search
from tests.fakes import fake_key

type Handler = Callable[[httpx2.Request], httpx2.Response]

ARTICLE = """<html><head><title>Rain in Paris</title></head><body>
<nav>Home | News | Weather</nav>
<article><h1>Rain in Paris</h1>
<p>Heavy rain is expected in Paris on Tuesday, with up to forty millimetres falling by evening.</p>
<p>Officials advise travellers to check train times before leaving, as some lines may close.</p>
</article><footer>Copyright</footer></body></html>"""


def client(handler: Handler) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler), follow_redirects=False)


@pytest.fixture(autouse=True)
def public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Names resolve to a public address unless a test says otherwise (no real DNS in tests)."""

    async def resolve(host: str, _port: int) -> set[w.IP] | None:
        table = {"localhost": "127.0.0.1", "router.home": "192.168.1.1", "nowhere.test": None}
        ip = table.get(host, "93.184.215.14")
        return None if ip is None else {ipaddress.ip_address(ip)}

    monkeypatch.setattr(w, "_resolve", resolve)
    monkeypatch.setattr(w, "_proxied", lambda _host: False)


# ---------- web_search ----------


class Recorder:
    def __init__(self, status: int = 200, body: object | None = None) -> None:
        self.status = status
        self.body = body if body is not None else {"query": "q", "results": []}
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return httpx2.Response(self.status, json=self.body)


async def test_search_is_keyless_by_default_and_sends_only_the_query() -> None:
    rec = Recorder(
        body={
            "query": "rain paris",
            "results": [
                {"title": "Rain", "url": "https://news.example/rain", "content": "Heavy rain"},
                {"url": "https://no-title.example", "content": None},
                {"title": "broken"},  # no url: dropped
            ],
        }
    )
    async with client(rec) as http:
        out = await search(SearchParams(query="rain paris"), http=http, key=None)
    (req,) = rec.requests
    assert (req.method, str(req.url)) == ("POST", w.TAVILY_SEARCH)
    assert req.headers["x-tavily-access-mode"] == "keyless"
    assert "authorization" not in req.headers
    assert json.loads(req.content) == {
        "query": "rain paris",
        "max_results": 5,
        "topic": "general",
        "search_depth": "basic",
    }
    assert [(h.title, h.url, h.snippet) for h in out.results] == [
        ("Rain", "https://news.example/rain", "Heavy rain"),
        ("https://no-title.example", "https://no-title.example", ""),
    ]


async def test_search_uses_a_connected_key_and_passes_filters() -> None:
    rec = Recorder()
    params = SearchParams(query="markets", max_results=3, topic="finance", time_range="week")
    async with client(rec) as http:
        await search(params, http=http, key=fake_key("tavily"))
    (req,) = rec.requests
    assert req.headers["authorization"] == f"Bearer {fake_key('tavily')}"
    assert "x-tavily-access-mode" not in req.headers
    body = json.loads(req.content)
    assert (body["max_results"], body["topic"], body["time_range"]) == (3, "finance", "week")


@pytest.mark.parametrize(
    ("status", "key", "message"),
    [
        (401, fake_key("tavily"), "rejected the Tavily key: run /login tavily"),
        (429, None, "limit reached: connect a free Tavily key with /login tavily"),
        (432, fake_key("tavily"), "web search limit reached"),
        (500, None, r"the search service failed \(HTTP 500\)"),
    ],
)
async def test_search_failures_say_what_to_do(status: int, key: str | None, message: str) -> None:
    async with client(Recorder(status, {"detail": {"error": "x"}})) as http:
        with pytest.raises(WebToolError, match=message) as exc:
            await search(SearchParams(query="q"), http=http, key=key)
    if key:
        assert "connect a free" not in str(exc.value)  # already has a key


async def test_search_unreachable() -> None:
    def down(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    async with client(down) as http:
        with pytest.raises(WebToolError, match="could not reach the search service"):
            await search(SearchParams(query="q"), http=http, key=None)


# ---------- check_public (SSRF guard) ----------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://localhost:8787/connections",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/",
        "http://[::1]/",
        "http://[fd00::1]/",
        "http://router.home/",
        "http://0.0.0.0/",
    ],
)
async def test_private_and_internal_addresses_are_refused(url: str) -> None:
    with pytest.raises(WebToolError, match="private network"):
        await check_public(url)


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("file:///etc/passwd", "only http and https"),
        ("ftp://example.com/x", "only http and https"),
        ("javascript:alert(1)", "only http and https"),
        ("https://user:pass@example.com/", "carries a login"),
        ("https:///no-host", "has no host"),
        ("https://nowhere.test/", "could not find the address"),
    ],
)
async def test_other_bad_addresses_are_refused(url: str, message: str) -> None:
    with pytest.raises(WebToolError, match=message):
        await check_public(url)


async def test_public_addresses_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    await check_public("https://example.com/page")
    await check_public("http://8.8.8.8/")
    # Behind a proxy that resolves names itself, an unresolvable name goes to the proxy.
    monkeypatch.setattr(w, "_proxied", lambda _host: True)
    await check_public("https://nowhere.test/")
    with pytest.raises(WebToolError, match="private network"):
        await check_public("http://127.0.0.1/")  # literal IPs are still checked


# ---------- read_page ----------


def page(body: str | bytes, kind: str = "text/html; charset=utf-8", status: int = 200) -> Handler:
    data = body.encode() if isinstance(body, str) else body
    return lambda _r: httpx2.Response(status, content=data, headers={"content-type": kind})


async def read(handler: Handler, url: str = "https://example.com/a", **kw: int) -> w.Page:
    async with client(handler) as http:
        return await fetch_page(ReadParams(url=url, **kw), http=http)


async def test_reads_the_main_text_of_a_page() -> None:
    got = await read(page(ARTICLE))
    assert got.title == "Rain in Paris"
    assert "forty millimetres" in got.text
    assert "Copyright" not in got.text  # boilerplate dropped
    assert (got.url, got.truncated) == ("https://example.com/a", False)


async def test_plain_text_and_truncation() -> None:
    got = await read(page("x" * 2000, "text/plain"), max_chars=500)
    assert (len(got.text), got.truncated, got.title) == (500, True, None)


async def test_follows_public_redirects_and_reports_the_final_address() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/old":
            return httpx2.Response(301, headers={"location": "/new"})
        return page(ARTICLE)(request)

    got = await read(handler, "https://example.com/old")
    assert got.url == "https://example.com/new"


async def test_a_redirect_into_the_private_network_is_refused() -> None:
    seen: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(str(request.url))
        return httpx2.Response(302, headers={"location": "http://169.254.169.254/latest/"})

    with pytest.raises(WebToolError, match="private network"):
        await read(handler)
    assert seen == ["https://example.com/a"]  # the private address was never requested


async def test_redirect_loops_stop() -> None:
    loop = lambda _r: httpx2.Response(302, headers={"location": "/a"})  # noqa: E731
    with pytest.raises(WebToolError, match="redirected more than 5 times"):
        await read(loop)


@pytest.mark.parametrize(
    ("handler", "message"),
    [
        (page(b"%PDF-1.7", "application/pdf"), "application/pdf, not a text page"),
        (page("gone", status=404), "returned HTTP 404; this fetch failed"),
        # a JavaScript app shell: nothing to read without a browser
        (
            page('<html><body><div id="root"></div><script>app()</script></body></html>'),
            "no readable main text",
        ),
    ],
)
async def test_unreadable_pages_fail_honestly(handler: Handler, message: str) -> None:
    with pytest.raises(WebToolError, match=message) as exc:
        await read(handler)
    assert "down" not in str(exc.value)  # §7.2.4: a failed fetch is not "the site is down"


async def test_huge_pages_are_not_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(w, "MAX_PAGE_BYTES", 1000)
    with pytest.raises(WebToolError, match="larger than"):
        await read(page("x" * 5000, "text/plain"))


async def test_unreachable_page_says_only_the_fetch_failed() -> None:
    def down(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    with pytest.raises(WebToolError, match="this fetch failed, the site may be fine"):
        await read(down)


async def test_web_tools_through_the_registry_are_fenced() -> None:
    async with client(page(ARTICLE)) as http:
        registry = ToolRegistry(w.web_tools(tavily_key=lambda: None, http=http))
        call = ToolCall(id="c1", name="read_page", arguments_json='{"url": "https://example.com"}')
        outcome = await registry.execute(call)
    assert outcome.status == "ok"
    assert outcome.result.content.startswith('<untrusted-data source="read_page" id="')
    assert "forty millimetres" in outcome.result.content
