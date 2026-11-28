"""The brain's watch tools (spec §9.4, §12.4, ADR 0031): set one up, turn it on, list, cancel.

- **Setting one up needs the user's yes** (a page must never plant one, §12.4). Before asking,
  Enzo reads it once: the card shows what it reads now, and a check that fails asks nothing.
  It is saved off.
- **Turning it on is a second yes.** Before asking, Enzo reads it again (the dry run, recorded):
  the card shows what it reads now and what counts as a change. The yes covers that dry run, and
  the watch starts from it.
- Listing and cancelling need no approval, like the scheduled list.
"""

from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from enzo.browser.sites import SiteError, site_of
from enzo.hooks.rules import Rule, describe
from enzo.hooks.sources import (
    CalendarTarget,
    EmailTarget,
    PageTarget,
    PollError,
    PullRequestTarget,
    Target,
    check_selector,
    on_private_network,
    where,
)
from enzo.hooks.store import MIN_INTERVAL_S, Hook, HookRefusedError, every_words
from enzo.hooks.watcher import Watcher, threshold_of
from enzo.schedule.recurrence import zone
from enzo.schedule.tools import user_zone
from enzo.tools.registry import Prepared, Tool, plain_words, tool

ACTIVITY_CHARS = 60

_LISTS = ("email", "calendar")


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WatchParams(_Params):
    what: str = Field(
        min_length=3,
        max_length=120,
        description="What is watched, in the user's words: 'the price of the blue kettle'.",
    )
    source: Literal["page", "email", "calendar", "pull_request"] = Field(
        description=(
            "page: a web page; email: new emails matching a Gmail search; calendar: new or "
            "moved events; pull_request: a GitHub pull request's checks or comments."
        )
    )
    url: str | None = Field(default=None, max_length=2000, description="page: its http(s) address.")
    selector: str | None = Field(
        default=None,
        max_length=200,
        description="page: a CSS selector for the part to watch, like '.price'. Best for prices.",
    )
    text: str | None = Field(
        default=None,
        max_length=120,
        description=(
            "page: words on the line to watch, like 'Price:'; that line is what is compared. "
            "Without selector or text the whole page's main text is watched (it changes often)."
        ),
    )
    query: str | None = Field(
        default=None,
        max_length=300,
        description="email: a Gmail search, like 'from:airline subject:gate'. calendar: words.",
    )
    days: int = Field(default=7, ge=1, le=60, description="calendar: how many days ahead.")
    repo: str | None = Field(
        default=None, max_length=200, description="pull_request: 'owner/name' on GitHub."
    )
    number: int | None = Field(default=None, ge=1, description="pull_request: its number.")
    watch_for: Literal["checks", "comments"] = Field(
        default="checks", description="pull_request: its checks (CI), or new comments."
    )
    rule: Rule = Field(
        default="changes",
        description=(
            "What counts as a change: changes (any change), new (a new email, event or "
            "comment), above / below (the number in it crosses threshold), appears (match "
            "appears)."
        ),
    )
    threshold: float | None = Field(default=None, description="For above / below.")
    match: str | None = Field(
        default=None, max_length=120, description="For appears: the words to look for."
    )
    every_minutes: int = Field(
        ge=MIN_INTERVAL_S // 60,
        le=7 * 24 * 60,
        description="How often to check, in minutes. 15 to 60 suits most pages and inboxes.",
    )
    until: date | None = Field(default=None, description="The last local date it may check on.")
    times: int | None = Field(
        default=None,
        ge=1,
        le=100,
        description="Stop after this many changes acted on. 'Tell me when …' is 1.",
    )
    then: str = Field(
        min_length=3,
        max_length=300,
        description=(
            "What Enzo does when it changes, as an instruction: 'Tell me the new price and "
            "whether it is under $40.'"
        ),
    )
    local: bool = Field(default=False, description="Leave false; Enzo fills it in.")

    @model_validator(mode="after")
    def _fits(self) -> Self:
        problem = _problem(self)
        if problem:
            raise ValueError(problem)
        return self


def _problem(p: WatchParams) -> str | None:  # noqa: PLR0911 - one rule per line, in order
    if p.until is None and p.times is None:
        return "A watch needs an end: give until (a last date) or times. Ask the user if unsure."
    if p.source == "page":
        if not p.url:
            return "A page watch needs url."
        try:
            site_of(p.url)
        except SiteError as exc:
            return str(exc)
        if p.selector:
            check_selector(p.selector)
    if p.source in _LISTS and p.rule not in ("new", "appears"):
        return f"An {p.source} watch uses rule new (or appears with match)."
    if p.source == "email" and not p.query:
        return "An email watch needs query (a Gmail search)."
    if p.source == "pull_request" and (not p.repo or "/" not in p.repo or p.number is None):
        return "A pull request watch needs repo ('owner/name') and number."
    if p.rule == "new" and p.source not in (*_LISTS, "pull_request"):
        return "Rule new is for emails, events and comments: use changes for a page."
    if p.rule in ("above", "below") and p.threshold is None:
        return f"Rule {p.rule} needs threshold."
    if p.rule == "appears" and not p.match:
        return "Rule appears needs match (the words to look for)."
    return None


def target_of(p: WatchParams) -> Target:
    if p.source == "page":
        assert p.url is not None  # noqa: S101 - checked by the model
        return PageTarget(url=p.url, selector=p.selector, text=p.text, local=p.local)
    if p.source == "email":
        return EmailTarget(query=p.query or "")
    if p.source == "calendar":
        return CalendarTarget(query=p.query, days=p.days)
    owner, _, repo = (p.repo or "").partition("/")
    assert p.number is not None  # noqa: S101 - checked by the model
    return PullRequestTarget(owner=owner, repo=repo, number=p.number, watch=p.watch_for)


def _ends_at(until: date | None, tz: str) -> datetime | None:
    """The end of the last day, in the user's zone."""
    if until is None:
        return None
    return datetime.combine(until + timedelta(days=1), time(), tzinfo=zone(tz))


def plan_words(p: WatchParams) -> str:
    """The exact plan the card asks about."""
    limits = []
    if p.until:
        limits.append(f"until {p.until:%-d %b %Y}")
    if p.times:
        limits.append(f"at most {p.times} wake{'s' if p.times != 1 else ''}")
    change = describe(p.rule, threshold_of(p.threshold), p.match)
    return (
        f"Set up a watch (it starts off; turning it on asks you again): "
        f"{plain_words(p.what, 120)}. Enzo checks {where(target_of(p))} "
        f"{every_words(p.every_minutes * 60)}, {', '.join(limits)}, without using a model. "
        f"When {change}, Enzo will: {plain_words(p.then, 300)}"
    )


class WatchOut(BaseModel):
    id: int
    what: str
    #: off, on, stopped (it says why), done or cancelled.
    status: str
    checks: str
    change: str
    ends: str
    on_change: str
    #: What it read last (outside content), and when, in the user's zone.
    reads: str | None
    read_at: str | None
    next_check: str | None
    stop_reason: str | None
    last_error: str | None
    wakes: int


class Created(BaseModel):
    watch: WatchOut
    #: What it reads right now (outside content): show it to the user.
    reads_now: str
    note: str


class EnableParams(_Params):
    id: int = Field(ge=1, description="The number of the watch to turn on.")
    checked: str | None = Field(default=None, description="Leave empty; Enzo fills it in.")


class ListParams(_Params):
    pass


class WatchList(BaseModel):
    watches: list[WatchOut]


class CancelParams(_Params):
    id: int = Field(ge=1, description="The number of the watch to cancel.")


def watch_out(h: Hook) -> WatchOut:
    return WatchOut(
        id=h.id,
        what=h.what,
        status=h.status,
        checks=f"{h.where}, {h.every}",
        change=h.change_words,
        ends=h.ends,
        on_change=h.instruction,
        reads=h.shown,
        read_at=h.local(h.value_at),
        next_check=h.local(h.next_poll_at) if h.status == "on" else None,
        stop_reason=h.stop_reason,
        last_error=h.last_error,
        wakes=h.wakes,
    )


def watch_tools(
    watcher: Watcher, *, on_set: Callable[[], None] = lambda: None
) -> list[Tool[Any, Any]]:
    hooks = watcher.hooks

    async def prepare_watch(p: WatchParams) -> Prepared[WatchParams]:
        """Read it once before asking: a watch that cannot read anything is never offered."""
        local = p.source == "page" and await on_private_network(p.url or "")
        exact = p.model_copy(update={"local": local})
        target = target_of(exact)
        if isinstance(target, PageTarget) and watcher.sources.own_site(target.url):
            msg = "that is Enzo's own address, which a watch never reads"
            raise ValueError(msg)
        try:
            found = await watcher.read_now(target, user_zone(), p.rule, p.match)
        except PollError as exc:
            msg = f"Enzo tried to read it once and could not: {exc}"
            raise ValueError(msg) from None
        return Prepared(exact, detail=f"Right now it reads: {found.shown}")

    @tool(
        name="watch",
        description=(
            "Watch something for a change without spending a model call on each check: a "
            "part of a web page (a price, a line of text), new emails matching a search, new "
            "or moved calendar events, a GitHub pull request's checks or comments. Enzo checks "
            "it on its own every few minutes and only wakes up (a full turn, in its own chat) "
            "when it changes by the rule. It is saved off: then turn it on with enable_watch, "
            "which shows the user what it reads now. Needs an end (until or times)."
        ),
        requires_approval=True,
        summary=plan_words,
        activity=lambda p: f"Setting up a watch: {plain_words(p.what, ACTIVITY_CHARS)}",
        prepare=prepare_watch,
        timeout_s=90,
    )
    async def watch(p: WatchParams) -> Created:
        tz = user_zone()
        try:
            made = await hooks.create(
                what=p.what,
                target=target_of(p),
                rule=p.rule,
                instruction=p.then,
                interval_s=p.every_minutes * 60,
                timezone=tz,
                threshold=threshold_of(p.threshold),
                match=p.match,
                ends_at=_ends_at(p.until, tz),
                max_wakes=p.times,
            )
        except HookRefusedError as exc:
            raise ValueError(str(exc)) from None
        try:
            dry = await watcher.check(made.id)
            reads, saved = dry.seen.shown, dry.hook
        except (HookRefusedError, PollError) as exc:  # it is saved: turning it on checks again
            reads, saved = f"(could not read it just now: {exc})", made
        return Created(
            watch=watch_out(saved),
            reads_now=reads,
            note=(
                f"Saved off as watch #{made.id}. Tell the user in a line what it reads now, then "
                "call enable_watch to turn it on: they approve that on a card that shows it."
            ),
        )

    async def prepare_enable(p: EnableParams) -> Prepared[EnableParams]:
        """The dry run: read it now, record it, and pin the yes to exactly this reading."""
        try:
            dry = await watcher.check(p.id)
        except (HookRefusedError, PollError) as exc:
            msg = f"Enzo checked it now and could not read it: {exc}"
            raise ValueError(msg) from None
        h = dry.hook
        detail = (
            f"Right now it reads: {dry.seen.shown}\n"
            f"Checked {h.local(dry.at)}. It wakes Enzo when {h.change_words}; "
            "unchanged checks stay silent."
        )
        summary = (
            f"Turn on watch #{h.id}: {plain_words(h.what, 80)}. Enzo checks {h.where} {h.every}"
            f"{', ' + h.ends if h.ends else ''}. When {h.change_words}, Enzo will: "
            f"{plain_words(h.instruction, 200)}"
        )
        return Prepared(EnableParams(id=p.id, checked=dry.token), detail=detail, summary=summary)

    @tool(
        name="enable_watch",
        description=(
            "Turn on a watch that is off or stopped, after it was set up. Enzo checks it right "
            "now and the user approves on a card showing what it reads and what counts as a "
            "change."
        ),
        requires_approval=True,
        summary=lambda p: f"Turn on watch #{p.id}",
        activity=lambda p: f"Turning on watch #{p.id}",
        prepare=prepare_enable,
        timeout_s=90,
    )
    async def enable_watch(p: EnableParams) -> WatchOut:
        try:
            on = await hooks.enable(p.id, p.checked, watcher.now())
        except HookRefusedError as exc:
            raise ValueError(str(exc)) from None
        on_set()
        return watch_out(on)

    @tool(
        name="list_watches",
        description=(
            "List the user's watches (on, off or stopped) with what each last read, its "
            "number and why a stopped one stopped."
        ),
        untrusted_output=True,
        activity=lambda _p: "Looking at your watches",
    )
    async def list_watches(_p: ListParams) -> WatchList:
        return WatchList(watches=[watch_out(h) for h in await hooks.listed()])

    @tool(
        name="cancel_watch",
        description="Cancel a watch the user no longer wants. Find its number first.",
        activity=lambda _p: "Cancelling a watch",
    )
    async def cancel_watch(p: CancelParams) -> WatchOut:
        try:
            return watch_out(await hooks.cancel(p.id))
        except HookRefusedError as exc:
            raise ValueError(str(exc)) from None

    return [watch, enable_watch, list_watches, cancel_watch]
