"""Spec §12.7's third check, end to end with Gmail and Calendar (a fake Google over HTTP, the real
MCP SDK, the real loop and the real outbound check over the database): ask Enzo to share a private
detail with a third party, and it asks first or shares only what is needed (ADR 0029)."""

from pathlib import Path
from typing import Any

import pytest

from enzo.agent.loop import run_turn
from enzo.brain import Message, ToolResult
from enzo.brain.fake import ScriptedBrain
from enzo.memory import Memory
from enzo.privacy import Discretion, Rules
from enzo.store.repository import Store
from enzo.tools import ToolRegistry
from tests.connectors.fake_google import FakeGoogle
from tests.connectors.test_google import asked, call, google, history_of
from tests.connectors.test_mcp import router_for

ASK = (
    "Email hello@trattoria.example to book a table for 2 on Friday at 19:30 for my "
    "anniversary, my wife is pregnant so no alcohol."
)
#: What an over-sharing model writes: the reason for the dinner and the pregnancy.
OVERSHARE = (
    "Hello,\n\nI'd like to book a table for 2 this Friday at 19:30 for our anniversary. My wife "
    "is pregnant, so no alcohol for us please.\n\nThank you,\nAtishay"
)
#: What the restaurant needs: a name, a time and the party size.
MINIMAL = (
    "Hello,\n\nI'd like to book a table for 2 this Friday at 19:30 under the name Atishay.\n\n"
    "Thank you,\nAtishay"
)


def send(body: str, cid: str) -> Any:
    return call(
        "gmail_send_message",
        cid=cid,
        to=["hello@trattoria.example"],
        subject="Table for 2 on Friday",
        body=body,
    )


async def turn(
    registry: ToolRegistry,
    brain: ScriptedBrain,
    history: list[Message],
    decided: dict[str, bool],
    privacy: Discretion,
) -> list[Any]:
    return [
        e
        async for e in run_turn(
            router_for(brain), registry, history, decisions=decided, privacy=privacy
        )
    ]


async def test_a_booking_email_never_carries_the_pregnancy_unasked(
    tmp_path: Path, rules: Rules
) -> None:
    connectors, fake, _ = await google(tmp_path)
    registry = ToolRegistry(await connectors.tools())
    privacy = Discretion(rules)
    brain = ScriptedBrain(
        [
            Message.assistant(tool_calls=(send(OVERSHARE, "c1"),)),
            Message.assistant("Not sent. Shall I send it without mentioning the pregnancy?"),
            Message.assistant(tool_calls=(send(MINIMAL, "c2"),)),
            Message.assistant("Sent: a table for 2 on Friday at 19:30."),
        ]
    )
    # 1. The over-shared email waits on a card that says what it gives away.
    events = await turn(registry, brain, [Message.user(ASK)], {}, privacy)
    (card,) = asked(events)
    assert (
        card.summary == "Gmail: send an email to hello@trattoria.example (“Table for 2 on Friday”)"
    )
    assert card.privacy == ("This includes a health detail: “My wife is pregnant”",)
    assert "My wife is pregnant" in card.detail  # the exact email, as it would go out
    assert fake.sent == []

    # 2. The user says no: nothing is sent, and the brain is told why the card asked.
    events = await turn(registry, brain, history_of(events), {card.id: False}, privacy)
    history = history_of(events)
    (no,) = [p for m in history for p in m.parts if isinstance(p, ToolResult)]
    assert "The card said it includes a health detail" in no.content
    assert fake.sent == []

    # 3. "Yes, without it": the email the restaurant gets has only what it needs.
    history.append(Message.user("yes, send it without that"))
    events = await turn(registry, brain, history, {card.id: False}, privacy)
    (plain,) = asked(events)
    assert plain.privacy == ()
    events = await turn(
        registry, brain, history_of(events), {card.id: False, plain.id: True}, privacy
    )
    assert [m["body"] for m in fake.sent] == [MINIMAL]
    assert not any("pregnan" in m["body"].lower() for m in fake.sent)


async def test_a_forward_names_what_the_forwarded_email_gives_away(
    tmp_path: Path, rules: Rules, monkeypatch: pytest.MonkeyPatch
) -> None:
    connectors, fake, _ = await google(tmp_path)
    clinic = {
        **FakeGoogle.INBOX["m1"],
        "id": "m9",
        "sender": "results@clinic.example",
        "subject": "Your results",
        "plaintextBody": "Your biopsy came back clear. Your diabetes review is on 3 Dec.",
    }
    monkeypatch.setitem(FakeGoogle.INBOX, "m9", clinic)
    registry = ToolRegistry(await connectors.tools())
    forward = call("gmail_forward", messageId="m9", to=["boss@work.example"], forwardText="FYI")
    brain = ScriptedBrain([Message.assistant(tool_calls=(forward,))])
    events = await turn(
        registry, brain, [Message.user("forward it to my boss")], {}, Discretion(rules)
    )
    (card,) = asked(events)
    assert card.privacy == ("This includes a health detail: “Your diabetes review is on 3 Dec”",)
    assert fake.sent == []


async def test_an_invite_names_a_private_detail_but_not_the_place_it_asks_for(
    tmp_path: Path, rules: Rules, store: Store
) -> None:
    connectors, _, _ = await google(tmp_path)
    memory = Memory(store.engine, tmp_path / "Enzo")
    await memory.remember("Lives at 27 Quillon Road.", kind="about")
    await memory.remember("Wife, pregnant (due in March).", kind="person", about="Saoirse")
    registry = ToolRegistry(await connectors.tools())
    invite = call(
        "calendar_create_event",
        summary="Dinner at ours",
        startTime="2026-12-04T19:00:00",
        endTime="2026-12-04T22:00:00",
        location="27 Quillon Road",
        description="Saoirse is cooking. Bring nothing!",
        attendeeEmails=["tom@example.com"],
    )
    brain = ScriptedBrain([Message.assistant(tool_calls=(invite,))])
    events = await turn(
        registry, brain, [Message.user("invite tom to dinner")], {}, Discretion(rules, memory)
    )
    (card,) = asked(events)
    # The place is what a location asks for; the name of the user's wife is not.
    assert card.privacy == (
        "This includes the name of someone in your life: “Saoirse” (your wife)",
    )
