"""A real browser that Enzo drives, one task at a time, in its own profile (ADR 0027).

- **Which browser:** the user's installed Google Chrome (or Microsoft Edge, or Chromium), found on
  disk and driven with Playwright; ``ENZO_BROWSER_PATH`` names another. No browser is downloaded.
- **Its own profile** in Enzo's data folder, with the password manager and autofill switched off,
  so Enzo never types a saved password or card. The user's own logged-in profile is never used.
- **Sites:** the task's approved sites only. Leaving them is blocked in the page and asks the
  user; requests into the user's private network are blocked unless the task names that address.
- **Lifetime:** a task lives as long as its turn. It closes when the turn ends, when the user stops
  it (Esc), when the gateway shuts down, and leftovers from a crash are closed at the next start.
  A task waiting for the user's approval keeps its page open for ``IDLE_CLOSE_S``.
"""

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import secrets
import sys
import uuid
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import psutil
from playwright.async_api import (
    BrowserContext,
    Dialog,
    Download,
    FileChooser,
    Page,
    Playwright,
    Request,
    Route,
    WebSocketRoute,
    async_playwright,
)
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from pydantic import BaseModel

from enzo.browser.rules import (
    ELEMENT_JS,
    FIELD_JS,
    Element,
    Field,
    click_risk,
    looks_like_card,
    masked,
    secret_kind,
    type_risk,
)
from enzo.browser.sites import allowed, host_words, site_of
from enzo.context import BrowserInUse, notify
from enzo.paths import private_dir
from enzo.tools.registry import ToolStoppedError, plain_words
from enzo.tools.web import private_addresses

log = logging.getLogger("enzo.browser")

#: A task waiting for the user's yes keeps its page this long, then closes.
IDLE_CLOSE_S = 600.0
ACTION_TIMEOUT_MS = 15_000
NAVIGATION_TIMEOUT_MS = 30_000
#: After a step, wait this long at most for the page to finish loading what it started.
SETTLE_MS = 2_000
#: A ref that does not exist fails this fast, instead of after the action timeout.
FIND_TIMEOUT_MS = 2_000
DEFAULT_CHARS = 10_000
#: Fields read per page to hide secrets; values of fields beyond this are hidden unread.
MAX_FIELDS = 120

_FIELD_LINE = re.compile(
    r"^(?P<head>\s*- (?P<role>textbox|searchbox|spinbutton|combobox)\b[^\n]*?"
    r"\[ref=(?P<ref>[A-Za-z0-9]+)\][^:\n]*?)(?P<value>: .*)?$"
)
_CARD_TEXT = re.compile(r"\b\d(?:[ -]?\d){12,18}\b")
_KIND_WORDS = {"password": "password field", "card": "card field", "code": "one-time code field"}


class BrowserToolError(Exception):
    """A browser step that could not be done, in plain words for the brain to relay or fix."""


class PageView(BaseModel):
    """What the brain sees after each step: the page as an accessibility snapshot.

    ``page`` lists the page's elements with references like ``[ref=e5]``: steps act on a ref.
    Passwords, card numbers and one-time codes are never shown, only that a field holds one.
    """

    site: str
    url: str
    title: str
    page: str
    truncated: bool
    #: What happened along the way: a visit that was blocked, a dialog the page showed.
    notes: list[str] = []


@dataclass(frozen=True, slots=True)
class Launch:
    """The browser to drive: an executable found on disk, or Playwright's own Chromium."""

    label: str
    executable: Path | None


def find_browser(explicit: Path | None = None) -> Launch | None:
    """The browser Enzo will drive, or None when none is installed where Enzo looks."""
    if explicit is not None:
        return Launch("the browser in ENZO_BROWSER_PATH", explicit) if explicit.exists() else None
    for label, path in _candidates():
        if path.exists():
            return Launch(label, path)
    return None


def _candidates() -> Iterator[tuple[str, Path]]:
    if sys.platform == "darwin":
        for root in (Path("/Applications"), Path.home() / "Applications"):
            yield "Google Chrome", root / "Google Chrome.app/Contents/MacOS/Google Chrome"
            yield "Microsoft Edge", root / "Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
            yield "Chromium", root / "Chromium.app/Contents/MacOS/Chromium"
        return
    yield "Google Chrome", Path("/opt/google/chrome/chrome")
    yield "Microsoft Edge", Path("/opt/microsoft/msedge/msedge")
    yield "Chromium", Path("/usr/bin/chromium")
    yield "Chromium", Path("/usr/bin/chromium-browser")


def has_display() -> bool:
    """A window can be shown: a Mac, or Linux with a desktop session."""
    return sys.platform == "darwin" or bool(
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    )


#: Chrome preferences for Enzo's profile: never offer, save or fill passwords, cards or addresses.
PROFILE_PREFS: dict[str, Any] = {
    "credentials_enable_service": False,
    "profile": {"password_manager_enabled": False},
    "autofill": {"profile_enabled": False, "credit_card_enabled": False},
}


def prepare_profile(profile: Path) -> None:
    """Create the profile (user-only) and switch Chrome's password manager and autofill off."""
    prefs = private_dir(profile / "Default") / "Preferences"
    try:
        current = json.loads(prefs.read_text(encoding="utf-8")) if prefs.exists() else {}
    except (OSError, ValueError):
        current = {}  # Chrome rewrites a broken file; ours starts it over
    prefs.write_text(json.dumps(_merge(current, PROFILE_PREFS)), encoding="utf-8")
    private_dir(profile)


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


@dataclass(eq=False)
class Session:
    """One browser task for one chat: its approved sites, the open page, and what happened."""

    conversation: uuid.UUID
    task: str
    sites: list[str]
    context: BrowserContext
    page: Page
    own_port: int | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    notes: list[str] = field(default_factory=list)
    closed: bool = False
    #: Closed because the user stopped it (Esc): a step cut off by that is "stopped".
    stopped: bool = False
    #: Pins are hashed with this, so a pin never shows what a form holds.
    salt: bytes = field(default_factory=lambda: secrets.token_bytes(16))
    _private: dict[str, bool] = field(default_factory=dict)

    @property
    def site(self) -> str:
        return host_words(self.page.url)

    def allow(self, site: str) -> None:
        """Add a site the user approved during the task (they said yes to going there)."""
        if site not in self.sites:
            self.sites.append(site)

    def note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)

    # -- what the page is and what a step would do ------------------------------------------

    def on_allowed_page(self) -> bool:
        url = self.page.url
        return url == "about:blank" or allowed(url, self.sites)

    async def inspect(self, ref: str) -> Element:
        """What the page says about the element ``ref`` points at (for gates and pins)."""
        self.require_open()
        if not self.on_allowed_page():
            msg = f"the page is on {self.site}, which is not an approved site; open one first"
            raise BrowserToolError(msg)
        try:
            data = await self.page.locator(f"aria-ref={ref}").evaluate(
                ELEMENT_JS, timeout=FIND_TIMEOUT_MS
            )
        except PlaywrightError as exc:
            raise self.failed(exc, f"there is no element {ref} on the page now") from exc
        return Element.model_validate(data)

    def pin(self, element: Element, *, form: bool, skip: Field | None = None) -> str:
        """What a step was checked against: the element, its page and, for a submit, the form's
        exact content. The step runs only if the page still matches (§12.3: changed content asks
        again). Hashed with a per-task salt: a pin never shows what a form holds."""
        target = element.model_dump(exclude={"form_fields", "field"})
        target["page_url"] = target["page_url"].split("#", 1)[0]
        if element.field is not None:
            target["field"] = element.field.model_dump(exclude={"value"})
        if form:
            target["form"] = [f.model_dump() for f in element.form_fields if f != skip]
        raw = json.dumps(target, sort_keys=True).encode()
        return hashlib.sha256(self.salt + raw).hexdigest()[:24]

    def click_pin(self, element: Element) -> str:
        """A click that asks first is pinned with its form's content; a plain one is not, so
        filling a field and then clicking "Add to cart" never needs a second look."""
        return self.pin(element, form=element.submits and click_risk(element) is not None)

    def type_pin(self, element: Element, text: str, *, submit: bool) -> str:
        risky = submit and type_risk(element, text, submit=True) is not None
        return self.pin(element, form=risky, skip=element.field)

    async def check_pin(self, ref: str, pin: str | None, pin_of: Callable[[Element], str]) -> None:
        """Refuse a step whose element or form changed since it was checked (or approved)."""
        current = await self.inspect(ref)
        if pin is None or pin != pin_of(current):
            msg = (
                "the page changed since this step was checked, so it was not done; "
                "look at the page again and retry"
            )
            raise BrowserToolError(msg)

    # -- acting -------------------------------------------------------------------------------

    async def goto(self, url: str) -> None:
        self.require_open()
        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
        except PlaywrightError as exc:
            if self.closed:
                raise self.ended() from exc
            if "ERR_BLOCKED_BY_CLIENT" in str(exc) or "net::ERR_ABORTED" in str(exc):
                await self.settle()  # the guard blocked it and left a note
                return
            raise self.failed(exc, f"could not open {host_words(url)}") from exc

    async def act(self, step: Callable[[], Awaitable[None]], what: str) -> None:
        """Run one step on the page, then wait for what it started to load."""
        self.require_open()
        try:
            await step()
        except PlaywrightError as exc:
            raise self.failed(exc, what) from exc
        await self.settle()

    async def settle(self) -> None:
        with contextlib.suppress(PlaywrightError):
            await self.page.wait_for_load_state("domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
        with contextlib.suppress(PlaywrightError):
            await self.page.wait_for_load_state("networkidle", timeout=SETTLE_MS)
        if self.page.url.startswith("chrome-error://"):
            # A blocked visit (or one that failed) leaves Chrome's error page: go back to the
            # page before it, so the next step starts from a real page.
            if not any(n.startswith("Blocked going to") for n in self.notes):
                self.note("The next page did not load; Enzo went back to the page before.")
            with contextlib.suppress(PlaywrightError):
                await self.page.go_back(wait_until="domcontentloaded")

    async def view(self, max_chars: int = DEFAULT_CHARS) -> PageView:
        """The page as the brain sees it, with secrets hidden and big pages cut (§5.2)."""
        self.require_open()
        notify(BrowserInUse(self.site))
        notes, self.notes = self.notes, []
        url = self.page.url
        if not self.on_allowed_page():
            notes.append(
                f"The page went to {self.site}, which is not one of the approved sites, so Enzo "
                "did not read it. Open an approved page, or ask to go there."
            )
            return PageView(
                site=self.site, url=url, title="", page="", truncated=False, notes=notes
            )
        try:
            raw = await self.page.aria_snapshot(mode="ai", timeout=ACTION_TIMEOUT_MS)
            title = await self.page.title()
        except PlaywrightError as exc:
            raise self.failed(exc, "could not read the page") from exc
        text = hide_card_numbers(await self._hide_secrets(raw))
        cut = len(text) > max_chars
        if cut:
            text = text[: text.rfind("\n", 0, max_chars) + 1 or max_chars]
            notes.append("The page is long and was cut: read more with a larger max_chars.")
        return PageView(site=self.site, url=url, title=title, page=text, truncated=cut, notes=notes)

    async def _hide_secrets(self, snapshot: str) -> str:
        """Each field's value is shown only if it is not a password, card or one-time code; a
        field that cannot be read is shown without its value."""
        lines = snapshot.split("\n")
        found = [(i, m) for i, line in enumerate(lines) if (m := _FIELD_LINE.match(line))]
        kinds = await asyncio.gather(
            *(self._field(m["ref"]) for _, m in found[:MAX_FIELDS]),
            return_exceptions=True,
        )
        for n, (i, m) in enumerate(found):
            got = kinds[n] if n < len(kinds) else None
            if isinstance(got, BaseException) or got is None:
                if m["value"]:
                    lines[i] = f"{m['head']}: (value not shown)"
                continue
            kind, value = got
            if kind is not None:
                lines[i] = f"{m['head']} [{_KIND_WORDS[kind]}]: {masked(kind, value)}"
        return "\n".join(lines)

    async def _field(self, ref: str) -> tuple[Literal["password", "card", "code"] | None, str]:
        data = await self.page.locator(f"aria-ref={ref}").evaluate(
            FIELD_JS, timeout=FIND_TIMEOUT_MS
        )
        found = Field.model_validate(data)
        return secret_kind(found), found.value

    # -- guards -------------------------------------------------------------------------------

    async def guard(self, route: Route) -> None:
        """Every request the page makes: no leaving the approved sites in the main window, and
        nothing into the user's private network unless the task names that address. A check
        that fails blocks the request (fail closed)."""
        try:
            await self._guard(route)
        except Exception:
            log.exception("browser.guard failed")
            await _abort(route)

    async def _guard(self, route: Route) -> None:
        request = route.request
        url = request.url
        if self.closed:
            await _abort(route)
            return
        if await self._is_own_gateway(url):
            self.note("Blocked a request to Enzo itself: pages never reach Enzo's own gateway.")
            await _abort(route)
            return
        if _top_navigation(request) and not allowed(url, self.sites):
            self.note(
                f"Blocked going to {host_words(url)}: it is not one of the sites approved for "
                "this task. To go there, open it: the user is asked first."
            )
            await _abort(route)
            return
        if not allowed(url, self.sites) and await self._is_private(url):
            self.note(f"Blocked a request to {host_words(url)}, inside a private network.")
            await _abort(route)
            return
        with contextlib.suppress(PlaywrightError):
            await route.continue_()

    async def guard_socket(self, socket: WebSocketRoute) -> None:
        """WebSockets get the same private-network guard as other requests."""
        url = _as_web(socket.url)
        blocked = self.closed or await self._is_own_gateway(url)
        if not blocked and not allowed(url, self.sites) and await self._is_private(url):
            self.note(f"Blocked a connection to {host_words(url)}, inside a private network.")
            blocked = True
        if blocked:
            with contextlib.suppress(PlaywrightError):
                await socket.close()
            return
        socket.connect_to_server()

    async def _is_private(self, url: str) -> bool:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https", "ws", "wss"):
            return False
        host = parts.hostname or ""
        port = parts.port or (443 if parts.scheme in ("https", "wss") else 80)
        key = f"{host}:{port}"
        if key not in self._private:
            self._private[key] = bool(await private_addresses(host, port))
        return self._private[key]

    async def _is_own_gateway(self, url: str) -> bool:
        """Enzo's own gateway listens on this machine: no page may reach it, approved or not."""
        if self.own_port is None:
            return False
        parts = urlsplit(url)
        return parts.port == self.own_port and await self._is_private(url)

    def watch(self, page: Page) -> None:
        """Handle what a page may throw at Enzo: dialogs, file pickers, downloads."""
        page.on("dialog", self._dialog)
        page.on("filechooser", self._file_chooser)
        page.on("download", self._download)
        page.set_default_timeout(ACTION_TIMEOUT_MS)
        page.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)

    async def _dialog(self, dialog: Dialog) -> None:
        # "Leave this page?", "Delete everything?": the safe answer is no, and the brain is told.
        self.note(f"The page showed a dialog, answered no: {plain_words(dialog.message, 160)}")
        with contextlib.suppress(PlaywrightError):
            await dialog.dismiss()

    def _file_chooser(self, _chooser: FileChooser) -> None:
        self.note("The page asked for a file to upload; Enzo does not upload files.")

    def _download(self, download: Download) -> None:
        name = plain_words(download.suggested_filename, 60)
        self.note(f"The page started a download ({name}); Enzo does not keep downloads.")

    def on_new_page(self, page: Page) -> None:
        """A link or the page opened a new tab: Enzo follows it there."""
        self.watch(page)
        self.page = page
        self.note("The page opened a new tab; Enzo is on it now. Go back to return.")

    # -- endings ------------------------------------------------------------------------------

    def require_open(self) -> None:
        if self.closed:
            raise self.ended()

    def ended(self) -> Exception:
        if self.stopped:
            return ToolStoppedError("Stopped: the user stopped the browser task.")
        return BrowserToolError("the browser task has ended; start a new one to continue")

    def failed(self, exc: PlaywrightError, what: str) -> Exception:
        """A Playwright failure in plain words: a stop is a stop, a timeout says so."""
        if self.closed:
            return self.ended()
        if isinstance(exc, PlaywrightTimeoutError):
            return BrowserToolError(f"{what}: the page did not respond in time")
        first = str(exc).strip().split("\n", 1)[0]
        return BrowserToolError(f"{what}: {plain_words(first, 200)}")


def hide_card_numbers(text: str) -> str:
    """Card numbers shown as page text are cut to their last four digits."""

    def cut(m: re.Match[str]) -> str:
        found = m.group(0)
        return f"•••• {re.sub(r'\D', '', found)[-4:]}" if looks_like_card(found) else found

    return _CARD_TEXT.sub(cut, text)


def _as_web(url: str) -> str:
    """``ws://`` and ``wss://`` addresses checked as the ``http``/``https`` sites they are on."""
    if url.startswith("ws://"):
        return f"http://{url[5:]}"
    if url.startswith("wss://"):
        return f"https://{url[6:]}"
    return url


def _top_navigation(request: Request) -> bool:
    try:
        return request.is_navigation_request() and request.frame.parent_frame is None
    except PlaywrightError:  # a request without a frame (a worker): not a page visit
        return False


async def _abort(route: Route) -> None:
    with contextlib.suppress(PlaywrightError):
        await route.abort("blockedbyclient")


class Browsers:
    """The browser tasks of this gateway: at most one at a time (one profile, one window)."""

    def __init__(
        self,
        profile: Path,
        *,
        executable: Path | None = None,
        headless: bool | None = None,
        own_port: int | None = None,
        idle_close_s: float = IDLE_CLOSE_S,
        sandbox: bool | None = None,
    ) -> None:
        self.profile = profile
        self._executable = executable
        self._headless = headless
        self._own_port = own_port
        self._idle_close_s = idle_close_s
        self._sandbox = sandbox
        self._pw: Playwright | None = None
        self._sessions: dict[uuid.UUID, Session] = {}
        self._idle: dict[uuid.UUID, asyncio.TimerHandle] = {}
        self._closing: set[asyncio.Task[bool]] = set()
        self._starting = asyncio.Lock()

    @property
    def sandbox(self) -> bool:
        """Chrome's sandbox stays on (Playwright turns it off by default); Linux cannot sandbox
        a browser run as root."""
        if self._sandbox is not None:
            return self._sandbox
        return not (hasattr(os, "geteuid") and os.geteuid() == 0)

    @property
    def own_port(self) -> int | None:
        return self._own_port

    @property
    def headless(self) -> bool:
        return self._headless if self._headless is not None else not has_display()

    def get(self, conversation: uuid.UUID | None) -> Session:
        session = self._sessions.get(conversation) if conversation else None
        if session is None:
            msg = "no browser task is running in this chat; start one first"
            raise BrowserToolError(msg)
        self._cancel_idle(session.conversation)
        return session

    def active(self, conversation: uuid.UUID) -> bool:
        return conversation in self._sessions

    async def start(
        self, conversation: uuid.UUID, task: str, sites: list[str], url: str
    ) -> Session:
        """Open the browser for an approved task. A new task in the same chat replaces the old."""
        approved = list(dict.fromkeys(site_or_error(site) for site in sites))
        async with self._starting:
            await self.close(conversation, why="replaced")
            other = next(iter(self._sessions.values()), None)
            if other is not None:
                msg = "the browser is busy with a task in another chat; try again when it is done"
                raise BrowserToolError(msg)
            context = await self._launch()
            page = context.pages[0] if context.pages else await context.new_page()
            session = Session(conversation, task, approved, context, page, self._own_port)
            session.watch(page)
            context.on("page", session.on_new_page)
            await context.route("**/*", session.guard)
            await context.route_web_socket("**/*", session.guard_socket)
            self._sessions[conversation] = session
        log.info("browser.start", extra={"sites": len(sites), "headless": self.headless})
        notify(BrowserInUse(host_words(url)))  # the sign shows while the first page loads
        await session.goto(url)
        return session

    async def _launch(self) -> BrowserContext:
        launch = find_browser(self._executable)
        if self._executable is not None and launch is None:
            msg = f"ENZO_BROWSER_PATH points at {self._executable}, which does not exist"
            raise BrowserToolError(msg)
        if self._pw is None:
            self._pw = await async_playwright().start()
        prepare_profile(self.profile)
        try:
            return await self._pw.chromium.launch_persistent_context(
                str(self.profile),
                executable_path=launch.executable if launch else None,
                headless=self.headless,
                chromium_sandbox=self.sandbox,
                accept_downloads=False,
                # Service workers would answer requests the guard never sees.
                service_workers="block",
                no_viewport=not self.headless,
                timeout=NAVIGATION_TIMEOUT_MS,
            )
        except PlaywrightError as exc:
            if launch is None:
                msg = (
                    "no browser was found: install Google Chrome, or set ENZO_BROWSER_PATH to a "
                    "Chrome, Edge or Chromium executable"
                )
                raise BrowserToolError(msg) from exc
            first = str(exc).strip().split("\n", 1)[0]
            msg = f"could not start {launch.label}: {plain_words(first, 200)}"
            raise BrowserToolError(msg) from exc

    async def close(self, conversation: uuid.UUID, *, why: str = "done") -> bool:
        """Close a chat's browser task. True if one was open."""
        self._cancel_idle(conversation)
        session = self._sessions.pop(conversation, None)
        if session is None:
            return False
        session.closed = True
        session.stopped = why == "stopped"
        with contextlib.suppress(PlaywrightError):
            await session.context.close()
        log.info("browser.close", extra={"why": why})
        notify(BrowserInUse(None))
        return True

    async def close_all(self) -> None:
        for conversation in list(self._sessions):
            await self.close(conversation, why="shutdown")
        if self._pw is not None:
            with contextlib.suppress(PlaywrightError):
                await self._pw.stop()
            self._pw = None

    def recover(self) -> int:
        """Close browsers left running on Enzo's profile by a gateway that crashed."""
        marker = f"--user-data-dir={self.profile}"
        left: list[psutil.Process] = []
        for proc in psutil.process_iter(["cmdline"]):
            with contextlib.suppress(psutil.Error):
                if marker in (proc.info.get("cmdline") or []):
                    left.append(proc)
        for proc in left:
            with contextlib.suppress(psutil.Error):
                proc.terminate()
        _, alive = psutil.wait_procs(left, timeout=3)
        for proc in alive:
            with contextlib.suppress(psutil.Error):
                proc.kill()
        if left:
            log.warning("browser.recovered", extra={"processes": len(left)})
        return len(left)

    # -- the turn's life (ConversationService hooks) -------------------------------------------

    async def turn_ended(self, conversation: uuid.UUID, stop: str | None) -> None:
        """A turn ended: its browser task ends too, unless it waits for the user's yes."""
        if conversation not in self._sessions:
            return
        if stop == "approval_pending":
            self._close_later(conversation)
            return
        await self.close(conversation, why="turn ended")

    async def moved_on(self, conversation: uuid.UUID) -> None:
        """A new message: a task still waiting for approval is over."""
        await self.close(conversation, why="moved on")

    async def stop(self, conversation: uuid.UUID) -> bool:
        """The user pressed Esc: the browser closes now, cutting off a step in progress."""
        return await self.close(conversation, why="stopped")

    def _close_later(self, conversation: uuid.UUID) -> None:
        self._cancel_idle(conversation)
        loop = asyncio.get_running_loop()

        def expire() -> None:
            self._idle.pop(conversation, None)
            task = loop.create_task(self.close(conversation, why="idle"))
            self._closing.add(task)
            task.add_done_callback(self._closing.discard)

        self._idle[conversation] = loop.call_later(self._idle_close_s, expire)

    def _cancel_idle(self, conversation: uuid.UUID) -> None:
        handle = self._idle.pop(conversation, None)
        if handle is not None:
            handle.cancel()


def site_or_error(text: str) -> str:
    """A site the brain named, normalised, or a plain-language error."""
    try:
        return site_of(text)
    except ValueError as exc:
        raise BrowserToolError(str(exc)) from exc
