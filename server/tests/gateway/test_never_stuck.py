"""Over HTTP with real Postgres: a chat never gets stuck (#39, #46)."""

import asyncio
import uuid

from enzo.agent.history import MOVED_ON
from enzo.agent.service import ConversationService, TurnMeta
from enzo.brain import Message, ToolCall, ToolResult
from enzo.store.repository import Store
from tests.agent import tools as t
from tests.agent.strict import Stalls, StrictBrain
from tests.gateway.test_api import client, events, new_conversation, service

EMAIL = ToolCall(id="c1", name="send_email", arguments_json='{"value": "hi bob"}')


async def test_a_reopened_chat_shows_the_open_approval_card(store: Store) -> None:
    brain = StrictBrain([Message.assistant(tool_calls=(EMAIL,))])
    async with client(service(store, {"m-main": brain})) as c:
        cid = await new_conversation(c)
        await c.post(f"/conversations/{cid}/turns", json={"message": "email bob"})
        # The app closed here. Reopened, it reads the conversation:
        conversation = (await c.get(f"/conversations/{cid}")).json()
        (card,) = conversation["pending_approvals"]
        assert (card["tool"], card["arguments"]) == ("send_email", {"value": "hi bob"})
        assert set(card) == {"id", "tool", "arguments", "summary", "detail", "privacy"}
        assert card["privacy"] == []  # nothing private in it
        assert conversation["running"] is False
        assert t.ran == []


async def test_a_new_message_while_an_approval_is_open_keeps_the_chat_working(
    store: Store,
) -> None:
    """Regression (#39): a new message after an unanswered approval broke the chat for good."""
    brain = StrictBrain(
        [
            Message.assistant(tool_calls=(EMAIL,)),
            Message.assistant("ok, not sending it"),
            Message.assistant("still here"),
        ]
    )
    async with client(service(store, {"m-main": brain})) as c:
        cid = await new_conversation(c)
        got = events(
            (await c.post(f"/conversations/{cid}/turns", json={"message": "email bob"})).text
        )
        (approval,) = [d for k, d in got if k == "approval_needed"]

        moved_on = events(
            (await c.post(f"/conversations/{cid}/turns", json={"message": "never mind"})).text
        )
        assert moved_on[-1][1]["text"] == "ok, not sending it"
        again = events((await c.post(f"/conversations/{cid}/turns", json={"message": "hi"})).text)
        assert again[-1][1]["text"] == "still here"  # and every later message works too

        sent = brain.calls[1].messages
        assert sent[-2] == Message.tool(  # the open call was closed, not left dangling
            ToolResult(call_id="c1", name="send_email", content=MOVED_ON, is_error=True)
        )
        assert t.ran == []  # nothing ran unasked
        assert (await c.get(f"/conversations/{cid}")).json()["pending_approvals"] == []
        late = await c.post(
            f"/conversations/{cid}/approvals/{approval['id']}", json={"approved": True}
        )
        assert late.status_code == 409  # the card is gone: a late yes runs nothing
        roles = [m["role"] for m in (await c.get(f"/conversations/{cid}/messages")).json()]
        assert roles == ["user", "assistant", "tool", "user", "assistant", "user", "assistant"]


async def test_a_chat_broken_before_the_fix_works_again(store: Store) -> None:
    cid = await store.create_conversation("main", None)
    await store.save_turn(  # what #39 left behind: an open call, then more messages
        cid,
        messages=[
            Message.user("delete it"),
            Message.assistant(tool_calls=(EMAIL,)),
            Message.user("hello?"),
        ],
    )
    brain = StrictBrain([Message.assistant("back to normal")])
    async with client(service(store, {"m-main": brain})) as c:
        got = events((await c.post(f"/conversations/{cid}/turns", json={"message": "hi"})).text)
        assert got[-1][1]["text"] == "back to normal"


async def test_stop_ends_a_stuck_reply_and_frees_the_chat(store: Store) -> None:
    """Regression (#46): a reply whose connection died locked the chat (409) for ten minutes."""
    stuck = Stalls()
    svc = service(store, {"m-main": stuck})
    async with client(svc) as c:
        cid = await new_conversation(c)
        turn = asyncio.create_task(
            c.post(f"/conversations/{cid}/turns", json={"message": "a story"})
        )
        await asyncio.wait_for(stuck.started.wait(), 5)

        busy = await c.post(f"/conversations/{cid}/turns", json={"message": "are you back?"})
        assert busy.status_code == 409
        assert busy.json()["detail"] == (
            "Enzo is still finishing the last reply in this chat. Stop it, or wait."
        )
        assert (await c.get(f"/conversations/{cid}")).json()["running"] is True

        assert (await c.post(f"/conversations/{cid}/stop")).json() == {"stopping": True}
        done = events((await asyncio.wait_for(turn, 5)).text)[-1][1]
        assert (done["stop"], done["text"]) == ("cancelled", "Stopped at your request.")
        await svc.wait_idle(uuid.UUID(cid))
        saved = (await c.get(f"/conversations/{cid}/messages")).json()
        assert saved[-1]["parts"][0]["text"] == "Once upon a time"  # the words already shown stay
        assert (await c.post(f"/conversations/{cid}/stop")).json() == {"stopping": False}
        assert (await c.get(f"/conversations/{cid}")).json()["running"] is False


async def test_stop_also_works_when_the_client_lost_the_stream(store: Store) -> None:
    """The window that started the reply may be gone; any client can still stop it."""
    stuck = Stalls()
    svc: ConversationService = service(store, {"m-main": stuck})
    cid = await store.create_conversation("main", None)
    stream = await svc.send(cid, "a story", TurnMeta())
    await asyncio.wait_for(stuck.started.wait(), 5)
    del stream  # nobody reads the events any more
    async with client(svc) as c:
        assert (await c.post(f"/conversations/{cid}/stop")).json() == {"stopping": True}
    await asyncio.wait_for(svc.wait_idle(cid), 5)
    assert not svc.running(cid)
