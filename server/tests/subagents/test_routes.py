"""``/helpers`` and a chat's helper notices over HTTP (ADR 0028)."""

import httpx2
from pydantic import SecretStr

from enzo.brain import Message
from enzo.config import Settings
from enzo.gateway.app import create_app
from enzo.store.repository import Store
from tests.subagents.fakes import Rig, call, calls, delegate, drain, rig

WORK = "Read the pages"


def client(r: Rig) -> httpx2.AsyncClient:
    settings = Settings(env="test", api_token=SecretStr("t"))
    app = create_app(settings, service=r.service, helpers=r.helpers)
    return httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        base_url="http://enzo",
        headers={"Authorization": "Bearer t"},
    )


async def test_helpers_are_listed_stopped_and_shown_in_their_chats(store: Store) -> None:
    r = rig(
        store,
        {
            WORK: [calls(call("step", "s1", value="page 1")), Message.assistant("Report.")],
            "compare": [
                delegate("d1", "Read pages", f"{WORK} one by one."),
                Message.assistant("On it."),
            ],
        },
    )
    second = r.brain.hold(WORK, 2)
    chat = await store.create_conversation()
    await drain(await r.service.send(chat, "Read and compare"))
    await r.until(lambda: r.brain.asked[WORK] == 2)
    async with client(r) as c:
        (h,) = (await c.get("/helpers")).json()["helpers"]
        assert (h["title"], h["status"], h["depth"], h["steps"], h["calls"]) == (
            "Read pages",
            "working",
            1,
            1,
            1,
        )
        assert h["conversation_id"] == h["parent_id"] == str(chat)
        # Its chat shows its brief for what it is: not something the user typed.
        shown = (await c.get(f"/conversations/{h['chat_id']}/messages")).json()
        assert shown[0]["origin"] == "brief"
        assert (await c.post("/helpers/999/stop")).status_code == 404
        stopped = (await c.post(f"/helpers/{h['id']}/stop")).json()
        assert (stopped["stopped"], stopped["helper"]["status"]) == (True, "stopped")
        again = (await c.post(f"/helpers/{h['id']}/stop")).json()
        assert again["stopped"] is False
        notices = (await c.get(f"/conversations/{chat}/helpers")).json()
        assert [(n["kind"], n["text"], n["chat_id"]) for n in notices] == [
            ("stopped", f"Stopped helper #{h['id']} “Read pages”.", h["chat_id"])
        ]
        assert notices[0]["after"] == len(await store.history(chat))
        assert (await c.get("/helpers", headers={"Authorization": "Bearer x"})).status_code == 401
    second.set()
