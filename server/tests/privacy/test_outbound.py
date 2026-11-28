"""The outbound check in the agent loop (spec §12.5, ADR 0029): a card names the private details
an action includes; a send that would not ask (a search) asks first when it carries one."""

from enzo.agent.loop import DECLINED, ApprovalNeeded, ToolStarted
from enzo.brain import Message, ToolResult
from enzo.brain.fake import ScriptedBrain
from enzo.privacy.check import UNCHECKED
from enzo.privacy.detect import Known
from enzo.tools.registry import Outgoing
from tests.agent.test_loop import run
from tests.privacy import helpers as h
from tests.privacy.helpers import SARA, Knows, Tavily

ANNIVERSARY = (
    "Table for 2 on Friday at 19:30 for our anniversary. My wife is pregnant, so no alcohol."
)
MINIMAL = "Table for 2 on Friday at 19:30, please, under the name Atishay. No alcohol for us."


def result_of(messages: tuple[Message, ...], call_id: str) -> ToolResult:
    return next(
        p
        for m in messages
        if m.role == "tool"
        for p in m.parts
        if isinstance(p, ToolResult) and p.call_id == call_id
    )


# ---------- ungated: a search ----------


async def test_a_search_that_carries_a_family_name_asks_first_and_sends_nothing() -> None:
    tavily = Tavily()
    brain = ScriptedBrain(
        [h.reply(h.search("birthday gift ideas for Sara")), Message.assistant("x")]
    )
    events, paused = await run(brain, h.registry(tavily), privacy=Knows(Known(people=(SARA,))))
    assert paused.stop == "approval_pending"
    (card,) = paused.pending
    assert card.summary == "Search the web for “birthday gift ideas for Sara”"
    assert card.privacy == ("This includes the name of someone in your life: “Sara” (your wife)",)
    assert [e.request for e in events if isinstance(e, ApprovalNeeded)] == [card]
    assert tavily.queries == []  # nothing left before the yes


async def test_a_yes_sends_the_search_exactly_as_shown() -> None:
    tavily = Tavily()
    registry = h.registry(tavily)
    check = Knows(Known(people=(SARA,)))
    brain = ScriptedBrain([h.reply(h.search("gift ideas for Sara")), Message.assistant("ideas")])
    _, paused = await run(brain, registry, privacy=check)
    (card,) = paused.pending
    events, done = await run(
        brain, registry, list(paused.messages), privacy=check, decisions={card.id: True}
    )
    assert done.stop == "final_answer"
    assert tavily.queries == ["gift ideas for Sara"]
    (step,) = [e.label for e in events if isinstance(e, ToolStarted)]
    assert step == "Searching the web for “gift ideas for Sara”"  # the step says what left


async def test_a_no_sends_nothing_and_the_brain_searches_again_without_the_detail() -> None:
    tavily = Tavily()
    registry = h.registry(tavily)
    check = Knows(Known(people=(SARA,)))
    brain = ScriptedBrain(
        [
            h.reply(h.search("gift ideas for Sara")),
            h.reply(h.search("birthday gift ideas", cid="s2")),
            Message.assistant("Here are some ideas."),
        ]
    )
    _, paused = await run(brain, registry, privacy=check)
    (card,) = paused.pending
    events, done = await run(
        brain, registry, list(paused.messages), privacy=check, decisions={card.id: False}
    )
    assert done.stop == "final_answer"
    no = result_of(done.messages, "s1")
    assert no.is_error
    assert "did not want this sent with the name of someone in your life" in no.content
    assert "Do it again without those details" in no.content
    # The second search carried nothing private: it ran without a card.
    assert not [e for e in events if isinstance(e, ApprovalNeeded)]
    assert tavily.queries == ["birthday gift ideas"]


async def test_a_search_about_a_topic_runs_without_asking() -> None:
    tavily = Tavily()
    brain = ScriptedBrain([h.reply(h.search("alcohol-free menus Austin")), Message.assistant("x")])
    events, done = await run(brain, h.registry(tavily), privacy=Knows(Known(people=(SARA,))))
    assert done.stop == "final_answer"
    assert not [e for e in events if isinstance(e, ApprovalNeeded)]
    assert tavily.queries == ["alcohol-free menus Austin"]


async def test_a_search_tying_a_health_detail_to_the_user_asks_first() -> None:
    tavily = Tavily()
    query = "restaurants for my pregnant wife in Austin"
    brain = ScriptedBrain([h.reply(h.search(query)), Message.assistant("x")])
    _, paused = await run(brain, h.registry(tavily), privacy=Knows())
    (card,) = paused.pending
    assert card.privacy == (f"This includes a health detail: “{query}”",)
    assert tavily.queries == []


async def test_a_check_that_cannot_run_asks_instead_of_letting_it_through() -> None:
    tavily = Tavily()
    brain = ScriptedBrain([h.reply(h.search("weather tomorrow")), Message.assistant("x")])
    _, paused = await run(brain, h.registry(tavily), privacy=Knows(broken=True))
    (card,) = paused.pending
    assert card.privacy == (UNCHECKED,)
    assert tavily.queries == []


async def test_without_a_check_nothing_changes() -> None:
    tavily = Tavily()
    brain = ScriptedBrain([h.reply(h.search("gift ideas for Sara")), Message.assistant("x")])
    _, done = await run(brain, h.registry(tavily))
    assert done.stop == "final_answer"
    assert tavily.queries == ["gift ideas for Sara"]


# ---------- gated: an email ----------


async def test_an_email_card_says_what_private_detail_it_includes() -> None:
    tavily = Tavily()
    check = Knows()
    brain = ScriptedBrain([h.reply(h.email(ANNIVERSARY)), Message.assistant("x")])
    _, paused = await run(brain, h.registry(tavily), privacy=check)
    (card,) = paused.pending
    assert card.summary == "Send an email to hello@trattoria.example"
    assert card.privacy == ("This includes a health detail: “My wife is pregnant”",)
    assert h.outbox == []
    assert check.checked == [[Outgoing(ANNIVERSARY, "message")]]


async def test_a_no_to_an_email_that_named_a_detail_offers_to_send_it_without() -> None:
    tavily = Tavily()
    registry = h.registry(tavily)
    check = Knows()
    brain = ScriptedBrain([h.reply(h.email(ANNIVERSARY)), Message.assistant("Not sent.")])
    _, paused = await run(brain, registry, privacy=check)
    (card,) = paused.pending
    _, done = await run(
        brain, registry, list(paused.messages), privacy=check, decisions={card.id: False}
    )
    no = result_of(done.messages, "e1").content
    assert "The card said it includes a health detail" in no
    assert "offer to do it without those details. Do not do it again unasked." in no
    assert h.outbox == []


async def test_an_email_with_only_what_is_needed_has_a_plain_card() -> None:
    tavily = Tavily()
    registry = h.registry(tavily)
    brain = ScriptedBrain([h.reply(h.email(MINIMAL)), Message.assistant("Sent.")])
    _, paused = await run(brain, registry, privacy=Knows())
    (card,) = paused.pending
    assert card.privacy == ()
    _, done = await run(
        brain, registry, list(paused.messages), privacy=Knows(), decisions={card.id: False}
    )
    assert DECLINED in result_of(done.messages, "e1").content  # the usual words


async def test_the_user_can_still_send_what_they_choose() -> None:
    """Never a block: the card names the detail, and a yes sends it exactly as shown."""
    tavily = Tavily()
    registry = h.registry(tavily)
    brain = ScriptedBrain([h.reply(h.email(ANNIVERSARY)), Message.assistant("Sent.")])
    _, paused = await run(brain, registry, privacy=Knows())
    (card,) = paused.pending
    _, done = await run(
        brain, registry, list(paused.messages), privacy=Knows(), decisions={card.id: True}
    )
    assert done.stop == "final_answer"
    assert [e.body for e in h.outbox] == [ANNIVERSARY]


# ---------- a no is final ----------


async def test_a_declined_call_never_runs_even_if_it_would_no_longer_ask() -> None:
    """Regression: the user said no to a search that named Sara; before the turn resumed, they
    told Enzo to forget her. The search then looked harmless and ran despite the no."""
    tavily = Tavily()
    registry = h.registry(tavily)
    check = Knows(Known(people=(SARA,)))
    brain = ScriptedBrain([h.reply(h.search("gift ideas for Sara")), Message.assistant("ok")])
    _, paused = await run(brain, registry, privacy=check)
    (card,) = paused.pending
    check.facts = Known()  # forgotten while the card waited
    _, done = await run(
        brain,
        registry,
        list(paused.messages),
        privacy=check,
        decisions={card.id: False},
        declined={card.call.id},
    )
    assert done.stop == "final_answer"
    assert tavily.queries == []
    assert result_of(done.messages, "s1").content.endswith(f'"message":"{DECLINED}"}}')
