"""Memory.md edited by hand (spec §11.2, §11.3, §6.2): the file and the store are one truth.

A changed, added or removed line is read back before the next message, as a correction the user
made (with its source and date). A correction replaces the old fact, a secret is never kept, and an
edit Enzo cannot read never loses anything.
"""

import io
import json
import os
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from enzo import logs
from enzo.brain import Message
from enzo.brain.fake import ScriptedBrain
from enzo.memory import Fact, Memory
from enzo.store.repository import Store
from tests.gateway.test_api import client, new_conversation
from tests.memory.test_memory import memory_part, service
from tests.test_logs import reset


@pytest.fixture
def memory(store: Store, tmp_path: Path) -> Memory:
    return Memory(store.engine, tmp_path / "Enzo")


def edit(memory: Memory, old: str, new: str) -> None:
    """Change Memory.md the way a person does in an editor: in place, saved a moment later."""
    assert memory.file is not None
    body = memory.file.read_text()
    assert body.count(old) == 1, body
    memory.file.write_text(body.replace(old, new))
    later = datetime.now(UTC).timestamp() + 1  # a save is always after Enzo's own write
    os.utime(memory.file, (later, later))


def texts(facts: Sequence[Fact]) -> list[str]:
    return [f.text for f in facts]


async def test_a_hand_edit_is_what_the_next_message_carries(store: Store, memory: Memory) -> None:
    """Regression (spec §11.3): "Edit a memory file by hand and confirm Enzo uses the edited
    value on the next question." Memory.md was written from the database only, so the brain
    still got the old value and the next change overwrote the user's edit."""
    await memory.remember("Lives in San Jose.", kind="about")
    edit(memory, "Lives in San Jose.", "Lives in Austin.")
    brain = ScriptedBrain([Message.assistant("In Austin.")])
    async with client(service(store, memory, brain)) as c:
        chat = await new_conversation(c)
        await c.post(f"/conversations/{chat}/turns", json={"message": "Where do I live?"})
    sent = memory_part(brain.calls[0].messages[-1])
    assert "Lives in Austin." in sent
    assert "San Jose" not in sent  # the old value is replaced, never kept alongside (§6.2)
    assert "edited it by hand in Memory.md" in sent  # who said it, and where
    (current,) = await memory.all()
    assert current.text == "Lives in Austin."
    assert current.said_by == "user"
    assert current.edited_by_hand
    assert current.learned_at.date() == datetime.now(UTC).date()


async def test_changed_added_and_removed_lines_become_the_users_corrections(
    memory: Memory,
) -> None:
    seats = await memory.remember("Prefers window seats.", kind="preference")
    await memory.remember("Sister, lives in Austin.", kind="person", about="Priya")
    await memory.remember("Has a dog named Rex.", kind="fact", said_by="inferred")
    edit(memory, "Prefers window seats.", "Prefers aisle seats.")
    edit(memory, "- Has a dog named Rex.", "- Drinks tea, not coffee.\n- Has a dog named Rex.")
    assert memory.file is not None
    body = memory.file.read_text()
    lines = [x for x in body.splitlines() if "Priya" not in x]
    memory.file.write_text("\n".join(lines) + "\n")
    edits = await memory.read_edits()
    assert edits is not None
    assert (edits.changed, edits.added, edits.removed, edits.notes) == (1, 1, 1, [])
    current = {f.text: f for f in await memory.all()}
    assert set(current) == {
        "Prefers aisle seats.",
        "Drinks tea, not coffee.",
        "Has a dog named Rex.",
    }
    aisle = current["Prefers aisle seats."]
    assert (aisle.kind, aisle.said_by, aisle.edited_by_hand) == ("preference", "user", True)
    assert current["Drinks tea, not coffee."].kind == "fact"  # the heading it was added under
    assert current["Has a dog named Rex."].said_by == "inferred"  # an untouched line stays as is
    assert await memory.search("window") == []  # replaced, so never recalled again
    assert await memory.search("Priya") == []  # removed by hand: forgotten
    async with memory._sessions() as session:  # the correction names what it replaced
        from enzo.store.schema import MemoryRow  # noqa: PLC0415

        row = await session.get(MemoryRow, aisle.id)
        assert row is not None
        assert (row.replaces, row.turn_id) == (seats.id, "Memory.md")
    text = memory.file.read_text()
    assert "Prefers aisle seats. _(edited by hand in Memory.md, " in text
    assert await memory.read_edits() is None  # read once: nothing new the next time


async def test_reading_back_is_cheap_when_nothing_changed(
    memory: Memory, monkeypatch: pytest.MonkeyPatch
) -> None:
    await memory.remember("Prefers window seats.", kind="preference")
    assert await memory.read_edits() is None
    reads = []
    original = Path.read_text

    def counting(self: Path, *args: object, **kwargs: object) -> str:
        reads.append(self)
        return original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", counting)
    assert await memory.read_edits() is None
    assert reads == []  # unchanged since Enzo wrote it: not even opened


async def test_a_line_that_looks_like_a_secret_is_not_saved_and_the_user_is_told(
    memory: Memory,
) -> None:
    await memory.remember("Banks with a credit union.", kind="fact")
    edit(memory, "Banks with a credit union.", "Bank PIN: 4321")
    edit(memory, "## Other facts\n", "## Other facts\n\n- My wifi password is hunter2\n")
    edits = await memory.read_edits()
    assert edits is not None
    assert edits.changed == edits.added == 0
    assert len(edits.notes) == 2
    assert all("looks like a password or code" in n for n in edits.notes)
    assert all("4321" not in n and "hunter2" not in n for n in edits.notes)
    assert texts(await memory.all()) == ["Banks with a credit union."]  # the old one stays
    assert memory.file is not None
    text = memory.file.read_text()
    assert "4321" not in text
    assert "hunter2" not in text
    assert "## Not saved" in text
    assert "a line you changed under Other facts was not saved" in text
    assert "Banks with a credit union. _(you said, " in text


async def test_a_malformed_edit_never_loses_data(memory: Memory) -> None:
    await memory.remember("Prefers window seats.", kind="preference")
    await memory.remember("Allergic to peanuts.", kind="about")
    assert memory.file is not None
    path = memory.file
    # The top of the file (with the line that says which copy this is) is gone, a memory's
    # line went with a careless cut, and some words are plain text instead of a "- " line.
    body = path.read_text().split("## About you", 1)[1]
    body = "\n".join(x for x in body.splitlines() if "window" not in x)
    path.write_text("## About you" + body + "\nMy favourite colour is green\n")
    edits = await memory.read_edits()
    assert edits is not None
    assert (edits.changed, edits.added, edits.removed) == (0, 0, 0)
    # Nothing is removed when Enzo cannot tell which copy was edited, and it says so.
    assert any("nothing was removed" in n for n in edits.notes)
    assert any("kept under “Not saved”" in n for n in edits.notes)  # and where the words went
    assert sorted(texts(await memory.all())) == ["Allergic to peanuts.", "Prefers window seats."]
    text = path.read_text()
    assert "My favourite colour is green" in text.split("## Not saved", 1)[1]  # kept, for them
    assert text.startswith("# What Enzo remembers\n")
    # Kept on the next write too, until the user moves it or deletes it.
    await memory.remember("Lives in Austin.", kind="about")
    assert "My favourite colour is green" in path.read_text()
    edit(memory, "My favourite colour is green", "- Favourite colour is green.")
    assert await memory.read_edits() is not None
    assert "Favourite colour is green." in texts(await memory.all())
    assert "My favourite colour" not in path.read_text()


async def test_an_emptied_or_deleted_file_is_written_again_and_nothing_is_forgotten(
    memory: Memory,
) -> None:
    await memory.remember("Prefers window seats.", kind="preference")
    assert memory.file is not None
    memory.file.write_text("")
    edits = await memory.read_edits()
    assert edits is not None
    assert edits.removed == 0
    assert texts(await memory.all()) == ["Prefers window seats."]
    assert "Prefers window seats." in memory.file.read_text()
    memory.file.unlink()
    edits = await memory.read_edits()
    assert edits is not None
    assert edits.removed == 0
    assert "Prefers window seats." in memory.file.read_text()


async def test_when_both_changed_a_stale_copy_never_undoes_enzo_and_the_newer_edit_wins(
    memory: Memory,
) -> None:
    """The user's editor kept an older copy open while Enzo learned things in chat."""
    home = await memory.remember("Lives in San Jose.", kind="about")
    seats = await memory.remember("Prefers window seats.", kind="preference")
    dog = await memory.remember("Has a dog.", kind="fact")
    assert memory.file is not None
    stale = memory.file.read_text()  # what the editor holds
    # Meanwhile, in chat: a new fact, a correction, and something forgotten.
    await memory.remember("Works at Acme.", kind="about")
    moved = await memory.remember("Lives in Denver.", kind="about", replaces=home.id)
    await memory.forget(dog.id)
    # The editor saves its old copy, with one line changed by the user.
    memory.file.write_text(stale.replace("Prefers window seats.", "Prefers aisle seats."))
    later = datetime.now(UTC).timestamp() + 1
    os.utime(memory.file, (later, later))
    edits = await memory.read_edits()
    assert edits is not None
    assert (edits.changed, edits.added, edits.removed) == (1, 0, 0)
    current = set(texts(await memory.all()))
    # Untouched stale lines change nothing: the move stands, the forgotten fact stays gone,
    # and what Enzo learned after that copy was written is not "removed".
    assert current == {"Lives in Denver.", "Works at Acme.", "Prefers aisle seats."}
    assert any("asked Enzo to forget" in n for n in edits.notes)
    assert seats.id not in [f.id for f in await memory.all()]
    assert moved.id in [f.id for f in await memory.all()]


async def test_when_both_changed_one_memory_the_newer_change_wins(memory: Memory) -> None:
    home = await memory.remember("Lives in San Jose.", kind="about")
    assert memory.file is not None
    stale = memory.file.read_text()
    await memory.remember("Lives in Denver.", kind="about", replaces=home.id)
    # A hand edit of the same fact, saved before the chat correction: the chat's is newer.
    memory.file.write_text(stale.replace("Lives in San Jose.", "Lives in Boston."))
    earlier = (datetime.now(UTC) - timedelta(hours=1)).timestamp()
    os.utime(memory.file, (earlier, earlier))
    edits = await memory.read_edits()
    assert edits is not None
    assert edits.changed == 0
    assert texts(await memory.all()) == ["Lives in Denver."]
    assert any("newer" in n for n in edits.notes)
    # Saved after it: the hand edit is newer and wins.
    stale = memory.file.read_text()
    memory.file.write_text(stale.replace("Lives in Denver.", "Lives in Boston."))
    later = datetime.now(UTC).timestamp() + 5
    os.utime(memory.file, (later, later))
    edits = await memory.read_edits()
    assert edits is not None
    assert edits.changed == 1
    assert texts(await memory.all()) == ["Lives in Boston."]


async def test_enzo_reads_a_hand_edit_before_it_writes_the_file_again(memory: Memory) -> None:
    """The old bug's other half: the next change Enzo made rewrote the file over the edit."""
    await memory.remember("Prefers window seats.", kind="preference")
    edit(memory, "Prefers window seats.", "Prefers aisle seats.")
    await memory.remember("Lives in Austin.", kind="about")  # no message came in between
    assert set(texts(await memory.all())) == {"Prefers aisle seats.", "Lives in Austin."}
    assert memory.file is not None
    assert "Prefers aisle seats." in memory.file.read_text()
    # Forgetting rewrites the file too: an edit made just before it counts first.
    edit(memory, "Prefers aisle seats.", "Prefers aisle seats, near the front.")
    (austin,) = [f for f in await memory._facts() if f.text == "Lives in Austin."]
    await memory.forget(austin.id)
    assert texts(await memory.all()) == ["Prefers aisle seats, near the front."]


async def test_a_line_that_lost_its_number_is_still_the_same_memory(memory: Memory) -> None:
    await memory.remember("Prefers window seats.", kind="preference")
    assert memory.file is not None
    body = memory.file.read_text()
    start = body.index(" _(you said")
    end = body.index(")_", start) + 2
    memory.file.write_text(body[:start] + body[end:])  # the provenance and number deleted
    later = datetime.now(UTC).timestamp() + 1
    os.utime(memory.file, (later, later))
    await memory.read_edits()
    (fact,) = await memory.all()
    assert (fact.id, fact.text) == (1, "Prefers window seats.")  # not saved twice, not removed


async def test_edits_reach_the_log_as_counts_never_words(memory: Memory) -> None:
    await memory.remember("Secretly planning a trip.", kind="fact")
    edit(memory, "Secretly planning a trip.", "Secretly planning a surprise.")
    out = io.StringIO()
    logs.configure(stream=out)
    try:
        await memory.read_edits()
    finally:
        reset()
    (line,) = [json.loads(x) for x in out.getvalue().splitlines() if "memory.file_edits" in x]
    assert (line["changed"], line["added"], line["removed"]) == (1, 0, 0)
    assert "surprise" not in out.getvalue().lower()


async def test_the_brain_is_told_what_was_not_saved(store: Store, memory: Memory) -> None:
    await memory.remember("Banks with a credit union.", kind="fact")
    edit(memory, "Banks with a credit union.", "Bank PIN: 4321")
    brain = ScriptedBrain([Message.assistant("ok")])
    async with client(service(store, memory, brain)) as c:
        chat = await new_conversation(c)
        await c.post(f"/conversations/{chat}/turns", json={"message": "hello"})
    texts_sent = [p.text for p in brain.calls[0].messages[-1].parts if hasattr(p, "text")]
    (note,) = [t for t in texts_sent if t.startswith("Memory.md was edited by hand")]
    assert "a line you changed under Other facts was not saved" in note  # what, and why:
    assert "it looks like a password or code" in note
    assert "tell the user" in note
    assert "4321" not in note


async def test_how_lines_are_read(memory: Memory) -> None:
    """A wrapped line is one memory; a line left with no words keeps its memory; a numbered line
    copied twice and changed is a new memory beside the old one; a layout change is no edit."""
    seats = await memory.remember("Prefers window seats.", kind="preference")
    tea = await memory.remember("Drinks tea.", kind="preference")
    assert memory.file is not None
    body = memory.file.read_text()
    window = next(x for x in body.splitlines() if "window" in x)
    tea_line = next(x for x in body.splitlines() if "Drinks tea." in x)
    copy = window.replace("Prefers window seats.", "Prefers aisle seats\n  on long flights.")
    body = body.replace(window, f"{window}\n{copy}")
    memory.file.write_text(body.replace(tea_line, tea_line.replace("Drinks tea.", "")))
    edits = await memory.read_edits()
    assert edits is not None
    assert (edits.changed, edits.added, edits.removed) == (0, 1, 0)
    assert any("#2 had no words left" in n for n in edits.notes)
    current = {f.id: f.text for f in await memory.all()}
    assert current[seats.id] == "Prefers window seats."
    assert current[tea.id] == "Drinks tea."  # its line was emptied, not deleted: kept
    assert "Prefers aisle seats on long flights." in current.values()
    # Moving lines around and adding blank lines changes nothing, and is not read again.
    body = memory.file.read_text().replace("\n\n## ", "\n\n\n## ")
    memory.file.write_text(body)
    assert await memory.read_edits() is None
    assert memory.file.read_text() == body  # the user's layout stays


async def test_a_file_that_is_not_text_is_set_aside_not_lost(memory: Memory) -> None:
    await memory.remember("Prefers window seats.", kind="preference")
    assert memory.file is not None
    memory.file.write_bytes("- Prefers aisle seats \xe9t\xe9\n".encode("latin-1"))
    edits = await memory.read_edits()
    assert edits is not None
    (note,) = edits.notes
    assert "could not read the changes" in note
    (aside,) = memory.file.parent.glob("Memory (unreadable copy *).md")
    assert aside.name in note
    assert aside.read_bytes().startswith(b"- Prefers aisle seats")  # the user's copy, unchanged
    assert texts(await memory.all()) == ["Prefers window seats."]
    assert "Prefers window seats." in memory.file.read_text()


async def test_a_failed_read_never_lets_enzo_write_over_the_edit(
    memory: Memory, monkeypatch: pytest.MonkeyPatch
) -> None:
    await memory.remember("Prefers window seats.", kind="preference")
    edit(memory, "Prefers window seats.", "Prefers aisle seats.")
    assert memory.file is not None
    edited = memory.file.read_text()

    def broken(_body: str) -> object:
        msg = "a bug in reading the file"
        raise RuntimeError(msg)

    monkeypatch.setattr("enzo.memory.store.parse", broken)
    await memory.remember("Lives in Austin.", kind="about")  # still saved in memory...
    assert memory.file.read_text() == edited  # ...but the user's edit is not written over
    monkeypatch.undo()
    await memory.read_edits()  # once reading works again, the edit counts
    assert set(texts(await memory.all())) == {"Prefers aisle seats.", "Lives in Austin."}
    assert "Lives in Austin." in memory.file.read_text()


async def test_unread_lines_are_kept_but_never_a_secret_and_notes_stay_until_deleted(
    memory: Memory,
) -> None:
    await memory.remember("Prefers window seats.", kind="preference")
    edit(memory, "## Preferences\n", "## Preferences\n\nrecovery code: 1234-5678\nCall Priya\n")
    edits = await memory.read_edits()
    assert edits is not None
    assert any("taken out of the file" in n for n in edits.notes)
    assert memory.file is not None
    text = memory.file.read_text()
    assert "1234-5678" not in text
    kept = text.split("## Not saved", 1)[1]
    assert "Call Priya" in kept
    assert "> " in kept  # the note, for the user to read
    await memory.remember("Lives in Austin.", kind="about")
    assert "taken out of the file" in memory.file.read_text()  # still there after a rewrite
    # Read again after another edit, a note stays; a secret typed into a note does not.
    edit(memory, "Lives in Austin.", "Lives in Denver.")
    edit(memory, "Call Priya", "Call Priya\n\n> my PIN is 4321")
    await memory.read_edits()
    text = memory.file.read_text()
    assert "taken out of the file" in text
    assert "4321" not in text
    lines = memory.file.read_text().splitlines()
    memory.file.write_text("\n".join(x for x in lines if "taken out" not in x) + "\n")
    await memory.read_edits()
    await memory.remember("Has a dog.", kind="fact")
    assert "taken out of the file" not in memory.file.read_text()  # the user deleted it
    assert "Call Priya" in memory.file.read_text()
