"""What every desktop backend offers (ADR 0035): the screen, its windows, apps, the
accessibility tree, and input. Backends are blocking; ``enzo.desktop.session`` runs them on one
worker thread.

Coordinates are the system's own screen coordinates (points on macOS, pixels on X11), origin at
the top left of the main screen. A screenshot may have more pixels than that (a Retina screen):
``Capture.screen`` says which part of the screen it shows.
"""

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

type Button = Literal["left", "right", "middle"]


class DesktopError(Exception):
    """A desktop step that could not be done, in plain words for the brain to relay or fix."""


@dataclass(frozen=True, slots=True)
class Rect:
    x: float
    y: float
    w: float
    h: float

    def contains(self, x: float, y: float) -> bool:
        return self.x <= x < self.x + self.w and self.y <= y < self.y + self.h

    @property
    def center(self) -> tuple[float, float]:
        return self.x + self.w / 2, self.y + self.h / 2

    @property
    def area(self) -> float:
        return max(self.w, 0) * max(self.h, 0)

    def overlaps(self, other: "Rect") -> bool:
        return (
            self.x < other.x + other.w
            and other.x < self.x + self.w
            and self.y < other.y + other.h
            and other.y < self.y + self.h
        )


@dataclass(frozen=True, slots=True)
class App:
    """A running app, as the system names it."""

    name: str
    pid: int
    #: macOS bundle id (com.apple.mail); on Linux the window class (Postbox).
    bundle: str | None = None


@dataclass(frozen=True, slots=True)
class Window:
    """A window on screen. Backends list them front to back."""

    pid: int
    rect: Rect
    title: str = ""
    #: The system's own (the menu bar): kept visible in screenshots, never a click target.
    system: bool = False
    #: A system helper that shows a dialog for the app in front (macOS's Save panel service):
    #: it belongs to that app for the task's rules.
    helper: bool = False


@dataclass(eq=False, slots=True)
class Node:
    """One element of an app's window, from the accessibility tree."""

    role: str
    name: str = ""
    value: str | None = None
    rect: Rect | None = None
    depth: int = 0
    #: A password field: never read, never typed into from memory, blacked out on screenshots.
    secure: bool = False
    editable: bool = False
    multiline: bool = False
    focused: bool = False
    enabled: bool = True
    #: The window's default button: Enter presses it.
    default: bool = False
    search: bool = False
    #: Where it is in its window (roles and positions): the same element keeps its key.
    key: str = ""
    #: The backend's own reference to the element.
    handle: object = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class Tree:
    """An app's front window: its title and elements in reading order."""

    app: App
    window: str
    nodes: tuple[Node, ...]
    #: Elements were left out (the window has more than Enzo reads at once).
    cut: bool = False


@dataclass(frozen=True, slots=True)
class Capture:
    """A screenshot of the main screen, as PNG."""

    png: bytes
    width: int
    height: int
    #: The part of the screen it shows, in screen coordinates.
    screen: Rect


@dataclass(frozen=True, slots=True)
class Check:
    """One thing desktop control needs, for ``enzo doctor`` and for a task that cannot start."""

    ok: bool
    line: str


@dataclass(frozen=True, slots=True)
class Readiness:
    """Whether Enzo can use this computer's desktop, and what to do if not."""

    backend: str
    checks: tuple[Check, ...]

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    def problem(self) -> str | None:
        """What is missing, in plain words with how to fix it; None when ready."""
        missing = [c.line for c in self.checks if not c.ok]
        return " ".join(missing) or None


@dataclass(frozen=True, slots=True)
class Combo:
    """A key with modifiers: ``cmd+shift+s`` is Combo({"cmd", "shift"}, "s")."""

    mods: frozenset[str]
    key: str

    def words(self) -> str:
        order = [m for m in MODIFIERS if m in self.mods]
        names = [KEY_WORDS.get(k, k.upper() if len(k) == 1 else k.title()) for k in order]
        key = KEY_WORDS.get(self.key, self.key.upper() if len(self.key) == 1 else self.key)
        return "+".join([*names, key])


class StopKey(Protocol):
    def close(self) -> None: ...


class Backend(Protocol):
    """A desktop Enzo can see and drive. Every method blocks; none is called concurrently."""

    label: str

    def readiness(self) -> Readiness: ...
    def screen(self) -> Rect: ...
    def capture(self) -> Capture: ...
    def windows(self) -> list[Window]: ...
    def apps(self) -> list[App]: ...
    def frontmost(self) -> App | None: ...
    def activate(self, app: App) -> None: ...
    def launch(self, name: str) -> bool:
        """Open an app that is not running; False when there is none by that name."""
        ...

    def installed(self, name: str) -> str | None:
        """The app by this name that can be opened, as the system names it; None if none."""
        ...

    def tree(self, app: App, limit: int) -> Tree | None: ...
    def click(self, x: float, y: float, *, button: Button, count: int) -> None: ...
    def type_text(self, text: str, stopped: Callable[[], bool]) -> None: ...
    def press(self, combo: Combo) -> None: ...
    def scroll(self, x: float, y: float, dy: int, dx: int) -> None: ...
    def release(self) -> None:
        """Let go of every key and button Enzo may be holding."""
        ...

    def listen_stop(self, on_stop: Callable[[], None]) -> StopKey | None:
        """Call ``on_stop`` when the user presses Esc anywhere; None if it cannot."""
        ...

    def close(self) -> None: ...


MODIFIERS = ("ctrl", "alt", "shift", "cmd", "fn")
_MOD_ALIASES = {
    "ctrl": "ctrl",
    "control": "ctrl",
    "alt": "alt",
    "option": "alt",
    "opt": "alt",
    "shift": "shift",
    "cmd": "cmd",
    "command": "cmd",
    "meta": "cmd",
    "super": "cmd",
    "win": "cmd",
    "fn": "fn",
}
#: Named keys every backend knows (the brain writes them; aliases on the right).
KEYS: dict[str, tuple[str, ...]] = {
    "enter": ("return", "enter", "kp_enter"),
    "tab": ("tab",),
    "space": ("space", " "),
    "backspace": ("backspace", "back_space"),
    "delete": ("delete", "del", "forwarddelete", "forward_delete"),
    "escape": ("escape", "esc"),
    "up": ("up", "arrowup", "up_arrow"),
    "down": ("down", "arrowdown", "down_arrow"),
    "left": ("left", "arrowleft", "left_arrow"),
    "right": ("right", "arrowright", "right_arrow"),
    "home": ("home",),
    "end": ("end",),
    "pageup": ("pageup", "page_up", "prior"),
    "pagedown": ("pagedown", "page_down", "next"),
    **{f"f{n}": (f"f{n}",) for n in range(1, 13)},
}
_KEY_ALIASES = {alias: name for name, aliases in KEYS.items() for alias in aliases}
KEY_WORDS = {
    "cmd": "Cmd",
    "ctrl": "Ctrl",
    "alt": "Alt",
    "shift": "Shift",
    "fn": "Fn",
    "enter": "Enter",
    "tab": "Tab",
    "space": "Space",
    "backspace": "Backspace",
    "delete": "Delete",
    "escape": "Esc",
    "pageup": "Page Up",
    "pagedown": "Page Down",
}
#: One printable character a shortcut can use (letters, digits, punctuation).
_CHAR = re.compile(r"^[!-~]$")


def parse_combo(text: str) -> Combo:
    """``cmd+s``, ``ctrl+shift+tab``, ``enter``: a key with modifiers, or a plain error."""
    parts = [p.strip() for p in text.strip().lower().split("+")]
    parts = [{"plus": "+", "minus": "-"}.get(p, p) for p in parts]
    if any(not p for p in parts):
        msg = f"“{text}” is not a key: write it like enter, tab, cmd+s or ctrl+shift+t"
        raise DesktopError(msg)
    *mods, key = parts
    mod_set = set()
    for m in mods:
        if m not in _MOD_ALIASES:
            msg = f"“{m}” is not a modifier key (use cmd, ctrl, alt, shift or fn)"
            raise DesktopError(msg)
        mod_set.add(_MOD_ALIASES[m])
    name = _KEY_ALIASES.get(key)
    if name is None:
        if not _CHAR.match(key):
            msg = f"“{key}” is not a key Enzo can press"
            raise DesktopError(msg)
        name = key
    return Combo(frozenset(mod_set), name)


def plain_role(role: str) -> str:
    return " ".join(role.split()).lower()


def app_key(name: str) -> str:
    """An app's name as it is compared: case, spaces, punctuation and ".app" do not count."""
    base = name.strip().casefold().removesuffix(".app").removesuffix(".py")
    return "".join(c for c in base if c.isalnum())


def same_app(name: str, app: App) -> bool:
    """The user's name for an app ("Mail", "postbox") names this running app."""
    want = app_key(name)
    if not want:
        return False
    names: Sequence[str] = (app.name, app.bundle or "", (app.bundle or "").rsplit(".", 1)[-1])
    return any(app_key(n) == want for n in names if n)
