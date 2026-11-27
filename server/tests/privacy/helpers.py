"""Helpers for privacy tests: a check that knows fixed facts, and a fake search service."""

import json
from collections.abc import Sequence

import httpx2
from pydantic import BaseModel, ConfigDict

from enzo.brain import Message, ToolCall
from enzo.privacy.check import Discretion, Verdict
from enzo.privacy.detect import Known, Person
from enzo.tools import ToolRegistry, tool
from enzo.tools.registry import Outgoing
from enzo.tools.web import TAVILY_SEARCH, web_tools

SARA = Person("Sara", "wife", memory=4)


class Knows(Discretion):
    """The real check, over fixed knowledge instead of the database. ``known`` may change
    between calls (a memory forgotten while a card waits); ``broken`` makes it fail."""

    def __init__(self, known: Known | None = None, *, broken: bool = False) -> None:
        super().__init__(rules=None)  # type: ignore[arg-type]  # never read: known() is fixed
        self.facts = known or Known()
        self.broken = broken
        self.checked: list[list[Outgoing]] = []

    async def known(self, history: Sequence[Message]) -> Known:
        if self.broken:
            msg = "the database went away"
            raise RuntimeError(msg)
        return self.facts

    async def check(self, outgoing: Sequence[Outgoing], history: Sequence[Message]) -> Verdict:
        self.checked.append(list(outgoing))
        return await super().check(outgoing, history)


class Tavily:
    """A fake search service: records every query that reached it."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    def client(self) -> httpx2.AsyncClient:
        def handle(request: httpx2.Request) -> httpx2.Response:
            assert str(request.url) == TAVILY_SEARCH
            query = json.loads(request.content)["query"]
            self.queries.append(query)
            hit = {"title": "A result", "url": "https://example.com/a", "content": "Some text."}
            return httpx2.Response(200, json={"query": query, "results": [hit]})

        return httpx2.AsyncClient(transport=httpx2.MockTransport(handle))


class Email(BaseModel):
    model_config = ConfigDict(extra="forbid")
    to: str
    body: str


class Sent(BaseModel):
    sent: bool


outbox: list[Email] = []


def _sending(a: Email) -> str:
    return f"Send an email to {a.to}"


def _sends(a: Email) -> list[Outgoing]:
    return [Outgoing(a.body, "message")]


@tool(
    name="send_email",
    description="Send an email.",
    requires_approval=True,
    summary=_sending,
    outbound=_sends,
)
async def send_email(args: Email) -> Sent:
    outbox.append(args)
    return Sent(sent=True)


def registry(tavily: Tavily) -> ToolRegistry:
    outbox.clear()
    return ToolRegistry([*web_tools(tavily_key=lambda: None, http=tavily.client()), send_email])


def search(query: str, cid: str = "s1") -> ToolCall:
    return ToolCall(id=cid, name="web_search", arguments_json=json.dumps({"query": query}))


def email(body: str, cid: str = "e1", to: str = "hello@trattoria.example") -> ToolCall:
    return ToolCall(id=cid, name="send_email", arguments_json=json.dumps({"to": to, "body": body}))


def reply(*calls: ToolCall) -> Message:
    return Message.assistant(tool_calls=calls)
