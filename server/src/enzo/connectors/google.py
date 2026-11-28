"""Gmail and Google Calendar (spec §8, ADR 0026): Google's own remote MCP servers.

Setup and operations: docs/connectors/google.md. Auth: Sign in with Google
(``enzo.accounts.google``); every request carries a fresh access token from the vault.

- **Servers:** ``gmailmcp.googleapis.com`` and ``calendarmcp.googleapis.com`` (Google-hosted,
  read-only hints on every read; tool lists and per-tool scopes read 2026-11-25).
- **Least privilege:** an allowlist per server, then only the tools whose scopes the user granted.
  Nothing that trashes, labels or marks spam is offered.
- **Approval with the exact content (§12.3):** sending, replying, forwarding, saving a draft, and
  creating, changing, deleting or answering an event wait for the user's approval. The card shows
  the email or event as it will go out; parameters the card can't show faithfully (HTML bodies,
  attachments, sending a draft "as is") are not offered, and unknown ones are refused. A reply is
  pinned to explicit recipients before asking. Changed content means a new approval (ADR 0014).
- **Untrusted content (§12.4):** emails and events are outside text: every result is fenced.
- **§8.3:** a rate limit pauses that service (Gmail or Calendar) for 15 minutes with no retry; a
  rejected sign-in stops it until the user reconnects with ``/login google``.
"""

import logging
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

import httpx2
import mcp_types
from mcp import Client
from pydantic import BaseModel, ConfigDict

from enzo import __version__
from enzo.accounts.google import (
    CALENDAR_FREEBUSY,
    CALENDAR_LIST,
    CALENDAR_READ,
    CALENDAR_WRITE,
    GMAIL_DRAFT,
    GMAIL_READ,
    GMAIL_SEND,
    GoogleSignIn,
    describe_access,
)
from enzo.accounts.service import CheckFailedError
from enzo.brain.errors import AuthError, BrainError
from enzo.connectors.mcp import (
    CallFn,
    ConnectorError,
    McpConnector,
    McpServer,
    PrepareFn,
    Ready,
    http_transport,
)
from enzo.tools.registry import Tool, tool

log = logging.getLogger("enzo.connectors.google")

GMAIL_MCP_URL = "https://gmailmcp.googleapis.com/mcp/v1"
CALENDAR_MCP_URL = "https://calendarmcp.googleapis.com/mcp/v1"
LOGIN = "/login google"

#: Google's 403 and 429 wordings for quotas (Google API error reasons), besides "rate limit".
RATE_LIMIT_PHRASES = ("quota exceeded", "ratelimitexceeded", "resource_exhausted")

type Needs = tuple[frozenset[str], ...]


def _any(*scopes: str) -> frozenset[str]:
    return frozenset(scopes)


#: Gmail tools Enzo offers, and the scopes each needs (every group, any scope of a group).
#: A reply or forward also reads the original email to show it on the card.
GMAIL_NEEDS: Mapping[str, Needs] = {
    "search_threads": (_any(GMAIL_READ),),
    "get_thread": (_any(GMAIL_READ),),
    "get_message": (_any(GMAIL_READ),),
    "send_message": (_any(GMAIL_SEND),),
    "reply": (_any(GMAIL_SEND), _any(GMAIL_READ)),
    "forward": (_any(GMAIL_SEND), _any(GMAIL_READ)),
    "create_draft": (_any(GMAIL_DRAFT),),
}
_EVENTS_READ = _any(CALENDAR_READ, CALENDAR_WRITE)
CALENDAR_NEEDS: Mapping[str, Needs] = {
    "list_calendars": (_any(CALENDAR_LIST),),
    "list_events": (_EVENTS_READ,),
    "search_events": (_EVENTS_READ,),
    "get_event": (_EVENTS_READ,),
    "suggest_time": (_any(CALENDAR_FREEBUSY, CALENDAR_WRITE),),
    "create_event": (_any(CALENDAR_WRITE),),
    "update_event": (_any(CALENDAR_WRITE),),
    "delete_event": (_any(CALENDAR_WRITE),),
    "respond_to_event": (_any(CALENDAR_WRITE),),
}

#: Not offered: what the approval card cannot show as it will go out.
GMAIL_DROP: Mapping[str, frozenset[str]] = {
    "send_message": frozenset({"attachments", "htmlBody", "draftId", "replyToMessageId"}),
    "reply": frozenset({"attachments", "htmlBody"}),
    "forward": frozenset({"attachments", "htmlBody"}),
    "create_draft": frozenset({"attachments", "htmlBody"}),
}
CALENDAR_DROP: Mapping[str, frozenset[str]] = {
    "create_event": frozenset({"attachments"}),
    "update_event": frozenset({"addedAttachments", "removedAttachmentFileUrls"}),
}


def allowed(needs: Mapping[str, Needs], granted: frozenset[str]) -> frozenset[str]:
    """The tools whose scopes were all granted."""
    return frozenset(n for n, groups in needs.items() if all(g & granted for g in groups))


# ---------- what the user sees ----------


def _clean(text: object, *, line: bool = False) -> str:
    """Outside text on a card: printable only (a terminal would run escape codes)."""
    raw = str(text)
    keep = "" if line else "\n"
    cleaned = "".join(c if c.isprintable() or c in keep else " " for c in raw)
    return " ".join(cleaned.split()) if line else cleaned


def _who(values: object) -> str:
    if not isinstance(values, list) or not values:
        return "no one"
    return ", ".join(_clean(v, line=True) for v in values)


def _subject(args: Mapping[str, Any]) -> str:
    return _clean(args.get("subject") or "(no subject)", line=True)


def _email(args: Mapping[str, Any], intro: str) -> str:
    lines = [intro]
    for label, key in (("To", "to"), ("Cc", "cc"), ("Bcc", "bcc")):
        if args.get(key):
            lines.append(f"{label}: {_who(args[key])}")
    if "subject" in args:
        lines.append(f"Subject: {_subject(args)}")
    if args.get("includeSignature"):
        lines.append("Your Gmail signature is added at the end.")
    text = args.get("body") or args.get("forwardText")
    lines += ["", _clean(text) if text else "(no text)"]
    return "\n".join(lines)


async def _read(call: CallFn, tool_name: str, args: dict[str, Any], what: str) -> dict[str, Any]:
    try:
        data = (await call(tool_name, args)).payload()
    except ValueError as exc:
        msg = f"could not read {what}, so nothing was prepared"
        raise ConnectorError(msg) from exc
    if not isinstance(data, dict):
        msg = f"could not read {what}, so nothing was prepared"
        raise ConnectorError(msg)
    return data


async def _send(args: dict[str, Any], _call: CallFn) -> tuple[dict[str, Any], str | None]:
    return args, _email(args, "Sends now from your Gmail. An email cannot be taken back.")


async def _draft(args: dict[str, Any], _call: CallFn) -> tuple[dict[str, Any], str | None]:
    return args, _email(args, "Saved as a draft in your Gmail. Nothing is sent.")


def _reply_pin(me: str | None) -> PrepareFn:
    """A reply goes to exact recipients, shown before asking: the sender, and for reply-all the
    others on the email (never the user). The brain's own recipients, if it gave some, win."""

    async def pin(args: dict[str, Any], call: CallFn) -> tuple[dict[str, Any], str | None]:
        message = {"messageId": args["messageId"], "messageFormat": "MINIMAL"}
        original = await _read(call, "get_message", message, "the email to reply to")
        sender = original.get("sender")
        if not isinstance(sender, str) or not sender:
            msg = "could not read who sent that email, so the reply was not prepared"
            raise ConnectorError(msg)
        exact = dict(args)
        if not args.get("to"):
            exact["to"] = [sender]
        if args.get("replyAll") and not args.get("cc"):
            skip = {sender.lower(), (me or "").lower()} | {a.lower() for a in exact["to"]}
            others = [
                a
                for key in ("toRecipients", "ccRecipients")
                for a in original.get(key) or []
                if isinstance(a, str) and a.lower() not in skip
            ]
            if others:
                exact["cc"] = list(dict.fromkeys(others))
        about = _clean(original.get("subject") or "(no subject)", line=True)
        when = _clean(original.get("date") or "", line=True)
        intro = (
            f"Replies in the thread “{about}” from {_clean(sender, line=True)}"
            + (f" ({when})" if when else "")
            + ". Sends now; an email cannot be taken back."
        )
        return exact, _email(exact, intro)

    return pin


async def _forward(args: dict[str, Any], call: CallFn) -> Ready:
    message = {"messageId": args["messageId"], "messageFormat": "PLAIN_TEXT"}
    original = await _read(call, "get_message", message, "the email to forward")
    about = _clean(original.get("subject") or "(no subject)", line=True)
    sender = _clean(original.get("sender") or "someone", line=True)
    files = original.get("attachmentIds") or []
    carried = f" and its {len(files)} attachment{'s' if len(files) != 1 else ''}" if files else ""
    intro = (
        f"Forwards the email “{about}” from {sender} (its full text{carried}). "
        "Sends now; an email cannot be taken back."
    )
    # The forwarded email's own words go out too: checked for private details (ADR 0029).
    body = original.get("plaintextBody")
    return Ready(args, _email(args, intro), sends=(body,) if isinstance(body, str) else ())


#: What each Gmail write sends to its recipients (a draft stays in the user's own mailbox).
GMAIL_OUTBOUND: Mapping[str, Mapping[str, str | None]] = {
    "send_message": {"subject": None, "body": None},
    "reply": {"body": None},
    "forward": {"forwardText": None},
    "create_draft": {},
}
#: What each Calendar write tells the guests; the place is a field that asks for one.
_EVENT_TEXT: Mapping[str, str | None] = {
    "summary": None,
    "description": None,
    "location": "Location",
}
CALENDAR_OUTBOUND: Mapping[str, Mapping[str, str | None]] = {
    "create_event": _EVENT_TEXT,
    "update_event": _EVENT_TEXT,
    "respond_to_event": {"responseComment": None},
    "delete_event": {},
}


def _send_summary(args: dict[str, Any]) -> str:
    return f"Gmail: send an email to {_who(args.get('to'))} (“{_subject(args)}”)"


def _reply_summary(args: dict[str, Any]) -> str:
    everyone = " (reply all)" if args.get("replyAll") else ""
    return f"Gmail: reply by email to {_who(args.get('to'))}{everyone}"


def _forward_summary(args: dict[str, Any]) -> str:
    return f"Gmail: forward an email to {_who(args.get('to'))}"


def _draft_summary(args: dict[str, Any]) -> str:
    return f"Gmail: save a draft to {_who(args.get('to'))} (“{_subject(args)}”)"


def _search_words(args: dict[str, Any]) -> str:
    query = args.get("query")
    return (
        f"Searching your email for “{_clean(query, line=True)}”"
        if query
        else "Looking at your email"
    )


def gmail_server(me: str | None, granted: frozenset[str]) -> McpServer:
    return McpServer(
        name="gmail",
        label="Gmail",
        allow=allowed(GMAIL_NEEDS, granted),
        prepare={
            "send_message": _send,
            "create_draft": _draft,
            "reply": _reply_pin(me),
            "forward": _forward,
        },
        summaries={
            "send_message": _send_summary,
            "reply": _reply_summary,
            "forward": _forward_summary,
            "create_draft": _draft_summary,
        },
        activities={
            "search_threads": _search_words,
            "get_thread": lambda _a: "Reading an email conversation",
            "get_message": lambda _a: "Reading an email",
        },
        drop=GMAIL_DROP,
        outbound=GMAIL_OUTBOUND,
        strict=True,
        login=LOGIN,
        rate_limit_phrases=RATE_LIMIT_PHRASES,
    )


def _parse(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _moment(value: object) -> str:
    at = _parse(value)
    return at.strftime("%a %-d %b %Y, %H:%M") if at else _clean(value, line=True)


def _when(args: Mapping[str, Any]) -> str:
    start, end = args.get("startTime"), args.get("endTime")
    if start is None:
        return "at its current time"
    span = _moment(start)
    if end is not None:
        began, ends = _parse(start), _parse(end)
        same_day = began is not None and ends is not None and began.date() == ends.date()
        span = f"{span} to {ends.strftime('%H:%M') if same_day and ends else _moment(end)}"
    if args.get("allDay"):
        span = f"{span} (all day)"
    zone = args.get("timeZone")
    return f"{span} ({_clean(zone, line=True)})" if zone else f"{span} (your calendar's time zone)"


def _guests(args: Mapping[str, Any], key: str, emails_key: str) -> list[str]:
    people = [a.get("email") for a in args.get(key) or [] if isinstance(a, dict) and a.get("email")]
    people += [e for e in args.get(emails_key) or [] if isinstance(e, str)]
    return [_clean(p, line=True) for p in people]


def _notices(args: Mapping[str, Any], default: str = "ALL") -> str:
    level = args.get("notificationLevel") or default
    return {
        "NONE": "Google sends no email about it.",
        "EXTERNAL_ONLY": "Google emails guests outside your organization.",
    }.get(level, "Google emails the guests about it.")


def _event_lines(args: Mapping[str, Any]) -> list[str]:
    lines = []
    if args.get("location"):
        lines.append(f"Where: {_clean(args['location'], line=True)}")
    if args.get("addGoogleMeetUrl") or args.get("googleMeetUrl"):
        lines.append("With a Google Meet link.")
    if args.get("recurrenceData"):
        lines.append(f"Repeats: {_clean('; '.join(map(str, args['recurrenceData'])), line=True)}")
    if args.get("visibility"):
        lines.append(f"Visible as: {_clean(args['visibility'], line=True)}")
    if args.get("calendarId"):
        lines.append(f"Calendar: {_clean(args['calendarId'], line=True)}")
    if args.get("description"):
        lines += ["", _clean(args["description"])]
    return lines


async def _create_card(args: dict[str, Any], _call: CallFn) -> tuple[dict[str, Any], str | None]:
    lines = [f"Adds “{_clean(args.get('summary', ''), line=True)}”, {_when(args)}."]
    guests = _guests(args, "attendees", "attendeeEmails")
    if guests:
        lines.append(f"Guests: {', '.join(guests)}. {_notices(args)}")
    lines += _event_lines(args)
    return args, "\n".join(lines)


def _event_args(args: Mapping[str, Any]) -> dict[str, Any]:
    where = {"eventId": args["eventId"]}
    if args.get("calendarId"):
        where["calendarId"] = args["calendarId"]
    return where


def _part(data: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    return value if isinstance(value, dict) else {}


def _describe_event(event: Mapping[str, Any]) -> str:
    title = _clean(event.get("summary") or "(untitled event)", line=True)
    start, end = _part(event, "start"), _part(event, "end")
    when = _when(
        {
            "startTime": start.get("dateTime") or start.get("date"),
            "endTime": end.get("dateTime") or end.get("date"),
            "timeZone": start.get("timeZone"),
        }
    )
    guests = [a for a in event.get("attendees") or [] if isinstance(a, dict)]
    extra = f", {len(guests)} guest{'s' if len(guests) != 1 else ''}" if guests else ""
    return f"“{title}”, {when}{extra}"


async def _update_card(args: dict[str, Any], call: CallFn) -> tuple[dict[str, Any], str | None]:
    event = await _read(call, "get_event", _event_args(args), "the event to change")
    lines = [f"Changes {_describe_event(event)}:"]
    if "summary" in args:
        lines.append(f"- Title: {_clean(args['summary'], line=True)}")
    if "startTime" in args or "endTime" in args:
        lines.append(f"- Time: {_when(args)}")
    added = _guests(args, "addedAttendees", "addedAttendeeEmails")
    if added:
        lines.append(f"- Invites: {', '.join(added)}")
    removed = [_clean(e, line=True) for e in args.get("removedAttendeeEmails") or []]
    if removed:
        lines.append(f"- Removes: {', '.join(removed)}")
    lines += [f"- {line}" for line in _event_lines(args) if line]
    lines.append(_notices(args))
    return args, "\n".join(lines)


async def _delete_card(args: dict[str, Any], call: CallFn) -> tuple[dict[str, Any], str | None]:
    event = await _read(call, "get_event", _event_args(args), "the event to delete")
    detail = f"Deletes {_describe_event(event)} from your calendar. {_notices(args)}"
    return args, detail


_ANSWERS = {"accepted": "Yes", "declined": "No", "tentative": "Maybe"}


async def _answer_card(args: dict[str, Any], call: CallFn) -> tuple[dict[str, Any], str | None]:
    event = await _read(call, "get_event", _event_args(args), "the event to answer")
    organizer = _part(event, "organizer")
    answer = _ANSWERS.get(
        str(args.get("responseStatus")), _clean(args["responseStatus"], line=True)
    )
    lines = [f"Answers {answer} to {_describe_event(event)}."]
    if organizer.get("email"):
        lines.append(f"Organizer: {_clean(organizer['email'], line=True)}")
    if args.get("responseComment"):
        lines.append(f"Your note: {_clean(args['responseComment'])}")
    lines.append(_notices(args, default="EXTERNAL_ONLY"))
    return args, "\n".join(lines)


def _create_summary(args: dict[str, Any]) -> str:
    guests = _guests(args, "attendees", "attendeeEmails")
    invite = f", inviting {', '.join(guests)}" if guests else ""
    title = _clean(args.get("summary", ""), line=True)
    return f"Calendar: add “{title}” on {_moment(args.get('startTime'))}{invite}"


def _answer_summary(args: dict[str, Any]) -> str:
    answer = _ANSWERS.get(str(args.get("responseStatus")), str(args.get("responseStatus")))
    return f"Calendar: answer {_clean(answer, line=True)} to an invitation"


def calendar_server(granted: frozenset[str]) -> McpServer:
    return McpServer(
        name="calendar",
        label="Google Calendar",
        allow=allowed(CALENDAR_NEEDS, granted),
        prepare={
            "create_event": _create_card,
            "update_event": _update_card,
            "delete_event": _delete_card,
            "respond_to_event": _answer_card,
        },
        summaries={
            "create_event": _create_summary,
            "update_event": lambda _a: "Calendar: change an event",
            "delete_event": lambda _a: "Calendar: delete an event",
            "respond_to_event": _answer_summary,
        },
        activities={
            "list_calendars": lambda _a: "Listing your calendars",
            "list_events": lambda _a: "Looking at your calendar",
            "search_events": lambda a: (
                f"Searching your calendar for “{_clean(a.get('query', ''), line=True)}”"
            ),
            "get_event": lambda _a: "Reading a calendar event",
            "suggest_time": lambda _a: "Looking for a free time",
        },
        drop=CALENDAR_DROP,
        outbound=CALENDAR_OUTBOUND,
        strict=True,
        login=LOGIN,
        rate_limit_phrases=RATE_LIMIT_PHRASES,
    )


# ---------- connecting ----------


class _Bearer(httpx2.Auth):
    """Adds a valid access token to every request (renewed by the sign-in when due)."""

    def __init__(self, token: Callable[[], Any]) -> None:
        self._token = token

    async def async_auth_flow(self, request: httpx2.Request) -> Any:  # noqa: ANN401 - httpx flow
        request.headers["Authorization"] = f"Bearer {await self._token()}"
        yield request


class _StatusParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GoogleStatus(BaseModel):
    works: bool
    detail: str


class GoogleConnectors:
    """Gmail and Calendar for the signed-in Google account: their tools, and a live check."""

    def __init__(
        self,
        sign_in: Callable[[], GoogleSignIn | None],
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        urls: tuple[str, str] = (GMAIL_MCP_URL, CALENDAR_MCP_URL),
    ) -> None:
        """``transport`` and ``urls`` are for tests: a fake Google over HTTP."""
        self._sign_in = sign_in
        self._transport = transport
        self._urls = urls
        self._gmail: McpConnector | None = None
        self._calendar: McpConnector | None = None

    def _build(self, sign_in: GoogleSignIn) -> tuple[McpConnector, McpConnector]:
        granted = sign_in.granted()
        credential = sign_in.credential()
        me = credential.email if credential else None
        info = mcp_types.Implementation(name="enzo", version=__version__)

        async def token() -> str:
            return await sign_in.access_token()

        async def ready() -> None:
            try:
                await sign_in.access_token()
            except AuthError as exc:
                raise ConnectorError(str(exc)) from exc
            except BrainError as exc:
                msg = f"could not reach Google to renew the sign-in ({exc.kind}); nothing was done"
                raise ConnectorError(msg) from exc

        def factory(url: str) -> Callable[[], Client]:
            def connect() -> Client:
                streams = http_transport(url, {}, auth=_Bearer(token), transport=self._transport)
                # Google's servers answer server/discover with "Invalid params" and speak the
                # initialize handshake (checked 2026-11-25): skip the probe, one round trip less.
                return Client(streams, client_info=info, mode="legacy")

            return connect

        gmail_url, calendar_url = self._urls
        gmail = McpConnector(gmail_server(me, granted), factory(gmail_url), ready=ready)
        calendar = McpConnector(calendar_server(granted), factory(calendar_url), ready=ready)
        return gmail, calendar

    async def tools(self) -> list[Tool[Any, Any]]:
        """The tools the granted access allows; none when Google is not connected. A service that
        cannot be listed is left out and logged; the other still works."""
        sign_in = self._sign_in()
        if sign_in is None or sign_in.credential() is None:
            self._gmail = self._calendar = None
            return []
        self._gmail, self._calendar = self._build(sign_in)
        out: list[Tool[Any, Any]] = []
        for connector in (self._gmail, self._calendar):
            if not connector.server.allow:
                continue
            try:
                out.extend(await connector.tools())
            except Exception as exc:  # unreachable: keep the rest of Enzo working
                log.warning(
                    "connector unavailable",
                    extra={"connector": connector.server.name, "error": str(exc)},
                )
        return [*out, self._status_tool()]

    async def check(self) -> str:
        """§8.2 status check: the sign-in renews at Google, then each service answers one small
        read. Raises CheckFailedError saying what failed; never claims what was not checked."""
        sign_in = self._sign_in()
        if sign_in is None or sign_in.credential() is None:
            msg = f"Google is not connected: {LOGIN}"
            raise CheckFailedError(msg)
        try:
            await sign_in.access_token(renew=True)
        except AuthError as exc:
            raise CheckFailedError(str(exc)) from exc
        except BrainError as exc:
            msg = f"Could not reach Google to check the sign-in ({exc.kind}). Nothing was checked."
            raise CheckFailedError(msg) from exc
        if self._gmail is None or self._calendar is None:
            self._gmail, self._calendar = self._build(sign_in)
        granted = sign_in.granted()
        probes = (
            (self._gmail, ("search_threads", {"pageSize": 1}) if GMAIL_READ in granted else None),
            (self._calendar, _calendar_probe(granted)),
        )
        said, failed = [], False
        for connector, probe in probes:
            label = connector.server.label
            if probe is None:
                continue
            try:
                await connector.call(*probe)
            except ConnectorError as exc:
                failed = True
                said.append(f"{label} did not work: {exc}")
            else:
                said.append(f"{label} works")
        if not said:
            msg = "Enzo has no read access to check with, so nothing was checked."
            raise CheckFailedError(msg)
        text = f"{'. '.join(said)}. Allowed: {describe_access(granted)}."
        if failed:
            raise CheckFailedError(text)
        return text

    def _status_tool(self) -> Tool[Any, Any]:
        @tool(
            name="google_status",
            description=(
                "Gmail and Google Calendar: check right now that the connection works. Use it "
                "before promising future work that needs email or calendar (a schedule or a "
                "watch). Read-only."
            ),
            activity=lambda _p: "Checking your Google connection",
        )
        async def status(_params: _StatusParams) -> GoogleStatus:
            try:
                return GoogleStatus(works=True, detail=await self.check())
            except CheckFailedError as exc:
                return GoogleStatus(works=False, detail=str(exc))

        return status


def _calendar_probe(granted: frozenset[str]) -> tuple[str, dict[str, Any]] | None:
    if CALENDAR_LIST in granted:
        return "list_calendars", {"pageSize": 1}
    if granted & {CALENDAR_READ, CALENDAR_WRITE}:
        return "list_events", {"pageSize": 1}
    return None
