"""The Linux desktop on X11 (ADR 0035): python-xlib for the screen, windows and input (the
XTEST extension, as xdotool uses), and AT-SPI for each app's elements.

Wayland sessions are not supported: Wayland lets no app read the screen or send input without a
portal prompt each time. Enzo says so instead of failing in odd ways.
"""

import contextlib
import io
import logging
import os
import shlex
import subprocess  # noqa: S404 - opening an app the user approved, from its desktop entry
import threading
import time
from collections.abc import Callable, Iterator
from configparser import ConfigParser, Error as ConfigError
from pathlib import Path
from typing import Any

from PIL import Image
from Xlib import XK, X, Xatom
from Xlib.display import Display
from Xlib.error import XError
from Xlib.ext import record, xtest
from Xlib.protocol import rq

from enzo.desktop.atspi import AppRef, Atspi, AtspiError
from enzo.desktop.base import (
    App,
    Button,
    Capture,
    Check,
    Combo,
    Readiness,
    Rect,
    Tree,
    Window,
    app_key,
    same_app,
)

log = logging.getLogger("enzo.desktop")

#: Pause between key presses: apps read typed text in order without it, but a little room keeps
#: slow apps (and their input methods) from dropping keys.
KEY_PAUSE_S = 0.004
_BUTTONS = {"left": 1, "middle": 2, "right": 3}
_MOD_KEYS = {"ctrl": "Control_L", "alt": "Alt_L", "shift": "Shift_L", "cmd": "Super_L"}
_KEYSYMS = {
    "enter": "Return",
    "tab": "Tab",
    "space": "space",
    "backspace": "BackSpace",
    "delete": "Delete",
    "escape": "Escape",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "home": "Home",
    "end": "End",
    "pageup": "Prior",
    "pagedown": "Next",
    **{f"f{n}": f"F{n}" for n in range(1, 13)},
}


def keysym_of(char: str) -> int:
    """The X keysym that types ``char`` (Latin-1 directly, the rest as Unicode keysyms)."""
    named = {"\t": XK.XK_Tab, " ": XK.XK_space}
    if char in named:
        return named[char]
    code = ord(char)
    if 0x20 <= code <= 0x7E or 0xA0 <= code <= 0xFF:
        return code
    return 0x01000000 | code


class X11Backend:
    """The X11 display in ``DISPLAY`` (a Linux desktop, or Xvfb in tests)."""

    label = "Linux (X11)"

    def __init__(self, display: str | None = None) -> None:
        self._name = display or os.environ.get("DISPLAY")
        self._display: Display | None = None
        self._atspi = Atspi()
        #: Escape presses Enzo sent itself, so the stop key does not stop Enzo for them.
        self._own_escapes = 0
        self._escapes_lock = threading.Lock()
        self._spare: int | None = None

    # -- the connection -------------------------------------------------------------------

    @property
    def d(self) -> Display:
        if self._display is None:
            if not self._name:
                msg = "no X display (DISPLAY is not set)"
                raise OSError(msg)
            self._display = Display(self._name)
            if not self._display.has_extension("XTEST"):
                msg = "the X server has no XTEST extension, so Enzo cannot send input"
                raise OSError(msg)
        return self._display

    def close(self) -> None:
        self._atspi.close()
        if self._display is not None:
            with contextlib.suppress(Exception):
                self._display.close()
            self._display = None

    def readiness(self) -> Readiness:
        checks = []
        if os.environ.get("WAYLAND_DISPLAY") and not self._name:
            line = (
                "This is a Wayland session: Enzo can use the desktop only on X11 (choose an "
                "X11 or Xorg session at the login screen)."
            )
            return Readiness(self.label, (Check(ok=False, line=line),))
        try:
            screen = self.screen()
            checks.append(
                Check(ok=True, line=f"Display {self._name}: {int(screen.w)}x{int(screen.h)}")
            )
        except (OSError, XError, ConnectionError) as exc:
            line = f"Display: Enzo cannot open the X display ({exc})."
            return Readiness(self.label, (Check(ok=False, line=line),))
        try:
            found = len(self._atspi.apps())
            checks.append(Check(ok=True, line=f"Accessibility: {found} apps share their elements"))
        except AtspiError as exc:
            checks.append(
                Check(
                    ok=False,
                    line=(
                        f"Accessibility: {exc}. Enzo reads app elements through AT-SPI to know "
                        "what it clicks (sudo apt install at-spi2-core)."
                    ),
                )
            )
        return Readiness(self.label, tuple(checks))

    # -- what is on screen ----------------------------------------------------------------

    def screen(self) -> Rect:
        s = self.d.screen()
        return Rect(0, 0, s.width_in_pixels, s.height_in_pixels)

    def capture(self) -> Capture:
        screen = self.screen()
        w, h = int(screen.w), int(screen.h)
        raw = self.d.screen().root.get_image(0, 0, w, h, X.ZPixmap, 0xFFFFFFFF)
        image = Image.frombytes("RGB", (w, h), bytes(raw.data), "raw", "BGRX")
        out = io.BytesIO()
        image.save(out, format="PNG")
        return Capture(out.getvalue(), w, h, screen)

    def _toplevels(self) -> Iterator[tuple[Any, Any, Rect]]:
        """Viewable top-level windows, back to front: (frame, client, rect)."""
        root = self.d.screen().root
        for frame in root.query_tree().children:
            with contextlib.suppress(XError):
                attrs = frame.get_attributes()
                if attrs.map_state != X.IsViewable or attrs.win_class == X.InputOnly:
                    continue
                geo = frame.get_geometry()
                if geo.width <= 1 or geo.height <= 1:
                    continue
                pos = root.translate_coords(frame, 0, 0)
                client = self._client(frame) or frame
                yield frame, client, Rect(pos.x, pos.y, geo.width, geo.height)

    def _client(self, frame: Any, depth: int = 0) -> Any | None:
        """The app's own window inside a window manager's frame (the one with a process id)."""
        if self._pid_of(frame) is not None:
            return frame
        if depth >= 2:
            return None
        with contextlib.suppress(XError):
            for child in frame.query_tree().children:
                if (found := self._client(child, depth + 1)) is not None:
                    return found
        return None

    def _pid_of(self, window: Any) -> int | None:
        with contextlib.suppress(XError):
            prop = window.get_full_property(self.d.intern_atom("_NET_WM_PID"), Xatom.CARDINAL)
            if prop is not None and len(prop.value):
                return int(prop.value[0])
        return None

    def _title(self, window: Any) -> str:
        with contextlib.suppress(XError):
            utf8 = self.d.intern_atom("UTF8_STRING")
            prop = window.get_full_property(self.d.intern_atom("_NET_WM_NAME"), utf8)
            if prop is not None:
                value = prop.value
                return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
            return str(window.get_wm_name() or "")
        return ""

    def windows(self) -> list[Window]:
        found = []
        for _frame, client, rect in self._toplevels():
            pid = self._pid_of(client)
            if pid is not None:
                found.append(Window(pid, rect, self._title(client)))
        return list(reversed(found))

    def apps(self) -> list[App]:
        names: dict[int, str] = {}
        with contextlib.suppress(AtspiError):
            for a in self._atspi.apps():
                if a.pid is not None:
                    names.setdefault(a.pid, a.name)
        apps: dict[int, App] = {}
        for _frame, client, _rect in self._toplevels():
            pid = self._pid_of(client)
            if pid is None or pid in apps:
                continue
            wm_class = None
            with contextlib.suppress(XError):
                wm_class = client.get_wm_class()
            klass = wm_class[1] if wm_class else None
            name = names.get(pid) or klass or self._title(client) or f"process {pid}"
            apps[pid] = App(name.removesuffix(".py"), pid, klass)
        return list(apps.values())

    def frontmost(self) -> App | None:
        pid = self._active_pid()
        return next((a for a in self.apps() if a.pid == pid), None) if pid else None

    def _active_pid(self) -> int | None:
        root = self.d.screen().root
        with contextlib.suppress(XError):
            prop = root.get_full_property(self.d.intern_atom("_NET_ACTIVE_WINDOW"), Xatom.WINDOW)
            if prop is not None and len(prop.value) and prop.value[0]:
                window = self.d.create_resource_object("window", int(prop.value[0]))
                if (pid := self._pid_of(window)) is not None:
                    return pid
        focus = self.d.get_input_focus().focus
        if isinstance(focus, int):  # PointerRoot or None: no window has the keyboard
            return None
        window: Any = focus
        while window is not None:
            if (pid := self._pid_of(window)) is not None:
                return pid
            with contextlib.suppress(XError):
                tree = window.query_tree()
                if tree.parent == root or not tree.parent:
                    client = self._client(window)
                    return self._pid_of(client) if client is not None else None
                window = tree.parent
                continue
            return None
        return None

    def activate(self, app: App) -> None:
        mine = [(f, c) for f, c, _r in self._toplevels() if self._pid_of(c) == app.pid]
        if not mine:
            return
        frame, client = mine[-1]  # its window in front
        root = self.d.screen().root
        if self._supports("_NET_ACTIVE_WINDOW"):
            data = (32, [2, X.CurrentTime, 0, 0, 0])
            event = _client_message(client, self.d.intern_atom("_NET_ACTIVE_WINDOW"), data)
            mask = X.SubstructureRedirectMask | X.SubstructureNotifyMask
            root.send_event(event, event_mask=mask)
        else:
            frame.configure(stack_mode=X.Above)
            client.set_input_focus(X.RevertToParent, X.CurrentTime)
        self.d.sync()

    def _supports(self, atom: str) -> bool:
        root = self.d.screen().root
        with contextlib.suppress(XError):
            prop = root.get_full_property(self.d.intern_atom("_NET_SUPPORTED"), Xatom.ATOM)
            return prop is not None and self.d.intern_atom(atom) in prop.value
        return False

    # -- apps that are not running ----------------------------------------------------------

    def installed(self, name: str) -> str | None:
        entry = _desktop_entry(name)
        return entry[0] if entry else None

    def launch(self, name: str) -> bool:
        entry = _desktop_entry(name)
        if entry is None:
            return False
        argv = [a for a in shlex.split(entry[1]) if not (len(a) == 2 and a.startswith("%"))]
        subprocess.Popen(  # noqa: S603 - the app's own desktop entry
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True

    # -- elements ---------------------------------------------------------------------------

    def tree(self, app: App, limit: int) -> Tree | None:
        try:
            refs = self._atspi.apps()
        except AtspiError:
            return None
        found: AppRef | None = next((r for r in refs if r.pid == app.pid), None)
        if found is None:
            found = next((r for r in refs if same_app(r.name, app)), None)
        if found is None:
            return None
        try:
            window = self._atspi.front_window(found)
            if window is None:
                return Tree(app, "", ())
            nodes, cut = self._atspi.walk(window, limit)
        except AtspiError:
            return None
        title = nodes[0].name if nodes else ""
        return Tree(app, title, tuple(nodes), cut)

    # -- input ------------------------------------------------------------------------------

    def _fake(self, kind: int, detail: int = 0, x: int = 0, y: int = 0) -> None:
        xtest.fake_input(self.d, kind, detail, x=x, y=y)

    def click(self, x: float, y: float, *, button: Button, count: int) -> None:
        self._fake(X.MotionNotify, x=round(x), y=round(y))
        self.d.sync()
        for _ in range(count):
            self._fake(X.ButtonPress, _BUTTONS[button])
            self._fake(X.ButtonRelease, _BUTTONS[button])
            self.d.sync()
            time.sleep(0.03)

    def type_text(self, text: str, stopped: Callable[[], bool]) -> None:
        for char in text:
            if stopped():
                return
            self._type_char(char)
            time.sleep(KEY_PAUSE_S)
        self.d.sync()

    def _type_char(self, char: str) -> None:
        sym = keysym_of(char)
        codes = [(kc, i) for kc, i in self.d.keysym_to_keycodes(sym) if i in (0, 1)]
        if codes:
            code, index = codes[0]
            self._tap(code, shift=index == 1)
            return
        code = self._spare_code()
        self.d.change_keyboard_mapping(code, [(sym, sym)])
        self.d.sync()
        self._tap(code, shift=False)
        self.d.sync()
        self.d.change_keyboard_mapping(code, [(X.NoSymbol, X.NoSymbol)])
        self.d.sync()

    def _spare_code(self) -> int:
        """A keycode with no key on it, borrowed to type a character the keyboard lacks."""
        if self._spare is None:
            first = self.d.display.info.min_keycode
            count = self.d.display.info.max_keycode - first + 1
            mapping = self.d.get_keyboard_mapping(first, count)
            empty = [first + i for i, syms in enumerate(mapping) if not any(syms)]
            if not empty:
                msg = "the keyboard has no free key to type this character"
                raise OSError(msg)
            self._spare = empty[-1]
        return self._spare

    def _tap(self, code: int, *, shift: bool) -> None:
        shift_code = self.d.keysym_to_keycode(XK.string_to_keysym("Shift_L"))
        if shift:
            self._fake(X.KeyPress, shift_code)
        self._fake(X.KeyPress, code)
        self._fake(X.KeyRelease, code)
        if shift:
            self._fake(X.KeyRelease, shift_code)

    def press(self, combo: Combo) -> None:
        mods = [self.d.keysym_to_keycode(XK.string_to_keysym(_MOD_KEYS[m])) for m in combo.mods if m in _MOD_KEYS]
        if combo.key in _KEYSYMS:
            sym, shift = XK.string_to_keysym(_KEYSYMS[combo.key]), False
            code = self.d.keysym_to_keycode(sym)
        else:
            sym = keysym_of(combo.key)
            found = [(kc, i) for kc, i in self.d.keysym_to_keycodes(sym) if i in (0, 1)]
            if not found:
                msg = f"the keyboard has no key for “{combo.key}”"
                raise OSError(msg)
            code, index = found[0]
            shift = index == 1 and "shift" not in combo.mods
        if combo.key == "escape":
            with self._escapes_lock:
                self._own_escapes += 1
        held = [*mods, *([self.d.keysym_to_keycode(XK.string_to_keysym("Shift_L"))] if shift else [])]
        for m in held:
            self._fake(X.KeyPress, m)
        self._fake(X.KeyPress, code)
        self._fake(X.KeyRelease, code)
        for m in reversed(held):
            self._fake(X.KeyRelease, m)
        self.d.sync()

    def scroll(self, x: float, y: float, dy: int, dx: int) -> None:
        self._fake(X.MotionNotify, x=round(x), y=round(y))
        for count, (neg, pos) in ((dy, (4, 5)), (dx, (6, 7))):
            button = pos if count > 0 else neg
            for _ in range(abs(count)):
                self._fake(X.ButtonPress, button)
                self._fake(X.ButtonRelease, button)
        self.d.sync()

    def release(self) -> None:
        if self._display is None:
            return
        with contextlib.suppress(XError, OSError):
            for name in (*_MOD_KEYS.values(), "Shift_R", "Control_R", "Alt_R"):
                code = self.d.keysym_to_keycode(XK.string_to_keysym(name))
                if code:
                    self._fake(X.KeyRelease, code)
            for button in _BUTTONS.values():
                self._fake(X.ButtonRelease, button)
            self.d.sync()

    # -- the stop key -----------------------------------------------------------------------

    def listen_stop(self, on_stop: Callable[[], None]) -> "X11StopKey | None":
        try:
            return X11StopKey(self._name or "", on_stop, self._own_escape)
        except (OSError, XError, ConnectionError, ValueError) as exc:
            log.warning("desktop.stop_key_unavailable", extra={"why": type(exc).__name__})
            return None

    def _own_escape(self) -> bool:
        with self._escapes_lock:
            if self._own_escapes > 0:
                self._own_escapes -= 1
                return True
            return False


class X11StopKey:
    """Esc pressed anywhere on the X display (seen with the RECORD extension, never grabbed:
    the app in front still gets its Esc)."""

    def __init__(self, name: str, on_stop: Callable[[], None], own: Callable[[], bool]) -> None:
        self._record = Display(name)
        self._control = Display(name)
        if not self._record.has_extension("RECORD"):
            msg = "no RECORD extension"
            raise ValueError(msg)
        self._escape = self._record.keysym_to_keycode(XK.string_to_keysym("Escape"))
        self._on_stop, self._own = on_stop, own
        self._context = self._record.record_create_context(
            0,
            [record.AllClients],
            [
                {
                    "core_requests": (0, 0),
                    "core_replies": (0, 0),
                    "ext_requests": (0, 0, 0, 0),
                    "ext_replies": (0, 0, 0, 0),
                    "delivered_events": (0, 0),
                    "device_events": (X.KeyPress, X.KeyPress),
                    "errors": (0, 0),
                    "client_started": False,
                    "client_died": False,
                }
            ],
        )
        self._thread = threading.Thread(target=self._run, name="enzo-stop-key", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        with contextlib.suppress(Exception):
            self._record.record_enable_context(self._context, self._seen)

    def _seen(self, reply: Any) -> None:
        if reply.category != record.FromServer or reply.client_swapped or not reply.data:
            return
        data = reply.data
        while len(data):
            event, data = rq.EventField(None).parse_binary_value(data, self._record.display, None, None)
            if event.type == X.KeyPress and event.detail == self._escape and not self._own():
                self._on_stop()

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._control.record_disable_context(self._context)
            self._control.flush()
        self._thread.join(timeout=2)
        with contextlib.suppress(Exception):
            self._control.record_free_context(self._context)
            self._control.close()
        with contextlib.suppress(Exception):
            self._record.close()


def _client_message(window: Any, kind: int, data: tuple[int, list[int]]) -> Any:
    from Xlib.protocol import event  # noqa: PLC0415

    return event.ClientMessage(window=window, client_type=kind, data=data)


def _desktop_dirs() -> list[Path]:
    home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    shared = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    return [home / "applications", *(Path(p) / "applications" for p in shared.split(":") if p)]


def _desktop_entry(name: str) -> tuple[str, str] | None:
    """The desktop entry of an installed app by this name: (its name, its command)."""
    want = app_key(name)
    for folder in _desktop_dirs():
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.desktop")):
            parser = ConfigParser(interpolation=None, strict=False)
            try:
                parser.read(path, encoding="utf-8")
                entry = parser["Desktop Entry"]
            except (ConfigError, KeyError, OSError, UnicodeDecodeError):
                continue
            if entry.get("Type") != "Application" or entry.getboolean("NoDisplay", False):
                continue
            title, command = entry.get("Name", ""), entry.get("Exec", "")
            if command and want in (app_key(title), app_key(path.stem)):
                return title, command
    return None
