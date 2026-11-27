"""A web form gets what it asks for, and a private reason typed into a free-text box asks first
(spec §12.5, ADR 0029). Real Chrome, the real loop and the local test shop's booking page."""

import uuid
from collections.abc import Callable

from enzo.agent.loop import ApprovalNeeded
from enzo.brain import Message
from enzo.browser.session import Browsers
from enzo.browser.tools import browser_tools
from enzo.context import conversation_var
from enzo.privacy.detect import Known, Person
from enzo.tools import ToolRegistry
from tests.agent.test_loop import run
from tests.browser.helpers import PageBrain, call, calls, click, ref, type_into
from tests.browser.shop import Running
from tests.privacy.helpers import Knows

PREGNANT = "My wife is pregnant, so no alcohol please"
NEED = "No alcohol for our table, please"


def notes(text: str, cid: str) -> Callable[[str], Message]:
    """Type into the booking form's free-text box."""
    return lambda page: calls(
        call(
            "browser_type",
            cid,
            ref=ref(page, "textbox", "Special requests"),
            element="Special requests",
            text=text,
        )
    )


async def test_a_booking_form_gets_the_name_but_a_private_reason_asks_first(
    browsers: Browsers, shop: Running
) -> None:
    conversation_var.set(str(uuid.uuid4()))
    check = Knows(Known(people=(Person("Sara", "wife"),)))
    registry = ToolRegistry(browser_tools(browsers))
    brain = PageBrain(
        [
            calls(
                call(
                    "browser_start",
                    "s1",
                    task="book a table for 2",
                    sites=[shop.url],
                    url=f"{shop.url}/book",
                )
            ),
            # The name goes into the Name field, which asks for it: no card.
            type_into("Name", "Sara Jain", "t1"),
            # The reason goes into the free-text box: it waits for the user.
            notes(PREGNANT, "t2"),
            # After the user's no: only the need, then the form is sent.
            notes(NEED, "t3"),
            click("button", "Request a table", "c1"),
            Message.assistant("Requested a table for 2."),
        ]
    )
    _, paused = await run(brain, registry, privacy=check)
    (task,) = paused.pending
    decided = {task.id: True}
    _, typed = await run(brain, registry, list(paused.messages), privacy=check, decisions=decided)
    assert typed.stop == "approval_pending"
    (card,) = typed.pending
    assert card.tool == "browser_type"
    assert card.summary == f"Type “{PREGNANT}” into “Special requests”"
    assert card.privacy == ("This includes a health detail: “My wife is pregnant”",)

    decided[card.id] = False
    events, done = await run(
        brain, registry, list(typed.messages), privacy=check, decisions=decided
    )
    assert done.stop == "final_answer", done.text
    assert not [e for e in events if isinstance(e, ApprovalNeeded)]
    assert shop.record.bookings == [{"name": "Sara Jain", "size": "2", "notes": NEED}]
