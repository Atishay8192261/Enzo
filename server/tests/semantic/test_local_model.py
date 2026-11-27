"""The real local model on spec §6.3's two checks, end to end through memory, the index and the
database (ADR 0030).

CI does not download the model: set ``ENZO_TEST_MODEL_DIR`` to a folder holding
``all-MiniLM-L6-v2/model.onnx`` and ``tokenizer.json`` (verified against their pins before use).
Run on 2026-11-26 in the cloud container with the model from Chroma's bucket.
"""

import json
import os
import uuid
from pathlib import Path

import httpx2
import pytest

from enzo.brain.models import Message
from enzo.memory import Memory
from enzo.semantic import MINILM, LocalEmbedder, SemanticIndex
from enzo.store.repository import Store

MODELS = os.environ.get("ENZO_TEST_MODEL_DIR")
pytestmark = pytest.mark.skipif(
    not MODELS, reason="needs the local model: set ENZO_TEST_MODEL_DIR (not downloaded in CI)"
)
CASES = json.loads((Path(__file__).parent / "meaning.json").read_text(encoding="utf-8"))


@pytest.fixture
async def model() -> LocalEmbedder:
    # Never a download here: the folder must already hold the pinned files.
    offline = httpx2.Client(transport=httpx2.MockTransport(lambda _r: httpx2.Response(599)))
    embedder = LocalEmbedder(Path(MODELS or "."), http=offline)
    assert await embedder.prepare(), embedder.problem
    return embedder


async def memory_with(store: Store, model: LocalEmbedder, tmp_path: Path) -> Memory:
    memory = Memory(store.engine, tmp_path / "Enzo", index=SemanticIndex(store.engine, model))
    for note in CASES["notes"]:
        about, _, words = note.partition(": ") if note.startswith("Ana: ") else ("", "", note)
        await memory.remember(words, kind="fact", about=about or None)
    return memory


async def test_vectors_are_unit_length_and_384_wide(model: LocalEmbedder) -> None:
    (one,) = await model.embed(["Prefers aisle seats on flights."], use="document")
    assert len(one) == MINILM.dims
    assert abs(sum(x * x for x in one) - 1) < 1e-5


async def test_a_differently_phrased_question_retrieves_the_right_note(
    store: Store, model: LocalEmbedder, tmp_path: Path
) -> None:
    """§6.3 verify, first check, on 26 rephrased questions with few or no words in common."""
    memory = await memory_with(store, model, tmp_path)
    ids = {f.text if not f.about else f"{f.about}: {f.text}": f.id for f in await memory.all()}
    order = [ids[n] for n in CASES["notes"]]
    right, recalled = 0, 0
    for case in (c for c in CASES["queries"] if "want" in c):
        found = await memory.search(case["q"], 3)
        right += bool(found) and found[0].id == order[case["want"]]
        recalled += any(f.id == order[case["want"]] for f in await memory.recall(case["q"]))
    # Measured 2026-11-27: 24 of 26 first (search), 23 of 26 recalled with the message. Misses:
    # "plan a dinner menu" (vegetarian, 0.11), "how should you format answers" (0.298, just
    # under the floor) and "the glass-floor skywalk tour" (afraid of heights, 0.235: the
    # theatre note, 0.309, is recalled instead).
    assert right >= 24
    assert recalled >= 23
    # The two §6.3 examples kept in the ADR, exactly:
    (shellfish,) = await memory.recall("Can I have shrimp scampi tonight?")
    assert shellfish.text == "Allergic to shellfish."


async def test_a_similar_but_different_topic_is_not_confused(
    store: Store, model: LocalEmbedder, tmp_path: Path
) -> None:
    """§6.3 verify, second check: each confusable pair ranks the right note above the other."""
    memory = await memory_with(store, model, tmp_path)
    ids = {f.text if not f.about else f"{f.about}: {f.text}": f.id for f in await memory.all()}
    order = [ids[n] for n in CASES["notes"]]
    pairs = [c for c in CASES["queries"] if "not" in c]
    left_out = 0
    for case in pairs:
        found = [f.id for f in await memory.search(case["q"], 10)]
        want, other = order[case["want"]], order[case["not"]]
        assert want in found, case["q"]
        assert other not in found or found.index(want) < found.index(other), case["q"]
        recalled = [f.id for f in await memory.recall(case["q"])]
        assert recalled[0] == want, case["q"]  # recalled unasked: the right note comes first
        left_out += other not in recalled
    # Measured 2026-11-27: the weaker neighbour is left out of recall for 8 of 9 pairs; "What
    # can't my sister eat?" recalls her peanut note first and the user's shellfish note second,
    # each with its own words, so the brain can tell them apart (§6.3 step 5).
    assert left_out >= len(pairs) - 1
    plane = await memory.recall("Book me a plane ticket, any seating preference?")
    assert [f.text for f in plane] == ["Prefers aisle seats on flights."]


async def test_unrelated_questions_recall_nothing(
    store: Store, model: LocalEmbedder, tmp_path: Path
) -> None:
    memory = await memory_with(store, model, tmp_path)
    unrelated = [c["q"] for c in CASES["queries"] if "want" not in c]
    recalled = {q: await memory.recall(q) for q in unrelated}
    quiet = [q for q, found in recalled.items() if not found]
    # Measured 2026-11-27: 9 of 10. "Translate 'good morning' into Japanese" recalls "Is learning
    # Spanish" (0.34): languages, arguably related.
    assert len(quiet) >= 9, {q: [f.text for f in f_] for q, f_ in recalled.items() if f_}


async def test_past_chats_are_found_by_meaning(
    store: Store, model: LocalEmbedder, tmp_path: Path
) -> None:
    index = SemanticIndex(store.engine, model)
    chats: list[uuid.UUID] = []
    for asked, said in CASES["turns"]:
        chat = await store.create_conversation("main", asked[:40])
        await store.save_turn(chat, messages=[Message.user(asked), Message.assistant(said)])
        chats.append(chat)
    await index.sync()
    for case in CASES["turn_queries"]:
        hits = await index.find(
            case["q"], sources={"turn"}, limit=2, mode="recall", by_meaning_only=True
        )
        if "want" in case:
            assert [h.conversation_id for h in hits] == [chats[case["want"]]], case["q"]
        else:
            assert hits == [], case["q"]
