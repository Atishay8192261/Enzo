"""Which browser steps need their own yes, and what page content is hidden (ADR 0027).

A browser task is approved once. Steps then run on their own, except anything irreversible or
outward (spec §12.3): buying or paying, sending or posting, deleting, entering a password, card or
one-time code, submitting a form that holds one, and going to a site the user did not approve.

The rules read what the page says about an element (its tag, type, autocomplete hint, label, the
form it submits). A site can mislabel its own buttons, so these are checks on the page, not a
guarantee; ADR 0027 records the limits.
"""

import re
from collections.abc import Sequence
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict

type SecretKind = Literal["password", "card", "code"]

#: Steps whose label says they buy, send, delete or sign up: each asks first.
_VERBS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "buys or pays for something",
        re.compile(
            r"\b(buy|purchase|pay|checkout now|place (your |my )?order|order now|"
            r"complete (the |your )?(order|purchase|payment)|confirm (and pay|order|purchase|"
            r"payment|booking)|submit (order|payment)|book( now)?|reserve|rent|bid|donate|"
            r"subscribe|start (free )?trial|upgrade|transfer|send money)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "sends or posts something",
        re.compile(
            r"\b(send|post|publish|share|tweet|reply|comment|submit|sign up|register|"
            r"create (an |my )?account|invite)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "deletes or cancels something",
        re.compile(
            r"\b(delete|remove|erase|trash|discard|clear (all|history|data)|unsubscribe|"
            r"cancel (my |your |the )?(order|subscription|membership|account|booking|plan)|"
            r"close (my |your )?account|deactivate)\b",
            re.IGNORECASE,
        ),
    ),
    ("confirms something", re.compile(r"^\s*(confirm|ok|yes|agree and continue)\s*$", re.I)),
)

#: A page about paying: a form submitted here asks first, whatever its button says.
_CHECKOUT = re.compile(
    r"check[-_ ]?out|payment|\bpay\b|billing|place[-_ ]?order|purchase|order[-_ ]?(review|confirm)",
    re.IGNORECASE,
)

_CARD = re.compile(
    r"card[-_ ]?(number|no|num)|\bcc[-_ ]?(num|number|no)\b|credit[-_ ]?card|debit[-_ ]?card|"
    r"\bcvc|\bcvv|\bcsc\b|security code|expir|\bexp[-_ ]?(date|month|year|mm|yy)|"
    r"card[-_ ]?holder|name on (the )?card",
    re.IGNORECASE,
)
_CODE = re.compile(
    r"\botp\b|one[-_ ]?time|verification[-_ ]?code|\b2fa\b|two[-_ ]?factor|\bmfa\b|\btotp\b|"
    r"auth(entication)?[-_ ]?code|sms[-_ ]?code|passcode|\bpin\b",
    re.IGNORECASE,
)
_PASSWORD = re.compile(r"pass(word|wd|phrase)", re.IGNORECASE)


class Field(BaseModel):
    """What the page says about one form field (read in the page, see ``FIELD_JS``)."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    tag: str = ""
    type: str = ""
    autocomplete: str = ""
    name: str = ""
    id: str = ""
    label: str = ""
    placeholder: str = ""
    value: str = ""


class Element(BaseModel):
    """What the page says about the element a step acts on (see ``ELEMENT_JS``)."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    tag: str
    type: str = ""
    role: str = ""
    label: str = ""
    #: The address a link (or the link around this element) goes to, made absolute.
    href: str | None = None
    #: The element is a field: what it is (for typing and choosing).
    field: Field | None = None
    #: Text can be typed into it although it is not a form field (a message editor).
    editable: bool = False
    #: The element submits its form when clicked (or Enter is pressed in it).
    submits: bool = False
    #: The form the element belongs to, if any.
    form_action: str | None = None
    form_method: str = "get"
    form_fields: tuple[Field, ...] = ()
    #: Where the page is, and its title (a checkout page is recognised by these).
    page_url: str = ""
    page_title: str = ""


def secret_kind(field: Field) -> SecretKind | None:
    """A password, payment card or one-time code field, from its type, hints and label."""
    auto = field.autocomplete.lower().split()
    words = " ".join((field.name, field.id, field.label, field.placeholder))
    if field.type.lower() == "password" or {"current-password", "new-password"} & set(auto):
        return "password"
    if any(a.startswith("cc-") for a in auto) or _CARD.search(words):
        return "card"
    if "one-time-code" in auto or _CODE.search(words):
        return "code"
    if _PASSWORD.search(words):
        return "password"
    return None


def looks_like_card(text: str) -> bool:
    """13 to 19 digits (spaces and dashes allowed) that pass the Luhn check: a card number."""
    digits = re.sub(r"[ -]", "", text)
    if not digits.isdigit() or not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def masked(kind: SecretKind, value: str) -> str:
    """How a secret is shown in a page snapshot or on a card: never the secret itself."""
    if not value:
        return "empty"
    if kind == "card":
        digits = re.sub(r"\D", "", value)
        return f"card number ending {digits[-4:]}" if len(digits) >= 12 else "filled (hidden)"
    return "filled (hidden)"


def is_checkout(url: str, title: str) -> bool:
    return bool(_CHECKOUT.search(urlsplit(url).path) or _CHECKOUT.search(title))


def verb_risk(label: str) -> str | None:
    """Why a step with this label asks first ("buys or pays for something"), or None."""
    words = " ".join(label.split())[:200]
    return next((why for why, pattern in _VERBS if pattern.search(words)), None)


def submit_risk(element: Element) -> str | None:
    """Why submitting this element's form asks first, or None."""
    kinds = {k for f in element.form_fields if (k := secret_kind(f)) and f.value}
    if "card" in kinds:
        return "submits payment card details"
    if "password" in kinds:
        return "signs in or sends a password"
    if "code" in kinds:
        return "sends a one-time code"
    if is_checkout(element.page_url, element.page_title):
        return "submits a form on a checkout page"
    return None


def click_risk(element: Element) -> str | None:
    """Why clicking this element asks first, or None: its words, then the form it submits."""
    if why := verb_risk(element.label):
        return f"it {why}"
    if element.submits and (why := submit_risk(element)):
        return f"it {why}"
    return None


def type_risk(element: Element, text: str, *, submit: bool) -> str | None:
    """Why typing this text here (and pressing Enter, when ``submit``) asks first, or None."""
    kind = secret_kind(element.field) if element.field else None
    if kind == "password":
        return "it enters a password"
    if kind == "card" or looks_like_card(text):
        return "it enters payment card details"
    if kind == "code":
        return "it enters a one-time code"
    if submit:
        filled = _with_value(element, text)
        if why := submit_risk(filled):
            return f"pressing Enter {why}"
    return None


def _with_value(element: Element, text: str) -> Element:
    """The element's form as it will be once ``text`` is typed into the element's field."""
    if element.field is None:
        return element
    fields = tuple(
        f.model_copy(update={"value": text}) if f == element.field else f
        for f in element.form_fields
    )
    return element.model_copy(update={"form_fields": fields})


def form_lines(fields: Sequence[Field]) -> list[str]:
    """A form's fields as the approval card shows them: secrets masked, nothing hidden left out."""
    lines = []
    for f in fields:
        name = " ".join((f.label or f.placeholder or f.name or f.id or "field").split())[:60]
        kind = secret_kind(f)
        value = masked(kind, f.value) if kind else (" ".join(f.value.split())[:120] or "empty")
        lines.append(f"  {name}: {value}")
    return lines


#: Reads one field in the page. Runs inside the page: the page's own JavaScript cannot change
#: what it returns before Enzo reads it, but it can lie in its labels (ADR 0027).
FIELD_JS = """
(el) => {
  const clean = (s) => (s || "").replace(/\\s+/g, " ").trim();
  const labels = el.labels ? [...el.labels].map((l) => clean(l.innerText)) : [];
  return {
    tag: el.tagName.toLowerCase(),
    type: clean(el.getAttribute("type")),
    autocomplete: clean(el.getAttribute("autocomplete")),
    name: clean(el.getAttribute("name")),
    id: clean(el.id),
    label: clean(el.getAttribute("aria-label")) || labels.join(" "),
    placeholder: clean(el.getAttribute("placeholder")),
    value: typeof el.value === "string" ? el.value : "",
  };
}
"""

#: Reads the element a step acts on, with its form and the page it is on.
ELEMENT_JS = (
    """
(target) => {
  // A label stands for its field: typing into it types into the field.
  const el = target.tagName === "LABEL" && target.control ? target.control : target;
  const field = """
    + FIELD_JS
    + """;
  const clean = (s) => (s || "").replace(/\\s+/g, " ").trim();
  const tag = el.tagName.toLowerCase();
  const type = clean(el.getAttribute("type")).toLowerCase();
  const isField = ["input", "textarea", "select"].includes(tag) && !["submit", "button",
    "image", "reset", "hidden"].includes(type);
  const control = el.closest("button, input, a[href], [role=button], [role=link]") || el;
  const ctag = control.tagName.toLowerCase();
  const ctype = clean(control.getAttribute("type")).toLowerCase();
  const form = el.form || control.form || el.closest("form");
  const submits = !!form && ((ctag === "button" && (ctype === "" || ctype === "submit")) ||
    (ctag === "input" && (ctype === "submit" || ctype === "image")));
  const link = el.closest("a[href]");
  const shown = (f) => f.type !== "hidden" && !["submit", "button", "image", "reset"]
    .includes((f.getAttribute("type") || "").toLowerCase());
  const fields = form ? [...form.elements].filter((f) =>
    ["input", "textarea", "select"].includes(f.tagName.toLowerCase()) && shown(f)) : [];
  const words = clean(control.getAttribute("aria-label")) || clean(control.innerText) ||
    clean(control.value) || clean(control.getAttribute("title")) ||
    clean(control.getAttribute("alt"));
  return {
    tag: ctag,
    type: ctype,
    role: clean(control.getAttribute("role")),
    label: isField ? field(el).label || field(el).placeholder || field(el).name : words,
    href: link ? link.href : null,
    field: isField ? field(el) : null,
    editable: !!el.isContentEditable,
    submits,
    form_action: form ? form.action : null,
    form_method: form ? (form.method || "get").toLowerCase() : "get",
    form_fields: fields.map(field),
    page_url: location.href,
    page_title: document.title,
  };
}
"""
)
