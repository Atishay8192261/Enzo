"""The summary record and its invariants (spec §5.2, §14 "Lost context")."""

from pathlib import Path

import pytest

from enzo.brain.models import Message, TextPart, ToolCall, ToolResult
from enzo.compaction.summary import (
    Correction,
    Folded,
    Note,
    NotesParams,
    StoredRef,
    Summary,
    SummaryInvariantError,
    check,
    fold,
    looks_like_correction,
    merge,
    render,
    unfence,
)
from enzo.tools.fence import fence
from enzo.tools.stored import OutputStore


def user(words: str) -> Message:
    return Message(role="user", parts=(TextPart(text=words), TextPart(text="[sent 2026-11-25]")))


def turns(*words: str, start: int = 0) -> dict[int, list[Message]]:
    return {start + i: [user(w), Message.assistant(f"ok {i}")] for i, w in enumerate(words)}


def store(tmp_path: Path) -> OutputStore:
    return OutputStore(
        tmp_path, tool_inline_chars=1_000, paste_inline_chars=2_000, excerpt_chars=200
    )


def notes(**kw: object) -> NotesParams:
    return NotesParams.model_validate({"story": "They talked.", **kw})


@pytest.mark.parametrize(
    "words",
    [
        "Actually, make it 4 people, not 3.",
        "No, the meeting is on Tuesday.",
        "wait - I meant the blue one",
        "Not the hotel by the station but the one by the river.",
        "From now on, answer in French.",
        "That's wrong: my sister is Ana.",
        "I don't drink coffee anymore",
        "Book the 9:40 instead.",
    ],
)
def test_corrections_are_recognised(words: str) -> None:
    assert looks_like_correction(words)


@pytest.mark.parametrize(
    "words",
    ["Sounds great, thanks!", "What's the weather in Lisbon?", "Not now.", "x" * 700 + " actually"],
)
def test_ordinary_messages_are_not_corrections(words: str) -> None:
    assert not looks_like_correction(words)


def test_corrections_are_copied_word_for_word_by_the_code(tmp_path: Path) -> None:
    folded = fold(
        turns("Let's book for 3 people.", "Actually, make it 4 people, not 3.", "Thanks"),
        store(tmp_path),
        limit=200,
    )
    new = merge(None, notes(corrections=[3]), folded)  # the model points at turn 3 too
    assert new.corrections == (
        Correction(turn=1, words="Actually, make it 4 people, not 3."),
        Correction(turn=2, words="Thanks"),
    )
    check(None, new, folded, [])


def test_a_correction_the_model_missed_is_kept_anyway(tmp_path: Path) -> None:
    folded = fold(turns("Hi", "No, my name is Ana, not Anna."), store(tmp_path), limit=200)
    new = merge(None, notes(), folded)
    assert [c.words for c in new.corrections] == ["No, my name is Ana, not Anna."]


def test_losing_a_correction_fails_the_summary(tmp_path: Path) -> None:
    """The regression guard of spec §5.3: a compaction never loses a user correction."""
    first = fold(turns("Actually, I'm vegetarian now."), store(tmp_path), limit=200)
    old = merge(None, notes(), first)
    later = fold(turns("Plan dinner", start=1), store(tmp_path), limit=200)
    new = merge(old, notes(), later)
    assert old.corrections[0] in new.corrections
    check(old, new, later, [])
    dropped = new.model_copy(update={"corrections": ()})
    with pytest.raises(SummaryInvariantError, match="the correction from turn 1"):
        check(old, dropped, later, [])
    missed = merge(None, notes(), first).model_copy(update={"corrections": ()})
    with pytest.raises(SummaryInvariantError, match="correction-like message of turn 1"):
        check(None, missed, first, [])
    reworded = merge(None, notes(), first).model_copy(
        update={"corrections": (Correction(turn=0, words="The user is vegetarian."),)}
    )
    with pytest.raises(SummaryInvariantError, match="not the user's own words"):
        check(None, reworded, first, [])


def test_earlier_notes_stay_unless_retired_by_id(tmp_path: Path) -> None:
    first = fold(turns("We decide on Lisbon."), store(tmp_path), limit=200)
    old = merge(None, notes(decisions=["Lisbon"], open_tasks=["Book flights", "Book hotel"]), first)
    assert [n.id for n in old.open_tasks] == ["T1", "T2"]
    later = fold(turns("Flights are booked.", start=1), store(tmp_path), limit=200)
    new = merge(old, notes(open_tasks=["Pack"], retire=["T1"]), later)
    assert [n.text for n in new.open_tasks] == ["Book hotel", "Pack"]
    assert [n.id for n in new.open_tasks] == ["T2", "T3"]  # ids never reused
    assert new.decisions == old.decisions
    check(old, new, later, ["T1"])
    silently = new.model_copy(update={"decisions": ()})
    with pytest.raises(SummaryInvariantError, match="decision D1"):
        check(old, silently, later, ["T1"])
    assert new.story == ("Turn 1: They talked.", "Turn 2: They talked.")


def test_long_parts_are_stored_and_their_pointers_kept(tmp_path: Path) -> None:
    keep = store(tmp_path)
    page = "PAGE " * 500
    call = ToolCall(id="c1", name="read_page", arguments_json='{"url": "https://x.test"}')
    result = ToolResult(call_id="c1", name="read_page", content=fence(page, source="read_page"))
    folded = fold(
        {0: [user("read x.test"), Message.assistant(tool_calls=(call,)), Message.tool(result)]},
        keep,
        limit=200,
    )
    assert len(folded.text) < 1_500
    (stored_id,) = folded.refs
    assert keep.text(stored_id) == page.strip("\n") or keep.text(stored_id) == page
    assert "<untrusted-data" in folded.text  # still marked as outside content
    new = merge(None, notes(), folded)
    assert [r.id for r in new.stored] == [stored_id]
    check(None, new, folded, [])
    with pytest.raises(SummaryInvariantError, match=f"the stored text {stored_id}"):
        check(None, new.model_copy(update={"stored": ()}), folded, [])
    # A pointer already in a message (a shortened result) is carried, not stored again.
    again = fold({1: [user(f"see stored text {stored_id}")]}, keep, limit=200)
    assert set(again.refs) == {stored_id}


def test_the_rendering_is_deterministic_and_complete() -> None:
    summary = Summary(
        turns=12,
        story=("Turns 1 to 12: Planned a trip.",),
        decisions=(Note(id="D1", text="Lisbon, Friday the 12th (turn 2)"),),
        facts=(Note(id="F1", text="Travels with Ana"),),
        open_tasks=(Note(id="T1", text="Book the hotel"),),
        corrections=(Correction(turn=6, words="Actually, 4 people."),),
        stored=(StoredRef(id="s_0123456789abcdef", what="read_page, 9,000 characters"),),
    )
    text = render(summary)
    assert text == render(Summary.model_validate(summary.model_dump(mode="json")))
    for piece in (
        "[Summary of turns 1 to 12",
        "D1: Lisbon, Friday the 12th (turn 2)",
        "F1: Travels with Ana",
        "T1: Book the hotel",
        'Turn 7: "Actually, 4 people."',
        "s_0123456789abcdef: read_page, 9,000 characters",
        "read_stored_output",
    ):
        assert piece in text


def test_unfence_reverses_fence() -> None:
    inside = 'a "quoted" text\nwith lines'
    assert unfence(fence(inside, source="read_page")) == (inside, True)
    assert unfence(fence(inside, source="x") + "\n[note]") == (f"{inside}\n[note]", True)
    assert unfence("plain") == ("plain", False)


def test_folded_turns_know_their_span() -> None:
    folded = Folded(turns={3: [], 4: [], 9: []}, text="", refs={})
    assert (folded.first, folded.last) == (3, 9)
