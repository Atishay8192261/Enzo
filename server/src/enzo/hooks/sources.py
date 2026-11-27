"""What a watch reads on each check, with no model call (spec §9.4, ADR 0031).

- **A page:** fetched by Enzo itself with ``read_page``'s fetch and SSRF guard. The part watched
  is the first element matching a CSS selector, or the first line of the main text holding some
  words, or the whole main text. A page on this computer or the local network is read only when
  the user approved that exact site on the watch's card, and never Enzo's own port.
- **Email:** a Gmail search (read-only), compared by the emails it finds: a new one is a change.
- **Calendar:** the events in the next days (optionally matching words), compared by event, start
  and end: a new or moved event is a change.
- **A pull request:** its checks (running, passing, failing) or its comments (a new one).

Connector reads go through the connected account's read-only tools (the same rate-limit and
sign-in stops, §8.3). Every reading is outside content: shown to the brain fenced, never obeyed.
"""

import ipaddress
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

import httpx2
import lxml.etree
import lxml.html
from cssselect import GenericTranslator, SelectorError
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from enzo.browser.sites import SiteError, site_of, split_site
from enzo.connectors.mcp import ConnectorError, McpResult, Stop
from enzo.hooks.rules import Reading, contains, normalize
from enzo.schedule.recurrence import zone
from enzo.tools.registry import JsonArgs, Tool, plain_words
from enzo.tools.web import WebToolError, fetch_body, main_text, page_name, private_addresses

log = logging.getLogger("enzo.hooks")

#: Comments read per page and pages at most (GitHub's maximum page size is 100).
COMMENT_PAGE = 100
COMMENT_PAGES = 5
#: Problems GitHub reports for a check that did not pass.
_FAILED = {"failure", "timed_out", "cancelled", "action_required", "startup_failure", "stale"}


class PollError(Exception):
    """A check that could not read its value, in plain words for the user.

    ``stop``: the service or site said "too many requests" (a rate limit) or refused the sign-in.
    The watch stops then, and every other watch on the same site or service (§8.3).
    """

    def __init__(self, words: str, *, stop: Stop | None = None) -> None:
        super().__init__(words)
        self.stop = stop


class _Target(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PageTarget(_Target):
    kind: Literal["page"] = "page"
    url: str
    selector: str | None = None
    #: Words on the line to watch ("Price:"): the first line of the main text that holds them.
    text: str | None = None
    #: The page is on this computer or the local network, and the user approved that on the card.
    local: bool = False


class EmailTarget(_Target):
    kind: Literal["email"] = "email"
    query: str


class CalendarTarget(_Target):
    kind: Literal["calendar"] = "calendar"
    query: str | None = None
    days: int = 7


class PullRequestTarget(_Target):
    kind: Literal["pull_request"] = "pull_request"
    owner: str
    repo: str
    number: int
    watch: Literal["checks", "comments"] = "checks"


type Target = Annotated[
    PageTarget | EmailTarget | CalendarTarget | PullRequestTarget, Field(discriminator="kind")
]
TARGET: TypeAdapter[Target] = TypeAdapter(Target)


def scope_of(target: Target) -> str:
    """Who answers its checks: a rate limit there stops every watch of the same scope."""
    if isinstance(target, PageTarget):
        return f"site:{site_of(target.url)}"
    return {"email": "gmail", "calendar": "calendar", "pull_request": "github"}[target.kind]


def where(target: Target) -> str:
    """What is read, in the user's words: "the text at “.price” on shop.example/kettle"."""
    if isinstance(target, PageTarget):
        page = page_name(target.url)
        local = " (on this computer or your local network)" if target.local else ""
        if target.selector:
            return f"the text at “{plain_words(target.selector, 60)}” on {page}{local}"
        if target.text:
            return f"the line with “{plain_words(target.text, 60)}” on {page}{local}"
        return f"the page {page}{local}"
    if isinstance(target, EmailTarget):
        return f"your email for “{plain_words(target.query, 80)}”"
    if isinstance(target, CalendarTarget):
        days = f"the next {target.days} day{'s' if target.days != 1 else ''}"
        found = f", events matching “{plain_words(target.query, 60)}”" if target.query else ""
        return f"your calendar for {days}{found}"
    pr = f"{target.owner}/{target.repo} #{target.number}"
    return f"the checks on {pr}" if target.watch == "checks" else f"the comments on {pr}"


def check_selector(selector: str) -> None:
    """Refuse a selector that is not valid CSS, before anything is saved or asked."""
    try:
        GenericTranslator().css_to_xpath(selector)
    except SelectorError as exc:
        msg = f"“{selector}” is not a CSS selector the page can be searched with ({exc})"
        raise ValueError(msg) from None


async def on_private_network(url: str) -> bool:
    """The page's address is on this computer or the local network (the card must say so)."""
    parts = urlsplit(url)
    if not parts.hostname:
        return False
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return bool(await private_addresses(parts.hostname, port))


class Sources:
    """Reads a watch's value. ``tools`` finds a connected account's tool by name (connectors
    come and go with their accounts); ``own_port`` is the gateway's, never readable."""

    def __init__(
        self,
        *,
        http: httpx2.AsyncClient,
        tools: Callable[[str], Tool[Any, Any] | None],
        own_port: int | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._http = http
        self._tools = tools
        self._own_port = own_port
        self._clock = clock

    async def read(self, target: Target, timezone: str) -> Reading:
        if isinstance(target, PageTarget):
            return await self._page(target)
        if isinstance(target, EmailTarget):
            return await self._email(target)
        if isinstance(target, CalendarTarget):
            return await self._calendar(target, timezone)
        if target.watch == "checks":
            return await self._checks(target)
        return await self._comments(target)

    # ---------- pages ----------

    def own_site(self, url: str) -> bool:
        """Enzo's own gateway: a watch never reads it, whatever the user approved."""
        try:
            host, port = split_site(site_of(url))
        except SiteError:
            return False
        port = port or (443 if url.lower().startswith("https") else 80)
        if port != self._own_port:
            return False
        bare = host.strip("[]")
        if bare == "localhost" or bare.endswith(".localhost"):
            return True
        try:
            ip = ipaddress.ip_address(bare)
        except ValueError:
            return False
        return ip.is_loopback or ip.is_unspecified

    async def _page(self, target: PageTarget) -> Reading:
        if self.own_site(target.url):
            msg = "that is Enzo's own address, which a watch never reads"
            raise PollError(msg)
        approved = site_of(target.url)

        def private_ok(url: str) -> bool:
            # Only the exact site the user approved as local, never Enzo's own port.
            try:
                same = site_of(url) == approved
            except SiteError:
                return False
            return target.local and same and not self.own_site(url)

        try:
            got = await fetch_body(target.url, http=self._http, private_ok=private_ok)
        except WebToolError as exc:
            if exc.status == 429:
                msg = f"{approved} answered “too many requests” (HTTP 429)"
                raise PollError(msg, stop="rate_limited") from exc
            raise PollError(str(exc)) from exc
        if target.selector:
            if got.kind == "text/plain":
                msg = f"{page_name(got.url)} is plain text: give words on the line instead"
                raise PollError(msg)
            return Reading(_select(got.body, target.selector))
        try:
            _title, text = main_text(got.url, got.kind, got.body)
        except WebToolError as exc:
            raise PollError(str(exc)) from exc
        if target.text:
            return Reading(_line_with(text, target.text))
        return Reading(text)

    # ---------- connectors ----------

    async def _call(self, name: str, args: dict[str, Any], service: str) -> McpResult:
        tool = self._tools(name)
        login = "/login github" if service == "GitHub" else "/login google"
        if tool is None:
            msg = f"{service} is not connected (or this access was not granted): {login}"
            raise PollError(msg, stop="auth")
        if tool.requires_approval:  # a watch only ever reads
            msg = f"{service}: Enzo only reads during a watch, and this step would change something"
            raise PollError(msg)
        try:
            result = await tool.handler(JsonArgs(**args))
        except ConnectorError as exc:
            raise PollError(str(exc), stop=exc.stop) from exc
        if not isinstance(result, McpResult):
            msg = f"{service} sent an answer Enzo cannot read"
            raise PollError(msg)
        return result

    async def _email(self, target: EmailTarget) -> Reading:
        result = await self._call(
            "gmail_search_threads", {"query": target.query, "pageSize": 10}, "Gmail"
        )
        threads = _list(result, "threads", "Gmail")
        items: list[str] = []
        newest: dict[str, Any] | None = None
        for thread in threads:
            if not isinstance(thread, dict) or not thread.get("id"):
                continue
            items.append(f"thread:{thread['id']}")
            for message in thread.get("messages") or []:
                if isinstance(message, dict) and message.get("id"):
                    items.append(f"message:{message['id']}")
                    newest = newest or message
        count = sum(1 for i in items if i.startswith("thread:"))
        label = f"{count} matching email conversation{'s' if count != 1 else ''}"
        if newest is not None and newest.get("subject"):
            sender = plain_words(str(newest.get("sender") or "someone"), 60)
            label += f"; newest: “{plain_words(str(newest['subject']), 80)}” from {sender}"
        return Reading(label, items=tuple(items), label=label)

    async def _calendar(self, target: CalendarTarget, timezone: str) -> Reading:
        start = self._clock().astimezone(zone(timezone)).replace(tzinfo=None, microsecond=0)
        args: dict[str, Any] = {
            "startTime": start.isoformat(),
            "endTime": (start + timedelta(days=target.days)).isoformat(),
            "timeZone": timezone,
            "pageSize": 100,
        }
        if target.query:
            args["fullText"] = target.query
        events = _list(await self._call("calendar_list_events", args, "Google Calendar"), "events")
        items, upcoming = [], []
        for event in events:
            if not isinstance(event, dict) or not event.get("id"):
                continue
            begins, ends = _moment(event.get("start")), _moment(event.get("end"))
            status = event.get("status") or "confirmed"
            items.append(f"{event['id']}|{begins}|{ends}|{status}")
            upcoming.append((begins, plain_words(str(event.get("summary") or "(untitled)"), 60)))
        count = len(items)
        label = f"{count} event{'s' if count != 1 else ''} in the next {target.days} days"
        if upcoming:
            begins, title = min(upcoming)
            label += f"; next: “{title}”, {_when(begins)}"
        return Reading(label, items=tuple(sorted(items)), label=label)

    async def _checks(self, target: PullRequestTarget) -> Reading:
        where_ = {"owner": target.owner, "repo": target.repo, "pullNumber": target.number}
        name = "github_pull_request_read"
        runs = _object(
            await self._call(name, {"method": "get_check_runs", **where_, "perPage": 100}, "GitHub")
        )
        checks = [c for c in runs.get("check_runs") or [] if isinstance(c, dict)]
        if checks:
            state, label = _state_of_runs(checks)
            return Reading(state, label=label)
        status = _object(await self._call(name, {"method": "get_status", **where_}, "GitHub"))
        state = {"success": "passing", "failure": "failing", "error": "failing"}.get(
            str(status.get("state")), "running"
        )
        if state == "running" and not status.get("total_count"):
            return Reading("no checks", label="no checks have run yet")
        return Reading(state, label=f"checks {state}")

    async def _comments(self, target: PullRequestTarget) -> Reading:
        where_ = {"owner": target.owner, "repo": target.repo, "pullNumber": target.number}
        comments: list[dict[str, Any]] = []
        for page in range(1, COMMENT_PAGES + 1):
            args = {"method": "get_comments", **where_, "perPage": COMMENT_PAGE, "page": page}
            got = _list(await self._call("github_pull_request_read", args, "GitHub"), None)
            comments += [c for c in got if isinstance(c, dict) and c.get("id") is not None]
            if len(got) < COMMENT_PAGE:
                break
        items = tuple(f"comment:{c['id']}" for c in comments)
        count = len(comments)
        label = f"{count} comment{'s' if count != 1 else ''}"
        if comments:
            last = comments[-1]  # GitHub lists comments oldest first
            user = last.get("user")
            login = user.get("login") if isinstance(user, dict) else None
            body = plain_words(str(last.get("body") or ""), 100)
            label += f"; newest by {plain_words(str(login or 'someone'), 40)}: “{body}”"
        return Reading(label, items=items, label=label)


def _select(body: bytes, selector: str) -> str:
    """The text of the first element matching ``selector``; empty when there is none now."""
    try:
        doc = lxml.html.document_fromstring(body)
    except (ValueError, lxml.etree.ParserError) as exc:
        msg = "the page could not be read as HTML"
        raise PollError(msg) from exc
    try:
        found = doc.cssselect(selector)
    except SelectorError as exc:
        msg = f"“{selector}” is not a valid CSS selector"
        raise PollError(msg) from exc
    return normalize(found[0].text_content()) if found else ""


def _line_with(text: str, words: str) -> str:
    """The first line of the main text holding ``words``; empty when no line does now."""
    for line in text.splitlines():
        if contains(line, words):
            return normalize(line)
    return ""


def _payload(result: McpResult) -> Any:  # noqa: ANN401 - whatever the service sent
    """The service's JSON: its text, else its structured data (servers send one or both)."""
    try:
        return json.loads(result.text)
    except ValueError:
        if result.data is not None:
            return result.data
        msg = "the service's answer was not JSON"
        raise PollError(msg) from None


def _object(result: McpResult) -> dict[str, Any]:
    data = _payload(result)
    if not isinstance(data, dict):
        msg = "the answer was not in the shape Enzo expects"
        raise PollError(msg)
    return data


def _list(result: McpResult, key: str | None, service: str = "the service") -> list[Any]:
    """A list in the answer (under ``key``, or the answer itself); fails closed on any other
    shape, so a changed answer never looks like "nothing new"."""
    data = _payload(result)
    if key is not None and isinstance(data, dict):
        data = data.get(key, [] if _empty(result, key) else None)
    if not isinstance(data, list):
        msg = f"{service}'s answer was not in the shape Enzo expects"
        raise PollError(msg)
    return data


def _empty(result: McpResult, key: str) -> bool:
    """An answer with no matches may leave the list out: {} or only a page token."""
    data = _payload(result)
    return isinstance(data, dict) and key not in data and set(data) <= {"nextPageToken"}


def _moment(value: object) -> str:
    """An event's start or end as sent: {"dateTime": …} or {"date": …}, or a plain string."""
    if isinstance(value, dict):
        value = value.get("dateTime") or value.get("date")
    return str(value or "")


def _when(moment: str) -> str:
    try:
        at = datetime.fromisoformat(moment)
    except ValueError:
        return plain_words(moment, 30)
    return at.strftime("%a %-d %b %H:%M") if "T" in moment else at.strftime("%a %-d %b")


def _state_of_runs(checks: list[dict[str, Any]]) -> tuple[str, str]:
    """GitHub's check runs as one state: running, failing or passing, and a short label."""
    running = [c for c in checks if c.get("status") != "completed"]
    failed = [c for c in checks if c.get("conclusion") in _FAILED]
    total = len(checks)
    if failed:
        names = ", ".join(plain_words(str(c.get("name") or "?"), 30) for c in failed[:3])
        state, label = "failing", f"checks failing ({len(failed)} of {total}: {names})"
    elif running:
        state, label = "running", f"checks running ({total - len(running)} of {total} done)"
    else:
        state, label = "passing", f"checks passing ({total} of {total})"
    return state, label
