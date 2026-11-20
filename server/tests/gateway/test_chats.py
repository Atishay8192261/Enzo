"""Past chats (#38): a list to reopen, named after their first message, renamed, or trashed."""

import asyncio
import uuid

import asyncpg
from alembic import command

from enzo.brain import Message
from enzo.brain.fake import ScriptedBrain
from enzo.store.repository import TITLE_CHARS, Store, title_from
from tests.gateway.test_api import client, new_conversation, service
from tests.pg import alembic_config, create_database, drop_database


async def chat(c, text: str) -> str:  # type: ignore[no-untyped-def]
    cid = await new_conversation(c)
    await c.post(f"/conversations/{cid}/turns", json={"message": text})
    return cid


def test_a_title_is_the_first_line_of_the_first_message() -> None:
    assert title_from("Plan my week\nwith gym on Monday") == "Plan my week"
    assert title_from("\n\n   \n  second line  ") == "second line"
    assert title_from("tabs\tand\x1b[31m escapes") == "tabs and [31m escapes"
    long = title_from("x" * 200)
    assert long is not None
    assert len(long) == TITLE_CHARS
    assert long.endswith("…")
    assert title_from("   ") is None


async def test_chats_are_listed_newest_first_with_titles(store: Store) -> None:
    brain = ScriptedBrain([Message.assistant("ok")] * 10)
    async with client(service(store, {"m-main": brain})) as c:
        old = await chat(c, "What is a mutex?\nexplain simply")
        await new_conversation(c)  # never used: an empty chat is not in the list
        new = await chat(c, "Plan my week")
        await c.post(f"/conversations/{old}/turns", json={"message": "and a semaphore?"})
        # The test database is shared: look only at this test's chats.
        listed = [x for x in (await c.get("/conversations")).json() if x["id"] in {old, new}]
        assert [(x["id"], x["title"], x["messages"]) for x in listed] == [
            (old, "What is a mutex?", 2),  # most recently active first
            (new, "Plan my week", 1),
        ]
        assert listed[0]["last_active"] >= listed[1]["last_active"]
        assert len((await c.get("/conversations?limit=1")).json()) == 1
        assert (await c.get("/conversations?limit=1")).json()[0]["id"] == old  # newest overall
        assert (await c.get("/conversations?limit=0")).status_code == 422


async def test_rename_sticks_and_trash_hides_without_destroying(store: Store) -> None:
    brain = ScriptedBrain([Message.assistant("ok")] * 10)
    async with client(service(store, {"m-main": brain})) as c:
        cid = await chat(c, "first words")
        r = await c.patch(f"/conversations/{cid}", json={"title": "  Weekly   plan "})
        assert r.status_code == 204
        await c.post(f"/conversations/{cid}/turns", json={"message": "more"})
        (listed,) = [x for x in (await c.get("/conversations")).json() if x["id"] == cid]
        assert listed["title"] == "Weekly plan"  # later messages never rename it back
        assert (await c.patch(f"/conversations/{cid}", json={"title": ""})).status_code == 422
        missing = uuid.uuid4()
        assert (await c.patch(f"/conversations/{missing}", json={"title": "x"})).status_code == 404
        assert (await c.delete(f"/conversations/{missing}")).status_code == 404

        assert (await c.delete(f"/conversations/{cid}")).status_code == 204
        assert cid not in {x["id"] for x in (await c.get("/conversations")).json()}
        reopened = await c.get(f"/conversations/{cid}")  # still there, for a window that has it
        assert reopened.status_code == 200
        assert reopened.json()["title"] == "Weekly plan"


async def test_a_chat_can_start_with_a_model_chosen(store: Store) -> None:
    async with client(service(store, {})) as c:
        r = await c.post("/conversations", json={"kind": "main", "brain": "fast"})
        assert r.status_code == 201
        assert r.json()["brain"] == "fast"
        stored = (await c.get(f"/conversations/{r.json()['id']}")).json()
        assert stored["brain"] == "fast"
        bad = await c.post("/conversations", json={"kind": "main", "brain": "ghost"})
        assert bad.status_code == 422


def test_chats_saved_before_titles_get_one_from_their_first_message() -> None:
    """The migration names older chats the way new ones are named."""
    name, url = create_database()
    raw = url.replace("postgresql+asyncpg://", "postgresql://")

    async def run(sql: str, *args: object) -> list[asyncpg.Record]:
        conn = await asyncpg.connect(raw)
        try:
            return await conn.fetch(sql, *args)
        finally:
            await conn.close()

    try:
        cfg = alembic_config(url)
        command.upgrade(cfg, "0003")
        cid = uuid.uuid4()
        asyncio.run(run("INSERT INTO conversations (id, kind) VALUES ($1, 'main')", cid))
        body = '{"role": "user", "parts": [{"type": "text", "text": "Old question\\nline two"}]}'
        asyncio.run(
            run(
                "INSERT INTO messages (conversation_id, seq, role, body) "
                "VALUES ($1, 0, 'user', $2::jsonb)",
                cid,
                body,
            )
        )
        command.upgrade(cfg, "head")
        (row,) = asyncio.run(run("SELECT title, archived_at FROM conversations"))
        assert (row["title"], row["archived_at"]) == ("Old question", None)
    finally:
        drop_database(name)
