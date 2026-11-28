"""Linux's accessibility tree (AT-SPI) over D-Bus, with jeepney (ADR 0035).

AT-SPI is how screen readers read GTK, Qt, Firefox, Chromium and LibreOffice apps on Linux: the
apps publish their elements on an accessibility bus that ``org.a11y.Bus`` names. Enzo reads it
directly (no PyGObject), one blocking connection on the desktop worker thread.
"""

import contextlib
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from jeepney import DBusAddress, new_method_call
from jeepney.io.blocking import DBusConnection, open_dbus_connection
from jeepney.wrappers import DBusErrorResponse, unwrap_msg

from enzo.desktop.base import Node, Rect

ACCESSIBLE = "org.a11y.atspi.Accessible"
COMPONENT = "org.a11y.atspi.Component"
TEXT = "org.a11y.atspi.Text"
REGISTRY = ("org.a11y.atspi.Registry", "/org/a11y/atspi/accessible/root")
#: Seconds one call may take: a hung app must not hang Enzo.
TIMEOUT_S = 1.5

# AtspiStateType bits (at-spi2-core atspi-constants.h).
ACTIVE, EDITABLE, ENABLED, FOCUSED, MULTI_LINE = 1, 7, 8, 12, 17
SENSITIVE, SHOWING, SINGLE_LINE, VISIBLE, IS_DEFAULT = 24, 25, 26, 30, 39
#: AtspiRelationType: this element is labelled by another (a field's label).
LABELLED_BY = 2

#: AT-SPI role names as the brain reads them; roles not listed are shown as AT-SPI names them.
ROLES = {
    "push button": "button",
    "toggle button": "toggle button",
    "text": "text field",
    "entry": "text field",
    "password text": "password field",
    "label": "text",
    "static": "text",
    "check box": "checkbox",
    "radio button": "radio button",
    "combo box": "pop-up button",
    "page tab": "tab",
    "page tab list": "tabs",
    "menu item": "menu item",
    "check menu item": "menu item",
    "radio menu item": "menu item",
    "menu": "menu",
    "menu bar": "menu bar",
    "list item": "list item",
    "table cell": "cell",
    "link": "link",
    "spin button": "number field",
    "slider": "slider",
    "frame": "window",
    "dialog": "dialog",
    "alert": "alert",
    "window": "window",
    "heading": "heading",
    "image": "image",
    "icon": "image",
    "document text": "text area",
    "paragraph": "text",
    "tool bar": "toolbar",
    "status bar": "status bar",
}
#: Roles that only hold other elements: shown only when they have a name of their own.
CONTAINERS = {
    "panel",
    "filler",
    "scroll pane",
    "viewport",
    "layered pane",
    "split pane",
    "section",
    "form",
    "grouping",
    "unknown",
    "redundant object",
    "internal frame",
    "root pane",
    "glass pane",
    "option pane",
    "table",
    "tree table",
    "list",
    "list box",
    "tree",
    "scroll bar",
    "separator",
    "application",
}


@dataclass(frozen=True, slots=True)
class Ref:
    """An element on the accessibility bus: the app's bus name and the element's path."""

    bus: str
    path: str


@dataclass(frozen=True, slots=True)
class AppRef:
    ref: Ref
    name: str
    pid: int | None


class AtspiError(Exception):
    pass


class Atspi:
    """A connection to the accessibility bus, opened on first use."""

    def __init__(self) -> None:
        self._conn: DBusConnection | None = None

    def close(self) -> None:
        if self._conn is not None:
            with contextlib.suppress(OSError):
                self._conn.close()
            self._conn = None

    def _bus(self) -> DBusConnection:
        if self._conn is None:
            try:
                session = open_dbus_connection("SESSION")
            except (KeyError, OSError, StopIteration) as exc:
                msg = "no D-Bus session (DBUS_SESSION_BUS_ADDRESS is not set)"
                raise AtspiError(msg) from exc
            try:
                bus = DBusAddress("/org/a11y/bus", bus_name="org.a11y.Bus", interface="org.a11y.Bus")
                reply = session.send_and_get_reply(new_method_call(bus, "GetAddress"), timeout=2)
                (address,) = unwrap_msg(reply)
                self._conn = open_dbus_connection(address)
            except (DBusErrorResponse, OSError, TimeoutError, ValueError) as exc:
                msg = "the accessibility bus is not running (install at-spi2-core)"
                raise AtspiError(msg) from exc
            finally:
                with contextlib.suppress(OSError):
                    session.close()
        return self._conn

    def call(
        self, ref: Ref, interface: str, method: str, sig: str | None = None, body: tuple[Any, ...] = ()
    ) -> tuple[Any, ...]:
        obj = DBusAddress(ref.path, bus_name=ref.bus, interface=interface)
        try:
            reply = self._bus().send_and_get_reply(
                new_method_call(obj, method, sig, body), timeout=TIMEOUT_S
            )
            return tuple(unwrap_msg(reply))
        except (DBusErrorResponse, TimeoutError) as exc:
            raise AtspiError(f"{method} failed: {exc}") from exc
        except OSError as exc:
            self.close()
            raise AtspiError(f"{method} failed: {exc}") from exc

    def prop(self, ref: Ref, interface: str, name: str) -> Any:
        (value,) = self.call(ref, "org.freedesktop.DBus.Properties", "Get", "ss", (interface, name))
        return value[1]

    # -- apps ---------------------------------------------------------------------------------

    def apps(self) -> list[AppRef]:
        """The apps that publish an accessibility tree, with their process ids."""
        (children,) = self.call(Ref(*REGISTRY), ACCESSIBLE, "GetChildren")
        found = []
        for bus, path in children:
            ref = Ref(bus, path)
            try:
                name = str(self.prop(ref, ACCESSIBLE, "Name"))
            except AtspiError:
                continue
            found.append(AppRef(ref, name, self._pid(bus)))
        return found

    def _pid(self, bus: str) -> int | None:
        dbus = Ref("org.freedesktop.DBus", "/org/freedesktop/DBus")
        try:
            (pid,) = self.call(dbus, "org.freedesktop.DBus", "GetConnectionUnixProcessID", "s", (bus,))
            return int(pid)
        except AtspiError:
            return None

    def children(self, ref: Ref) -> list[Ref]:
        (found,) = self.call(ref, ACCESSIBLE, "GetChildren")
        return [Ref(bus, path) for bus, path in found]

    def states(self, ref: Ref) -> set[int]:
        (words,) = self.call(ref, ACCESSIBLE, "GetState")
        return {i * 32 + bit for i, word in enumerate(words) for bit in range(32) if word >> bit & 1}

    def front_window(self, app: AppRef) -> Ref | None:
        """The app's active window, else its first one on screen."""
        shown = None
        for ref in self.children(app.ref):
            try:
                states = self.states(ref)
            except AtspiError:
                continue
            if ACTIVE in states:
                return ref
            if shown is None and SHOWING in states:
                shown = ref
        return shown

    # -- elements -----------------------------------------------------------------------------

    def walk(self, window: Ref, limit: int) -> tuple[list[Node], bool]:
        """The window's elements in reading order, at most ``limit`` (and whether some were
        left out). Elements not on screen are skipped with their children."""
        nodes: list[Node] = []
        cut = False

        def visit(ref: Ref, depth: int, path: str) -> None:
            nonlocal cut
            if len(nodes) >= limit:
                cut = True
                return
            try:
                node, kids = self._node(ref, depth, path)
            except AtspiError:
                return
            if node is None:
                return
            nodes.append(node)
            for i, kid in enumerate(kids):
                visit(kid, depth + 1, f"{path}/{node.role}{i}")

        visit(window, 0, "")
        return nodes, cut

    def _node(self, ref: Ref, depth: int, path: str) -> tuple[Node | None, list[Ref]]:
        states = self.states(ref)
        if depth > 0 and not ({SHOWING, VISIBLE} <= states):
            return None, []
        raw = str(self.call(ref, ACCESSIBLE, "GetRoleName")[0])
        role = ROLES.get(raw, raw)
        secure = raw == "password text"
        multiline = MULTI_LINE in states and SINGLE_LINE not in states
        if role == "text field" and multiline:
            role = "text area"
        editable = EDITABLE in states
        if raw == "text" and not editable:
            role = "text"
        name = str(self.prop(ref, ACCESSIBLE, "Name") or "")
        if not name and editable:
            name = self._label(ref)
        value = None
        if editable and not secure:
            value = self._text(ref)
        elif raw in ("label", "static", "text", "paragraph", "heading") and not name:
            name = self._text(ref) or ""
        interfaces = set(self.call(ref, ACCESSIBLE, "GetInterfaces")[0])
        rect = None
        if COMPONENT in interfaces:
            x, y, w, h = self.call(ref, COMPONENT, "GetExtents", "u", (0,))[0]
            if w > 0 and h > 0:
                rect = Rect(x, y, w, h)
        node = Node(
            role="container" if raw in CONTAINERS else role,
            name=" ".join(name.split()),
            value=value,
            rect=rect,
            depth=depth,
            secure=secure,
            editable=editable,
            multiline=multiline,
            focused=FOCUSED in states,
            enabled=ENABLED in states or SENSITIVE in states,
            default=IS_DEFAULT in states,
            search="search" in name.casefold(),
            key=f"{path}/{role}",
            handle=ref,
        )
        return node, self.children(ref)

    def _label(self, ref: Ref) -> str:
        """A field's label, from the element that labels it."""
        try:
            (relations,) = self.call(ref, ACCESSIBLE, "GetRelationSet")
        except AtspiError:
            return ""
        for kind, targets in relations:
            if kind == LABELLED_BY:
                for bus, path in targets:
                    with contextlib.suppress(AtspiError):
                        return str(self.prop(Ref(bus, path), ACCESSIBLE, "Name") or "")
        return ""

    def _text(self, ref: Ref) -> str | None:
        try:
            return str(self.call(ref, TEXT, "GetText", "ii", (0, -1))[0])
        except AtspiError:
            return None


def flatten(nodes: list[Node]) -> Iterator[Node]:
    yield from nodes
