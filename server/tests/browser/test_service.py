"""A chat's browser task lives as long as its turn, end to end (ADR 0027): a real store, the
conversation service, real Chromium, and the events a client sees."""

import uuid

from enzo.agent.loop import ApprovalNeeded, ToolFinished, TurnDone
from enzo.agent.service import ConversationService, ServiceEvent
from enzo.brain import Message
from enzo.browser.session import Browsers
from enzo.browser.tools import browser_tools
from enzo.context import BrowserInUse
from enzo.gateway.conversations import _sse
from enzo.store.repository import Store
from enzo.tools import ToolRegistry
from tests.agent.test_loop import make_router
from tests.browser.helpers import PageBrain, Step, call, calls, click
from tests.browser.shop import Running


async def drain(stream: object) -> list[ServiceEvent]:
    return [e async for e in stream]  # type: ignore[attr-defined]


def start(shop: Running) -> Message:
    return calls(
        call(
            "browser_start",
            "s1",
            task="put the kettle in the cart",
            sites=[shop.url],
            url=f"{shop.url}/product/kettle",
        )
    )


async def test_the_browser_sign_shows_while_the_task_runs_and_goes_when_the_turn_ends(
    store: Store, browsers: Browsers, shop: Running
) -> None:
    brain = PageBrain(
        [start(shop), click("button", "Add to cart", "c1"), Message.assistant("In your cart.")]
    )
    svc = ConversationService(
        store, make_router(brain), ToolRegistry(browser_tools(browsers)), hooks=[browsers]
    )
    chat = await store.create_conversation("main")
    first = await drain(await svc.send(chat, "put the blue kettle in my cart"))
    (card,) = [e.request for e in first if isinstance(e, ApprovalNeeded)]
    await store.decide(chat, card.id, approved=True)
    events = await drain(await svc.resume(chat))
    done = events[-1]
    assert isinstance(done, TurnDone)
    assert done.outcome.stop == "final_answer"
    site = shop.url.removeprefix("http://")
    signs = [e for e in events if isinstance(e, BrowserInUse)]
    # Shown as the first page loads, after each step, and gone when the task ends.
    assert signs == [BrowserInUse(site)] * 3 + [BrowserInUse(None)]
    # The sign goes before the client hears the turn is done.
    assert events.index(BrowserInUse(None)) < events.index(done)
    assert not browsers.active(chat)
    assert shop.record.cart == {"kettle": 1}
    sse = _sse(BrowserInUse(site))
    assert (sse.event, sse.data) == ("browser", {"site": site})


async def paused_on_buy_now(
    store: Store, browsers: Browsers, shop: Running, *after: Step
) -> tuple[ConversationService, uuid.UUID]:
    """A chat whose task waits for the user's yes on "Buy now", with its page open."""
    brain = PageBrain([start(shop), click("button", "Buy now", "b1"), *after])
    svc = ConversationService(
        store, make_router(brain), ToolRegistry(browser_tools(browsers)), hooks=[browsers]
    )
    chat = await store.create_conversation("main")
    events = await drain(await svc.send(chat, "buy the kettle"))
    (card,) = [e.request for e in events if isinstance(e, ApprovalNeeded)]
    await store.decide(chat, card.id, approved=True)
    events = await drain(await svc.resume(chat))
    (buy,) = [e.request for e in events if isinstance(e, ApprovalNeeded)]
    assert buy.summary is not None
    assert buy.summary.endswith("it buys or pays for something")
    assert browsers.active(chat)  # its page stays open while the card waits
    return svc, chat


async def test_esc_closes_a_task_that_waits_for_a_yes(
    store: Store, browsers: Browsers, shop: Running
) -> None:
    svc, chat = await paused_on_buy_now(store, browsers, shop)
    assert await svc.stop(chat)  # no turn runs, but the browser closes
    assert not browsers.active(chat)
    assert not await svc.stop(chat)  # nothing left to stop
    assert shop.record.orders == []


async def test_a_new_message_ends_a_task_that_waits_for_a_yes(
    store: Store, browsers: Browsers, shop: Running
) -> None:
    # The next message's turn tries to keep using the old task's page: that task is over, so
    # nothing runs on it without a new approval.
    svc, chat = await paused_on_buy_now(
        store,
        browsers,
        shop,
        click("button", "Add to cart", "c9"),
        Message.assistant("Okay, I won't buy it."),
    )
    events = await drain(await svc.send(chat, "never mind, just put it in the cart"))
    assert isinstance(events[-1], TurnDone)
    assert not browsers.active(chat)
    (refused,) = [e.outcome for e in events if isinstance(e, ToolFinished)]
    assert refused.status == "failed"
    assert "no browser task is running" in refused.result.content
    assert shop.record.cart == {}
    assert shop.record.orders == []
