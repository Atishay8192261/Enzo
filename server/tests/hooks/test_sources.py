"""What one check reads, with no model call: a page's part, new emails, calendar events, a pull
request's checks and comments. Failures are plain words; a rate limit or refused sign-in says so."""

import ipaddress
from datetime import UTC, datetime
from pathlib import Path

import httpx2
import pytest

from enzo.connectors.github import github_tools
from enzo.hooks.rules import seen
from enzo.hooks.sources import (
    CalendarTarget,
    EmailTarget,
    PageTarget,
    PollError,
    PullRequestTarget,
    Sources,
    scope_of,
    where,
)
from enzo.tools import ToolRegistry
from enzo.tools import web as w
from tests.connectors.fake_github import client_for, fake_github
from tests.connectors.test_google import google

SHOP = """<html><head><title>Blue kettle</title></head><body>
<nav>Home | Kettles</nav>
<article><h1>Blue kettle</h1>
<p>A sturdy kettle for the stove, in blue enamel, that holds one and a half litres.</p>
<p>Price: <span class="price">$59.99</span> (free delivery over $50)</p>
<p>Ships in two days from our warehouse, with a one year guarantee on the enamel.</p>
</article></body></html>"""

OWN_PORT = 8890


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    async def resolve(host: str, _port: int) -> set[w.IP] | None:
        table = {"localhost": "127.0.0.1", "router.home": "192.168.1.1"}
        return {ipaddress.ip_address(table.get(host, "93.184.215.14"))}

    monkeypatch.setattr(w, "_resolve", resolve)
    monkeypatch.setattr(w, "_proxied", lambda _host: False)


class Site:
    """A fake web: pages by URL, changed by the test; every request is recorded."""

    def __init__(self, pages: dict[str, tuple[int, str]]) -> None:
        self.pages = pages
        self.asked: list[str] = []

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.asked.append(str(request.url))
        status, body = self.pages.get(str(request.url), (404, "gone"))
        if status in (301, 302):
            return httpx2.Response(status, headers={"location": body})
        return httpx2.Response(status, text=body, headers={"content-type": "text/html"})

    def sources(self, registry: ToolRegistry | None = None) -> Sources:
        http = httpx2.AsyncClient(transport=httpx2.MockTransport(self.handler))
        return Sources(
            http=http,
            tools=(registry or ToolRegistry()).get,
            own_port=OWN_PORT,
            clock=lambda: datetime(2026, 11, 27, 9, 0, tzinfo=UTC),
        )


URL = "https://shop.example/kettle"


async def test_a_page_part_by_selector_line_or_whole_text() -> None:
    site = Site({URL: (200, SHOP)})
    read = site.sources().read
    price = await read(PageTarget(url=URL, selector=".price"), "UTC")
    assert price.text == "$59.99"
    line = await read(PageTarget(url=URL, text="price:"), "UTC")  # any case
    assert line.text == "Price: $59.99 (free delivery over $50)"
    whole = await read(PageTarget(url=URL), "UTC")
    assert "sturdy kettle" in whole.text
    assert "Home | Kettles" not in whole.text  # the page's main text, not its menus
    # Not there now is a reading too ("appears" waits for it), never an error.
    assert (await read(PageTarget(url=URL, selector=".sold-out"), "UTC")).text == ""
    assert (await read(PageTarget(url=URL, text="In stock"), "UTC")).text == ""


async def test_a_rate_limit_says_so_and_other_failures_are_plain() -> None:
    site = Site({URL: (429, "slow down"), "https://shop.example/x": (503, "down")})
    with pytest.raises(PollError, match="too many requests") as limited:
        await site.sources().read(PageTarget(url=URL, selector=".price"), "UTC")
    assert limited.value.stop == "rate_limited"
    with pytest.raises(PollError, match="HTTP 503") as down:
        await site.sources().read(PageTarget(url="https://shop.example/x"), "UTC")
    assert down.value.stop is None


async def test_private_pages_need_the_users_ok_and_never_enzos_own_port() -> None:
    local = "http://localhost:8891/price"
    site = Site(
        {
            local: (200, SHOP),
            "http://localhost:8891/away": (302, "http://router.home/admin"),
            f"http://localhost:{OWN_PORT}/healthz": (200, "{}"),
            "https://shop.example/to-local": (302, local),
        }
    )
    read = site.sources().read
    with pytest.raises(PollError, match="private network"):
        await read(PageTarget(url=local, selector=".price"), "UTC")  # not approved as local
    assert (await read(PageTarget(url=local, selector=".price", local=True), "UTC")).text
    with pytest.raises(PollError, match="private network"):  # only the approved site
        await read(PageTarget(url="http://localhost:8891/away", local=True), "UTC")
    with pytest.raises(PollError, match="private network"):  # a public page cannot lead in
        await read(PageTarget(url="https://shop.example/to-local"), "UTC")
    with pytest.raises(PollError, match="Enzo's own address"):
        await read(PageTarget(url=f"http://localhost:{OWN_PORT}/healthz", local=True), "UTC")
    assert f"http://localhost:{OWN_PORT}/healthz" not in site.asked
    assert "http://router.home/admin" not in site.asked


def test_scopes_and_words() -> None:
    assert scope_of(PageTarget(url="https://www.shop.example/a")) == "site:shop.example"
    assert scope_of(EmailTarget(query="x")) == "gmail"
    assert (
        where(PageTarget(url=URL, selector=".price"))
        == "the text at “.price” on shop.example/kettle"
    )
    assert where(PageTarget(url="http://localhost:8891/p", local=True)).endswith(
        "(on this computer or your local network)"
    )
    assert where(CalendarTarget(query="dentist", days=1)) == (
        "your calendar for the next 1 day, events matching “dentist”"
    )
    pr = PullRequestTarget(owner="octo", repo="app", number=7, watch="comments")
    assert where(pr) == "the comments on octo/app #7"


# ---------- connectors, through the real MCP connectors and their fakes ----------


async def _google(tmp_path: Path, **kw: object) -> tuple[Sources, object]:
    connectors, fake, _sign_in = await google(tmp_path, **kw)  # type: ignore[arg-type]
    registry = ToolRegistry(await connectors.tools())
    return Site({}).sources(registry), fake


async def test_new_email_is_a_new_conversation_or_a_new_message(tmp_path: Path) -> None:
    sources, fake = await _google(tmp_path)
    one = {"id": "m1", "subject": "Gate change", "sender": "airline@example.com"}
    fake.threads = [{"id": "t1", "messages": [one]}]  # type: ignore[attr-defined]
    target = EmailTarget(query="from:airline")
    before = seen(await sources.read(target, "UTC"), None)
    assert before.shown == (
        "1 matching email conversation; newest: “Gate change” from airline@example.com"
    )
    reply = {"id": "m2", "subject": "Re: Gate change", "sender": "airline@example.com"}
    fake.threads = [{"id": "t1", "messages": [reply, one]}]  # type: ignore[attr-defined]
    after = seen(await sources.read(target, "UTC"), None)
    assert set(after.key.splitlines()) - set(before.key.splitlines()) == {"message:m2"}
    (call,) = [c for c in fake.calls if c[1] == "search_threads"][-1:]  # type: ignore[attr-defined]
    assert call[2] == {"query": "from:airline", "pageSize": 10}  # read-only, and only this


async def test_a_gmail_rate_limit_or_no_account_stops_with_its_reason(tmp_path: Path) -> None:
    sources, fake = await _google(tmp_path)
    fake.mode["gmail"] = "rate_limited"  # type: ignore[attr-defined]
    with pytest.raises(PollError) as limited:
        await sources.read(EmailTarget(query="x"), "UTC")
    assert limited.value.stop == "rate_limited"
    with pytest.raises(PollError, match="Gmail is not connected") as missing:
        await Site({}).sources().read(EmailTarget(query="x"), "UTC")
    assert missing.value.stop == "auth"


async def test_calendar_events_are_compared_by_when_they_are(tmp_path: Path) -> None:
    sources, fake = await _google(tmp_path)
    target = CalendarTarget(days=7)
    before = await sources.read(target, "Europe/Berlin")
    assert before.label == "1 event in the next 7 days; next: “Team sync”, Wed 2 Dec 10:00"
    fake.events["ev1"]["start"] = {"dateTime": "2026-12-02T11:00:00+01:00"}  # type: ignore[attr-defined]
    after = await sources.read(target, "Europe/Berlin")
    assert set(after.items or ()) - set(before.items or ())  # moved: a new key
    (call,) = [c for c in fake.calls if c[1] == "list_events"][-1:]  # type: ignore[attr-defined]
    assert call[2]["startTime"] == "2026-11-27T10:00:00"  # local time, as Google asks
    assert call[2]["endTime"] == "2026-12-04T10:00:00"
    assert call[2]["timeZone"] == "Europe/Berlin"


async def _github() -> tuple[Sources, object]:
    server, state = fake_github()
    async with httpx2.AsyncClient() as http:
        tools = await github_tools(lambda: "t", http, connect=lambda: client_for(server))
    return Site({}).sources(ToolRegistry(tools)), state


async def test_pull_request_checks_read_as_one_state() -> None:
    sources, state = await _github()
    target = PullRequestTarget(owner="octo", repo="app", number=7)
    assert (await sources.read(target, "UTC")).text == "no checks"
    state.statuses, state.status = 1, "success"  # type: ignore[attr-defined]
    assert (await sources.read(target, "UTC")).text == "passing"  # legacy statuses
    state.checks = [  # type: ignore[attr-defined]
        {"id": 1, "name": "lint", "status": "completed", "conclusion": "success"},
        {"id": 2, "name": "test", "status": "in_progress"},
    ]
    running = await sources.read(target, "UTC")
    assert (running.text, running.label) == ("running", "checks running (1 of 2 done)")
    state.checks[1].update(status="completed", conclusion="failure")  # type: ignore[attr-defined]
    failing = await sources.read(target, "UTC")
    assert (failing.text, failing.label) == ("failing", "checks failing (1 of 2: test)")


async def test_pull_request_comments_across_pages_and_a_rate_limit() -> None:
    sources, state = await _github()
    state.comments = [  # type: ignore[attr-defined]
        {"id": i, "body": f"note {i}", "user": {"login": "ann"}} for i in range(1, 151)
    ]
    target = PullRequestTarget(owner="octo", repo="app", number=7, watch="comments")
    got = await sources.read(target, "UTC")
    assert len(got.items or ()) == 150  # the newest are on the second page
    assert got.label == "150 comments; newest by ann: “note 150”"
    state.rate_limited = True  # type: ignore[attr-defined]
    with pytest.raises(PollError) as limited:
        await sources.read(target, "UTC")
    assert limited.value.stop == "rate_limited"
