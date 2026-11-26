"""The rules that decide which browser steps ask first, and what a page view hides (ADR 0027)."""

import pytest

from enzo.browser.rules import (
    Element,
    Field,
    click_risk,
    form_lines,
    looks_like_card,
    masked,
    secret_kind,
    type_risk,
)
from enzo.browser.session import hide_card_numbers
from enzo.browser.sites import SiteError, allowed, site_of

CARD = "4242 4242 4242 4242"  # the well-known test card number (Luhn-valid)


@pytest.mark.parametrize(
    ("text", "site"),
    [
        ("amazon.com", "amazon.com"),
        ("https://www.Amazon.com/gp/cart", "amazon.com"),
        ("http://localhost:8851/x", "localhost:8851"),
        ("https://shop.example:443/", "shop.example"),
        ("shop.example.", "shop.example"),
        ("http://[::1]:8080/", "[::1]:8080"),
        ("127.0.0.1:9000", "127.0.0.1:9000"),
    ],
)
def test_sites_are_named_as_people_say_them(text: str, site: str) -> None:
    assert site_of(text) == site


@pytest.mark.parametrize("bad", ["ftp://x.example", "user:pw@shop.example", "", "not a site!"])
def test_things_that_are_not_sites_are_refused(bad: str) -> None:
    with pytest.raises(SiteError):
        site_of(bad)


def test_a_site_covers_its_subdomains_but_a_port_must_match() -> None:
    sites = ["amazon.com", "localhost:8851"]
    assert allowed("https://www.amazon.com/cart", sites)
    assert allowed("https://smile.amazon.com/", sites)
    assert not allowed("https://amazon.com.evil.example/", sites)
    assert not allowed("https://notamazon.com/", sites)
    assert allowed("http://localhost:8851/checkout", sites)
    # Approving a local test shop never opens the database or Enzo itself on the same machine.
    assert not allowed("http://localhost:5432/", sites)
    assert not allowed("http://localhost:8787/conversations", sites)
    assert not allowed("http://localhost/", sites)
    assert not allowed("file:///etc/passwd", sites)
    assert not allowed("http://127.0.0.1:8851/", sites)  # another name is another site


@pytest.mark.parametrize(
    ("field", "kind"),
    [
        (Field(type="password", name="pw"), "password"),
        (Field(autocomplete="current-password"), "password"),
        (Field(name="user_passphrase"), "password"),
        (Field(autocomplete="cc-number"), "card"),
        (Field(autocomplete="billing cc-csc"), "card"),
        (Field(label="Card number"), "card"),
        (Field(name="cvv"), "card"),
        (Field(placeholder="MM / YY", label="Expiry date"), "card"),
        (Field(autocomplete="one-time-code"), "code"),
        (Field(label="Verification code"), "code"),
        (Field(name="otp"), "code"),
        (Field(label="Full name", autocomplete="name"), None),
        (Field(label="Search", type="search"), None),
        (Field(label="Street address"), None),
    ],
)
def test_secret_fields_are_recognised(field: Field, kind: str | None) -> None:
    assert secret_kind(field) == kind


def test_card_numbers_are_recognised_by_the_luhn_check() -> None:
    assert looks_like_card(CARD)
    assert looks_like_card("4242-4242-4242-4242")
    assert not looks_like_card("4242 4242 4242 4241")  # fails the check
    assert not looks_like_card("1234")
    assert not looks_like_card("order 4242424242424242")


def test_secrets_are_never_spelled_out() -> None:
    assert masked("card", CARD) == "card number ending 4242"
    assert masked("card", "123") == "filled (hidden)"
    assert masked("password", "hunter2") == "filled (hidden)"
    assert masked("code", "") == "empty"
    assert hide_card_numbers(f"Paid with {CARD}, order 12345") == "Paid with •••• 4242, order 12345"


def button(label: str, **kw: object) -> Element:
    return Element.model_validate({"tag": "button", "label": label, **kw})


CHECKOUT = {"page_url": "https://shop.example/checkout", "page_title": "Checkout"}


@pytest.mark.parametrize(
    ("element", "why"),
    [
        (button("Add to cart", submits=True), None),
        (button("Proceed to checkout"), None),
        (button("Search", submits=True), None),
        (button("Next page"), None),
        (button("Buy now", submits=True), "it buys or pays for something"),
        (button("Place your order"), "it buys or pays for something"),
        (button("Pay $24.99"), "it buys or pays for something"),
        (button("Send"), "it sends or posts something"),
        (button("Post reply"), "it sends or posts something"),
        (button("Delete account"), "it deletes or cancels something"),
        (button("Cancel subscription"), "it deletes or cancels something"),
        (button("Confirm"), "it confirms something"),
        # Whatever its button says, a form on a checkout page or one holding a secret asks.
        (button("Continue", submits=True, **CHECKOUT), "it submits a form on a checkout page"),
        (button("Continue", **CHECKOUT), None),  # not a submit: just a click there
        (
            button("Go", submits=True, form_fields=[Field(type="password", value="x")]),
            "it signs in or sends a password",
        ),
        (
            button("Go", submits=True, form_fields=[Field(autocomplete="cc-number", value=CARD)]),
            "it submits payment card details",
        ),
        (button("Go", submits=True, form_fields=[Field(type="password", value="")]), None),
    ],
)
def test_clicks_that_ask_first(element: Element, why: str | None) -> None:
    assert click_risk(element) == why


def field(**kw: object) -> Element:
    f = Field.model_validate(kw)
    return Element(tag="input", field=f, form_fields=(f,))


def test_typing_that_asks_first() -> None:
    assert type_risk(field(type="password"), "x", submit=False) == "it enters a password"
    assert type_risk(field(autocomplete="cc-csc"), "123", submit=False) == (
        "it enters payment card details"
    )
    assert type_risk(field(autocomplete="one-time-code"), "123456", submit=False) == (
        "it enters a one-time code"
    )
    # A card number typed into any field is still a card number.
    assert type_risk(field(label="Notes"), CARD, submit=False) == "it enters payment card details"
    assert type_risk(field(label="Full name"), "Ada", submit=False) is None
    assert type_risk(field(label="Search"), "kettle", submit=True) is None
    on_checkout = field(label="Full name").model_copy(update=CHECKOUT)
    assert type_risk(on_checkout, "Ada", submit=False) is None
    assert type_risk(on_checkout, "Ada", submit=True) == (
        "pressing Enter submits a form on a checkout page"
    )


def test_the_card_shows_a_form_with_secrets_masked() -> None:
    fields = [
        Field(label="Full name", value="Ada Lovelace"),
        Field(autocomplete="cc-number", label="Card number", value=CARD),
        Field(type="password", name="pw", value="hunter2"),
        Field(placeholder="Notes"),
    ]
    assert form_lines(fields) == [
        "  Full name: Ada Lovelace",
        "  Card number: card number ending 4242",
        "  pw: filled (hidden)",
        "  Notes: empty",
    ]
