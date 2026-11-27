"""The browser steps the brain can take (spec §7.2.4 live browser task, ADR 0027).

The user approves a task once: what Enzo will do and the exact sites. Every later step checks the
live page first (``prepare``) and is pinned to what it saw; most run on their own, and the ones
that buy, send, delete, enter a password, card or one-time code, or leave the sites wait for their
own yes with the exact action (§12.3). Everything read from a page is fenced as untrusted (§12.4).
"""

import dataclasses
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field
from pydantic.json_schema import SkipJsonSchema

from enzo.browser.rules import (
    Element,
    click_risk,
    form_lines,
    looks_like_card,
    masked,
    secret_kind,
    type_risk,
)
from enzo.browser.session import (
    DEFAULT_CHARS,
    Browsers,
    BrowserToolError,
    PageView,
    Session,
    site_or_error,
)
from enzo.browser.sites import allowed, split_site
from enzo.context import brain_var, conversation_var, current_conversation
from enzo.images.store import ImageStore
from enzo.memory.guard import secret_reason
from enzo.tools.registry import ImageResult, Outgoing, Prepared, Tool, plain_words, tool
from enzo.tools.web import address_words, page_name, private_addresses

#: What every task's approval card says Enzo will still ask about.
ASKS_AGAIN = (
    "Enzo then clicks and types on its own. It asks you again before it buys or pays, sends or "
    "posts, deletes, enters a password, card or one-time code, or goes to another site. "
    "Esc stops it."
)

#: Told to the brain with every step tool: the card is the confirmation (#42).
STEP_NOTE = (
    "One step per reply: the page changes after each. A step that buys, sends, deletes, enters "
    "a password, card or code, or leaves the task's sites waits for the user's yes on a card: "
    "call it directly, never ask in chat first."
)

_REF = r"^[A-Za-z0-9]{1,16}$"


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StartParams(_Params):
    task: str = Field(
        min_length=3,
        max_length=300,
        description="What Enzo will do, in plain words: the user approves exactly this.",
    )
    sites: list[str] = Field(
        min_length=1,
        max_length=8,
        description=(
            "Every site the task needs, e.g. ['amazon.com', 'bestbuy.com']. A site covers its "
            "subdomains. Going anywhere else asks the user first."
        ),
    )
    url: str = Field(
        min_length=1, max_length=2000, description="The first page to open, on one of the sites."
    )


class OpenParams(_Params):
    url: str = Field(min_length=1, max_length=2000, description="The http(s) address to open.")
    #: Pinned by prepare: a site the user agreed to add to the task by approving this call.
    add_site: SkipJsonSchema[str | None] = None


class _RefParams(_Params):
    ref: str = Field(pattern=_REF, description="The element's ref from the latest page, e.g. e12.")
    element: str = Field(
        min_length=1,
        max_length=120,
        description="The element in a few words, as the page labels it: 'Add to cart button'.",
    )
    #: Pinned by prepare: the element (and, for a step that asks, the form) it was checked on.
    seen: SkipJsonSchema[str | None] = None
    #: Pinned by prepare: the element's label on the page, for the step's words.
    target: SkipJsonSchema[str | None] = None


class ClickParams(_RefParams):
    #: Pinned by prepare: the approved site a link leads to, outside the task's sites.
    leave_to: SkipJsonSchema[str | None] = None


class TypeParams(_RefParams):
    text: str = Field(max_length=4000, description="The text to type; it replaces what is there.")
    submit: bool = Field(default=False, description="Press Enter afterwards (submits the form).")
    #: Pinned by prepare: what the field asks for (label, name, autocomplete hint); None for
    #: an editor that takes free text, like a message box (ADR 0029).
    fills: SkipJsonSchema[str | None] = None


class SelectParams(_RefParams):
    option: str = Field(min_length=1, max_length=300, description="The option's label or value.")


class ScrollParams(_Params):
    direction: Literal["down", "up"] = Field(default="down", description="Which way to scroll.")


class ReadParams(_Params):
    max_chars: int = Field(
        default=DEFAULT_CHARS,
        ge=1000,
        le=40_000,
        description="Longest page text to return; the rest is cut.",
    )


class NoParams(_Params):
    pass


class Closed(BaseModel):
    closed: bool


class Screenshot(ImageResult):
    """What the page looks like: the image comes with the result (ADR 0033)."""

    site: str
    url: str
    title: str
    image: str
    width: int
    height: int


def browser_tools(  # noqa: PLR0915 - one closure per tool
    browsers: Browsers, images: ImageStore | None = None
) -> list[Tool[Any, Any]]:
    """The browser steps, bound to this gateway's browser. With ``images``, also a screenshot
    step, offered only to brains that can see (ADR 0027 left it out until images existed)."""

    def session() -> Session:
        return browsers.get(current_conversation())

    async def prepare_start(args: StartParams) -> Prepared[StartParams]:
        sites = list(dict.fromkeys(site_or_error(s) for s in args.sites))
        if urlsplit(args.url).scheme not in ("http", "https") or not allowed(args.url, sites):
            msg = "the first page must be an http(s) address on one of the task's sites"
            raise BrowserToolError(msg)
        lines = []
        for site in sites:
            local = await _local(site)
            if local and split_site(site)[1] == browsers.own_port:
                msg = f"{site} is Enzo itself; a browser task never opens Enzo's own gateway"
                raise BrowserToolError(msg)
            lines.append(
                f"  {site}" + (" (on this computer or your local network)" if local else "")
            )
        detail = "\n".join(
            ["Sites (and their subdomains):", *lines, f"First page: {args.url}", ASKS_AGAIN]
        )
        return Prepared(args.model_copy(update={"sites": sites}), detail=detail)

    @tool(
        name="browser_start",
        description=(
            "Start a browser task: use a real web browser for the user, for pages that need "
            "clicks, forms, a cart or a login (read_page is enough to just read a page). The "
            "user approves the task and its sites once on a card; then the other browser steps "
            "run on their own. Returns the first page with element refs."
        ),
        requires_approval=True,
        timeout_s=90,
        untrusted_output=True,
        summary=lambda a: plain_words(
            f"Use your browser to {a.task.rstrip('.')}, on {', '.join(a.sites)}", 200
        ),
        activity=lambda a: f"Opening {page_name(a.url)}",
        prepare=prepare_start,
        outbound=lambda a: [Outgoing(address_words(a.url), "address")],
    )
    async def browser_start(args: StartParams) -> PageView:
        conversation = current_conversation()
        if conversation is None:
            msg = "a browser task belongs to a chat"
            raise BrowserToolError(msg)
        started = await browsers.start(conversation, args.task, args.sites, args.url)
        async with started.lock:
            return await started.view()

    async def prepare_open(args: OpenParams) -> Prepared[OpenParams]:
        s = session()
        if urlsplit(args.url).scheme not in ("http", "https"):
            msg = "only http and https addresses can be opened"
            raise BrowserToolError(msg)
        site = site_or_error(args.url)
        if allowed(args.url, s.sites):
            return Prepared(args.model_copy(update={"add_site": None}))
        local = await _local(site)
        return Prepared(
            args.model_copy(update={"add_site": site}),
            summary=plain_words(
                f"Open {site} in the browser: it is not one of the sites approved for this task",
                200,
            ),
            detail=(
                f"Address: {args.url}\n"
                + ("It is on this computer or your local network.\n" if local else "")
                + "Approving adds it to this task's sites."
            ),
            ask=True,
        )

    @tool(
        name="browser_open",
        description=(
            "Open an address in the running browser task. A site outside the task's sites asks "
            "the user first."
        ),
        timeout_s=60,
        untrusted_output=True,
        activity=lambda a: f"Opening {page_name(a.url)}",
        prepare=prepare_open,
        # An address that carries a private detail asks first (§12.5, ADR 0029).
        summary=lambda a: plain_words(f"Open {a.url} in the browser", 200),
        outbound=lambda a: [Outgoing(address_words(a.url), "address")],
    )
    async def browser_open(args: OpenParams) -> PageView:
        s = session()
        async with s.lock:
            if args.add_site:
                s.allow(args.add_site)
            await s.goto(args.url)
            return await s.view()

    async def prepare_click(args: ClickParams) -> Prepared[ClickParams]:
        s = session()
        element = await s.inspect(args.ref)
        label = _label(element, args.element)
        leave = _leaves(element, s)
        pinned = args.model_copy(
            update={"seen": s.click_pin(element), "target": label, "leave_to": leave}
        )
        if leave is not None:
            return Prepared(
                pinned,
                summary=plain_words(
                    f"Click “{label}” on {s.site}: it goes to {leave}, which is not one of the "
                    "sites approved for this task",
                    200,
                ),
                detail=f"Link: {element.href}\nApproving adds {leave} to this task's sites.",
                ask=True,
            )
        if why := click_risk(element):
            detail = _form_detail(element) if element.submits and element.form_fields else None
            return Prepared(
                pinned,
                summary=plain_words(f"Click “{label}” on {s.site}: {why}", 200),
                detail=detail,
                ask=True,
            )
        return Prepared(pinned)

    @tool(
        name="browser_click",
        description=f"Click an element of the page by its ref. {STEP_NOTE}",
        timeout_s=60,
        untrusted_output=True,
        activity=lambda a: f"Clicking “{plain_words(a.target or a.element, 60)}”",
        prepare=prepare_click,
    )
    async def browser_click(args: ClickParams) -> PageView:
        s = session()
        async with s.lock:
            await s.check_pin(args.ref, args.seen, s.click_pin)
            if args.leave_to:
                s.allow(args.leave_to)
            locator = s.page.locator(f"aria-ref={args.ref}")
            await s.act(locator.click, f"could not click “{args.target or args.element}”")
            return await s.view()

    async def prepare_type(args: TypeParams) -> Prepared[TypeParams]:
        s = session()
        element = await s.inspect(args.ref)
        if element.field is None and not element.editable:
            msg = f"{args.ref} is not a field that takes text; type into a text field"
            raise BrowserToolError(msg)
        label = _label(element, args.element)
        pinned = args.model_copy(
            update={
                "seen": s.type_pin(element, args.text, submit=args.submit),
                "target": label,
                "fills": _fills(element),
            }
        )
        why = type_risk(element, args.text, submit=args.submit)
        if why is None:
            return Prepared(pinned)
        what = _typed_words(element, args.text)
        enter = " and press Enter" if args.submit else ""
        detail = None
        if args.submit and element.form_fields:
            typed = element.model_copy(
                update={
                    "form_fields": tuple(
                        f.model_copy(update={"value": args.text}) if f == element.field else f
                        for f in element.form_fields
                    )
                }
            )
            detail = _form_detail(typed)
        return Prepared(
            pinned,
            summary=plain_words(f"Type {what} into “{label}” on {s.site}{enter}: {why}", 200),
            detail=detail,
            ask=True,
        )

    @tool(
        name="browser_type",
        description=(
            "Type into a field of the page by its ref (replacing what is there); submit presses "
            "Enter. Give a site only what it needs for the task. Never ask the user for a "
            f"password in chat: they can sign in themselves in the browser window. {STEP_NOTE}"
        ),
        timeout_s=60,
        untrusted_output=True,
        activity=lambda a: (
            f"Typing into “{plain_words(a.target or a.element, 60)}”"
            + (" and pressing Enter" if a.submit else "")
        ),
        prepare=prepare_type,
        # What is typed goes to the site: a private detail the field does not ask for asks
        # first (§12.5, ADR 0029). The card's words never spell out a secret.
        summary=lambda a: plain_words(
            f"Type {_typed_text(a.text)} into “{a.target or a.element}”"
            + (" and press Enter" if a.submit else ""),
            200,
        ),
        outbound=lambda a: [
            Outgoing(a.text, "field" if a.fills is not None else "message", field=a.fills)
        ],
    )
    async def browser_type(args: TypeParams) -> PageView:
        s = session()
        async with s.lock:
            await s.check_pin(
                args.ref, args.seen, lambda e: s.type_pin(e, args.text, submit=args.submit)
            )
            locator = s.page.locator(f"aria-ref={args.ref}")
            what = f"could not type into “{args.target or args.element}”"
            await s.act(lambda: locator.fill(args.text), what)
            if args.submit:
                await s.act(lambda: locator.press("Enter"), what)
            return await s.view()

    async def prepare_select(args: SelectParams) -> Prepared[SelectParams]:
        s = session()
        element = await s.inspect(args.ref)
        if element.field is None or element.field.tag != "select":
            msg = f"{args.ref} is not a list of options; click it instead"
            raise BrowserToolError(msg)
        label = _label(element, args.element)
        return Prepared(
            args.model_copy(update={"seen": s.pin(element, form=False), "target": label})
        )

    @tool(
        name="browser_select",
        description=f"Choose an option in a drop-down list of the page by its ref. {STEP_NOTE}",
        timeout_s=60,
        untrusted_output=True,
        activity=lambda a: (
            f"Choosing “{plain_words(a.option, 40)}” in “{plain_words(a.target or a.element, 40)}”"
        ),
        prepare=prepare_select,
    )
    async def browser_select(args: SelectParams) -> PageView:
        s = session()
        async with s.lock:
            await s.check_pin(args.ref, args.seen, lambda e: s.pin(e, form=False))
            locator = s.page.locator(f"aria-ref={args.ref}")
            what = f"could not choose “{args.option}”"

            async def choose() -> None:
                try:
                    await locator.select_option(label=args.option)
                except Exception:
                    await locator.select_option(value=args.option)

            await s.act(choose, what)
            return await s.view()

    @tool(
        name="browser_scroll",
        description="Scroll the page, so it loads more of itself.",
        timeout_s=30,
        untrusted_output=True,
        activity=_scrolling,
    )
    async def browser_scroll(args: ScrollParams) -> PageView:
        s = session()
        async with s.lock:
            dy = 800 if args.direction == "down" else -800
            await s.act(lambda: s.page.mouse.wheel(0, dy), "could not scroll")
            return await s.view()

    @tool(
        name="browser_back",
        description="Go back to the previous page of the browser task.",
        timeout_s=60,
        untrusted_output=True,
        activity=lambda _a: "Going back a page",
    )
    async def browser_back(_args: NoParams) -> PageView:
        s = session()
        async with s.lock:

            async def back() -> None:
                await s.page.go_back()

            await s.act(back, "could not go back")
            return await s.view()

    @tool(
        name="browser_read",
        description="Read the browser task's current page again, e.g. more of a long page.",
        timeout_s=30,
        untrusted_output=True,
        activity=lambda _a: "Reading the page",
    )
    async def browser_read(args: ReadParams) -> PageView:
        s = session()
        async with s.lock:
            return await s.view(args.max_chars)

    @tool(
        name="browser_close",
        description="End the browser task once it is done (it also ends with your reply).",
        timeout_s=30,
        activity=lambda _a: "Closing the browser",
    )
    async def browser_close(_args: NoParams) -> Closed:
        conversation = current_conversation()
        closed = conversation is not None and await browsers.close(conversation)
        return Closed(closed=closed)

    steps: list[Tool[Any, Any]] = [
        browser_start,
        browser_open,
        browser_click,
        browser_type,
        browser_select,
        browser_scroll,
        browser_back,
        browser_read,
        browser_close,
    ]
    if images is None:
        return steps

    @tool(
        name="browser_screenshot",
        description=(
            "See the browser task's current page as an image (what is on screen), when the "
            "layout, a picture or a chart matters. Password, card and code fields are blacked "
            "out. Use browser_read for the page's text and element refs."
        ),
        timeout_s=30,
        untrusted_output=True,
        activity=lambda _a: "Looking at the page",
    )
    async def browser_screenshot(_args: NoParams) -> Screenshot:
        s = session()
        async with s.lock:
            png = await s.screenshot()
            url, title = s.page.url, await s.page.title()
        kept = await images.keep_async(
            png,
            source="screenshot",
            tool="browser_screenshot",
            brain=brain_var.get(),
            chat=conversation_var.get(),
        )
        return Screenshot(
            site=s.site,
            url=url,
            title=title,
            image=kept.id,
            width=kept.width,
            height=kept.height,
            images=(kept.part(),),
        )

    return [*steps, dataclasses.replace(browser_screenshot, needs_vision=True)]


def _scrolling(args: ScrollParams) -> str:
    return f"Scrolling {args.direction}"


def _label(element: Element, said: str) -> str:
    """The element as the page labels it; the brain's words when the page gives none."""
    return plain_words(element.label or said, 60)


def _leaves(element: Element, s: Session) -> str | None:
    """The site a link goes to, when it is outside the task's sites."""
    href = element.href
    if not href or urlsplit(href).scheme not in ("http", "https") or allowed(href, s.sites):
        return None
    return site_or_error(href)


def _typed_words(element: Element, text: str) -> str:
    """What is about to be typed, as the card says it: secrets are never spelled out."""
    kind = secret_kind(element.field) if element.field else None
    if kind == "password":
        return "a password"
    if kind == "card" or looks_like_card(text):
        shown = masked("card", text)
        return f"payment card details ({shown})" if "ending" in shown else "payment card details"
    if kind == "code":
        return "a one-time code"
    return f"“{plain_words(text, 80)}”"


def _typed_text(text: str) -> str:
    """Typed text as a card says it, when the field itself is not a secret one."""
    if secret_reason(text) or looks_like_card(text):
        return "text that looks like a password, key or card number"
    return f"“{plain_words(text, 120)}”"


def _fills(element: Element) -> str | None:
    """What a field asks for, in the page's words; None for a free-text editor."""
    f = element.field
    if f is None:
        return None
    words = (f.label, f.name, f.id, f.placeholder, f.autocomplete, f.type)
    return " ".join(w for w in words if w) or "field"


def _form_detail(element: Element) -> str:
    """What a form will send, field by field, for the approval card (§12.3, §12.5)."""
    to = element.form_action or element.page_url
    return "\n".join([f"This sends to {page_name(to)}:", *form_lines(element.form_fields)])


async def _local(site: str) -> bool:
    """The site is on this computer or the user's local network."""
    host, port = split_site(site)
    return bool(await private_addresses(host, port or 80))
