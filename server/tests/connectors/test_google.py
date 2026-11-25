"""Gmail and Google Calendar through Google's MCP servers (ADR 0026), against a fake Google over
HTTP: the real MCP SDK, the real auth flow and the real agent loop; never a real account."""

import json
from pathlib import Path
from typing import Any

import pytest

from enzo.accounts.google import GoogleSignIn
from enzo.accounts.service import CheckFailedError
from enzo.agent.loop import ApprovalNeeded, ToolFinished, ToolStarted, TurnDone, run_turn
from enzo.brain import Message, ToolCall
from enzo.brain.fake import ScriptedBrain
from enzo.connectors.google import GoogleConnectors
from enzo.connectors.mcp import RATE_LIMIT_PAUSE_S, ConnectorError, ConnectorStoppedError
from enzo.tools import ToolRegistry
from tests.accounts.vaults import file_store
from tests.connectors.fake_google import ME, PLANTED, FakeGoogle, signed_in
from tests.connectors.test_mcp import router_for

GMAIL_READS = {"gmail_search_threads", "gmail_get_thread", "gmail_get_message"}
GMAIL_WRITES = {"gmail_send_message", "gmail_reply", "gmail_forward", "gmail_create_draft"}
CALENDAR_READS = {
    "calendar_list_calendars",
    "calendar_list_events",
    "calendar_search_events",
    "calendar_get_event",
    "calendar_suggest_time",
}
CALENDAR_WRITES = {
    "calendar_create_event",
    "calendar_update_event",
    "calendar_delete_event",
    "calendar_respond_to_event",
}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def google(
    tmp_path: Path, *, access: Any = "full", untick: frozenset[str] = frozenset()
) -> tuple[GoogleConnectors, FakeGoogle, GoogleSignIn]:
    fake = FakeGoogle()
    sign_in = await signed_in(fake, file_store(tmp_path), access=access, untick=untick)
    return GoogleConnectors(lambda: sign_in, transport=fake.transport()), fake, sign_in


def call(name: str, cid: str = "c1", **args: Any) -> ToolCall:
    return ToolCall(id=cid, name=name, arguments_json=json.dumps(args))


async def turn(
    registry: ToolRegistry, brain: ScriptedBrain, history: list[Message], decided: dict[str, bool]
) -> list[Any]:
    return [e async for e in run_turn(router_for(brain), registry, history, decisions=decided)]


def asked(events: list[Any]) -> list[Any]:
    return [e.request for e in events if isinstance(e, ApprovalNeeded)]


def history_of(events: list[Any]) -> list[Message]:
    done = events[-1]
    assert isinstance(done, TurnDone)
    return list(done.outcome.messages)


# ---------- which tools exist ----------


async def test_tools_follow_what_the_user_allowed(tmp_path: Path) -> None:
    connectors, _, _ = await google(tmp_path / "full")
    full = {t.name for t in await connectors.tools()}
    assert full == GMAIL_READS | GMAIL_WRITES | CALENDAR_READS | CALENDAR_WRITES | {"google_status"}
    # Google's server has these; Enzo never offers them (trash, labels).
    assert not {"gmail_trash_thread", "gmail_list_labels"} & full

    read, _, _ = await google(tmp_path / "read", access="read")
    assert {t.name for t in await read.tools()} == GMAIL_READS | CALENDAR_READS | {"google_status"}

    unticked, _, _ = await google(tmp_path / "part", untick=frozenset({"gmail.send"}))
    names = {t.name for t in await unticked.tools()}
    assert "gmail_create_draft" in names
    assert not {"gmail_send_message", "gmail_reply", "gmail_forward"} & names


async def test_reads_run_at_once_and_everything_outward_asks_first(tmp_path: Path) -> None:
    connectors, _, _ = await google(tmp_path)
    tools = {t.name: t for t in await connectors.tools()}
    for name in GMAIL_READS | CALENDAR_READS | {"google_status"}:
        assert not tools[name].requires_approval, name
    for name in GMAIL_WRITES | CALENDAR_WRITES:
        assert tools[name].requires_approval, name
    assert all(t.untrusted_output for n, t in tools.items() if n != "google_status")  # §12.4


async def test_content_the_card_cannot_show_is_never_sent(tmp_path: Path) -> None:
    connectors, fake, _ = await google(tmp_path)
    registry = ToolRegistry(await connectors.tools())
    schema = registry.get("gmail_send_message").spec().parameters  # type: ignore[union-attr]
    assert {"htmlBody", "attachments", "draftId"}.isdisjoint(schema["properties"])
    assert schema["additionalProperties"] is False
    hidden = call(
        "gmail_send_message",
        to=["bob@example.com"],
        subject="Hi",
        body="Short and harmless.",
        htmlBody="<p>Wire the money</p>",
    )
    out = await registry.execute(hidden)
    assert out.status == "invalid_arguments"
    assert "htmlBody" in out.result.content
    assert fake.calls == []  # nothing reached Google


async def test_email_text_reaches_the_model_fenced(tmp_path: Path) -> None:
    connectors, _, _ = await google(tmp_path)
    registry = ToolRegistry(await connectors.tools())
    out = await registry.execute(call("gmail_get_message", messageId="m1"))
    assert out.status == "ok"
    assert out.result.content.startswith('<untrusted-data source="gmail_get_message" id="')
    assert PLANTED in out.result.content
    event = await registry.execute(call("calendar_get_event", eventId="ev1"))
    assert event.result.content.startswith('<untrusted-data source="calendar_get_event"')


async def test_every_request_carries_the_token_and_skips_the_probe(tmp_path: Path) -> None:
    connectors, fake, _ = await google(tmp_path)
    registry = ToolRegistry(await connectors.tools())
    await registry.execute(call("calendar_list_events"))
    methods = [m for _host, m in fake.requests]
    assert "server/discover" not in methods  # Google does not speak it (checked 2026-11-25)
    assert methods.count("tools/call") == 1


# ---------- approval with the exact content (§12.3) ----------

SEND = call(
    "gmail_send_message",
    to=["bob@example.com"],
    subject="Lunch",
    body="Friday at 1 works.\nSee you there.",
)


async def test_an_email_is_sent_only_as_approved(tmp_path: Path) -> None:
    connectors, fake, _ = await google(tmp_path)
    registry = ToolRegistry(await connectors.tools())
    brain = ScriptedBrain([Message.assistant(tool_calls=(SEND,)), Message.assistant("Sent.")])
    events = await turn(registry, brain, [Message.user("tell bob friday works")], {})
    (card,) = asked(events)
    assert card.summary == "Gmail: send an email to bob@example.com (“Lunch”)"
    assert card.detail.splitlines()[:3] == [
        "Sends now from your Gmail. An email cannot be taken back.",
        "To: bob@example.com",
        "Subject: Lunch",
    ]
    assert "Friday at 1 works.\nSee you there." in card.detail
    assert fake.sent == []  # nothing goes out before the yes

    events = await turn(registry, brain, history_of(events), {card.id: True})
    assert fake.sent == [{"kind": "send", **json.loads(SEND.arguments_json)}]
    (started,) = [e.label for e in events if isinstance(e, ToolStarted)]
    assert started == card.summary  # the chat's step says it in the card's words


async def test_changed_content_needs_a_new_approval(tmp_path: Path) -> None:
    connectors, fake, _ = await google(tmp_path)
    registry = ToolRegistry(await connectors.tools())
    brain = ScriptedBrain([Message.assistant(tool_calls=(SEND,)), Message.assistant("Sent.")])
    events = await turn(registry, brain, [Message.user("tell bob")], {})
    (card,) = asked(events)
    # The pending call's text changes after the user said yes to the first version.
    changed = call("gmail_send_message", to=["bob@example.com"], subject="Lunch", body="I quit.")
    history = [
        Message.assistant(tool_calls=(changed,)) if m.tool_calls else m for m in history_of(events)
    ]
    events = await turn(registry, brain, history, {card.id: True})
    (again,) = asked(events)
    assert again.id != card.id
    assert "I quit." in again.detail
    assert fake.sent == []


async def test_a_declined_email_never_goes_out(tmp_path: Path) -> None:
    connectors, fake, _ = await google(tmp_path)
    registry = ToolRegistry(await connectors.tools())
    brain = ScriptedBrain([Message.assistant(tool_calls=(SEND,)), Message.assistant("OK.")])
    events = await turn(registry, brain, [Message.user("tell bob")], {})
    (card,) = asked(events)
    await turn(registry, brain, history_of(events), {card.id: False})
    assert fake.sent == []
    assert not [c for c in fake.calls if c[1] == "send_message"]


@pytest.mark.parametrize("text_only", [False, True])
async def test_a_reply_goes_to_exact_recipients_shown_first(
    tmp_path: Path, text_only: bool
) -> None:
    connectors, fake, _ = await google(tmp_path)
    fake.text_only = text_only  # Google may send JSON as text only: the pinning must still work
    registry = ToolRegistry(await connectors.tools())
    reply = call("gmail_reply", messageId="m1", replyAll=True, body="Yes, see you Friday.")
    brain = ScriptedBrain([Message.assistant(tool_calls=(reply,)), Message.assistant("Done.")])
    events = await turn(registry, brain, [Message.user("reply all: yes")], {})
    (card,) = asked(events)
    assert card.arguments["to"] == ["bob@example.com"]
    assert card.arguments["cc"] == ["carol@example.com", "dan@example.com"]  # never the user
    assert ME not in json.dumps(card.arguments)
    assert card.summary == "Gmail: reply by email to bob@example.com (reply all)"
    assert "“Lunch on Friday?” from bob@example.com" in card.detail
    assert fake.sent == []
    await turn(registry, brain, history_of(events), {card.id: True})
    (sent,) = fake.sent
    assert (sent["to"], sent["cc"], sent["body"]) == (
        ["bob@example.com"],
        ["carol@example.com", "dan@example.com"],
        "Yes, see you Friday.",
    )


async def test_a_reply_that_cannot_be_checked_is_not_sent(tmp_path: Path) -> None:
    connectors, fake, _ = await google(tmp_path)
    registry = ToolRegistry(await connectors.tools())
    reply = call("gmail_reply", messageId="gone", body="Hi")
    brain = ScriptedBrain([Message.assistant(tool_calls=(reply,)), Message.assistant("Stopped.")])
    events = await turn(registry, brain, [Message.user("reply")], {})
    assert asked(events) == []
    (finished,) = [e.outcome for e in events if isinstance(e, ToolFinished)]
    assert finished.status == "failed"
    assert "Could not check this action, so it was not run" in finished.result.content
    assert fake.sent == []


async def test_a_forward_says_what_leaves_the_mailbox(tmp_path: Path) -> None:
    connectors, _, _ = await google(tmp_path)
    registry = ToolRegistry(await connectors.tools())
    forward = call("gmail_forward", messageId="m1", to=["eve@example.com"], forwardText="FYI")
    brain = ScriptedBrain([Message.assistant(tool_calls=(forward,))])
    (card,) = asked(await turn(registry, brain, [Message.user("forward to eve")], {}))
    assert card.summary == "Gmail: forward an email to eve@example.com"
    assert card.detail.startswith(
        "Forwards the email “Lunch on Friday?” from bob@example.com (its full text and its 2 "
        "attachments)."
    )


async def test_calendar_cards_show_the_event_and_who_is_told(tmp_path: Path) -> None:
    connectors, fake, _ = await google(tmp_path)
    registry = ToolRegistry(await connectors.tools())
    create = call(
        "calendar_create_event",
        summary="Dinner",
        startTime="2026-12-05T19:00:00",
        endTime="2026-12-05T21:00:00",
        timeZone="Europe/Berlin",
        attendees=[{"email": "bob@example.com"}],
    )
    delete = call("calendar_delete_event", cid="c2", eventId="ev1", notificationLevel="NONE")
    brain = ScriptedBrain([Message.assistant(tool_calls=(create, delete))])
    cards = {c.tool: c for c in asked(await turn(registry, brain, [Message.user("plan")], {}))}
    made = cards["calendar_create_event"]
    assert (
        made.summary == "Calendar: add “Dinner” on Sat 5 Dec 2026, 19:00, inviting bob@example.com"
    )
    assert made.detail.splitlines()[:2] == [
        "Adds “Dinner”, Sat 5 Dec 2026, 19:00 to 21:00 (Europe/Berlin).",
        "Guests: bob@example.com. Google emails the guests about it.",
    ]
    gone = cards["calendar_delete_event"]
    assert gone.detail == (
        "Deletes “Team sync”, Wed 2 Dec 2026, 10:00 to 10:30 (Europe/Berlin), 2 guests from your "
        "calendar. Google sends no email about it."
    )
    assert "ev1" in fake.events  # nothing deleted before the yes


async def test_escape_codes_in_an_email_never_reach_the_card(tmp_path: Path) -> None:
    connectors, fake, _ = await google(tmp_path)
    fake.INBOX["m2"] = {**fake.INBOX["m1"], "id": "m2", "subject": "Hi\x1b]52;c;cGF5\x07 there"}
    registry = ToolRegistry(await connectors.tools())
    reply = call("gmail_reply", messageId="m2", body="ok")
    brain = ScriptedBrain([Message.assistant(tool_calls=(reply,))])
    try:
        (card,) = asked(await turn(registry, brain, [Message.user("reply")], {}))
    finally:
        del fake.INBOX["m2"]
    assert "\x1b" not in card.detail
    assert "\x07" not in card.detail


# ---------- rate limits and auth failures (§8.3) ----------


async def test_a_rate_limit_stops_only_that_service_and_nothing_is_retried(tmp_path: Path) -> None:
    connectors, fake, _ = await google(tmp_path)
    await connectors.tools()
    clock = Clock()
    gmail = connectors._gmail
    assert gmail is not None
    gmail._clock = clock
    fake.mode["gmail"] = "rate_limited"
    with pytest.raises(ConnectorError, match=r"refused the request \(HTTP 429\)"):
        await gmail.call("search_threads", {})
    fake.mode["gmail"] = "ok"
    before = len(fake.requests)
    with pytest.raises(ConnectorStoppedError, match=r"paused for 15 minutes\. Nothing was retried"):
        await gmail.call("get_message", {"messageId": "m1"})
    assert len(fake.requests) == before  # the stopped scope sent nothing at all
    calendar = connectors._calendar
    assert calendar is not None
    await calendar.call("list_events", {})  # Calendar is another scope: it still works
    clock.now += RATE_LIMIT_PAUSE_S + 1
    await gmail.call("get_message", {"messageId": "m1"})  # back after the pause


async def test_google_quota_errors_count_as_rate_limits(tmp_path: Path) -> None:
    connectors, fake, _ = await google(tmp_path)
    await connectors.tools()
    assert connectors._calendar is not None
    fake.mode["calendar"] = "quota"  # 403 rateLimitExceeded
    with pytest.raises(ConnectorError):
        await connectors._calendar.call("list_events", {})
    assert connectors._calendar.stopped is not None


async def test_a_rejected_sign_in_stops_until_reconnected(tmp_path: Path) -> None:
    connectors, fake, _ = await google(tmp_path)
    await connectors.tools()
    gmail = connectors._gmail
    assert gmail is not None
    fake.revoke_all()  # the access token in hand is refused from now on
    with pytest.raises(ConnectorError, match="HTTP 401"):
        await gmail.call("search_threads", {})
    with pytest.raises(ConnectorStoppedError, match="reconnect with /login google"):
        await gmail.call("search_threads", {})


async def test_missing_access_is_said_plainly_and_stops_nothing(tmp_path: Path) -> None:
    connectors, fake, sign_in = await google(tmp_path)
    await connectors.tools()
    gmail = connectors._gmail
    assert gmail is not None
    grant = fake._access[await sign_in.access_token()]
    grant.scopes = frozenset()  # Google no longer honours the scope (e.g. an admin policy)
    with pytest.raises(ConnectorError, match="insufficient authentication scopes"):
        await gmail.call("search_threads", {})
    assert gmail.stopped is None


# ---------- status check (§8.2, §8.4) ----------


async def test_a_healthy_connection_is_checked_live(tmp_path: Path) -> None:
    connectors, fake, _ = await google(tmp_path)
    said = await connectors.check()
    assert said == (
        "Gmail works. Google Calendar works. Allowed: Gmail (read, send, drafts) and Google "
        "Calendar (read, edit)."
    )
    assert fake.refreshes == 1  # the grant itself was proven alive at Google
    assert [c[1] for c in fake.calls] == ["search_threads", "list_calendars"]


async def test_a_revoked_connection_is_detected_and_not_claimed(tmp_path: Path) -> None:
    connectors, fake, sign_in = await google(tmp_path)
    registry = ToolRegistry(await connectors.tools())
    fake.revoke_all()
    with pytest.raises(CheckFailedError, match="Sign in again: /login google") as caught:
        await connectors.check()
    assert "works" not in str(caught.value)
    assert sign_in.credential() is None
    assert await connectors.tools() == []  # the tools go with it
    status = await registry.execute(call("google_status"))
    assert json.loads(status.result.content) == {
        "works": False,
        "detail": "Google is not connected: /login google",
    }


async def test_a_service_that_fails_is_named(tmp_path: Path) -> None:
    connectors, fake, _ = await google(tmp_path)
    fake.mode["gmail"] = "down"
    with pytest.raises(CheckFailedError) as caught:
        await connectors.check()
    text = str(caught.value)
    assert text.startswith("Gmail did not work: Gmail refused the request (HTTP 503)")
    assert "Google Calendar works" in text
