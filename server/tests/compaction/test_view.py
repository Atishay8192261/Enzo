"""The prompt is a pure function of the transcript and the latest summary (ADR 0024)."""

from pathlib import Path

from enzo.brain.models import Message, NativePart, TextPart, ToolCall, ToolResult
from enzo.compaction import Compaction, Summary, build_view
from enzo.compaction.view import turn_positions, without_replay
from enzo.tools.stored import OutputStore

SYSTEM = Message.system("rules")
NATIVE = NativePart(provider="anthropic", payload={"thinking": "x"})


def reply(text: str) -> Message:
    return Message(role="assistant", parts=(NATIVE, TextPart(text=text)))


def record(turns: int, made_turn: int, made_offset: int) -> Compaction:
    return Compaction(Summary(turns=turns), turns, made_turn, made_offset, "b", 0, 0)


CALL = ToolCall(id="c1", name="t", arguments_json="{}")
CONVERSATION = [
    Message.user("one"),
    reply("r1"),
    Message.user("two"),
    Message(role="assistant", parts=(NATIVE, CALL)),
    Message.tool(ToolResult(call_id="c1", name="t", content="ok")),
    reply("r2"),
    Message.user("three"),
    reply("r3"),
]


def test_turns_start_at_user_messages() -> None:
    assert turn_positions(CONVERSATION) == [
        (0, 0), (0, 1), (1, 0), (1, 1), (1, 2), (1, 3), (2, 0), (2, 1)
    ]  # fmt: skip
    assert turn_positions([reply("early"), Message.user("u")]) == [(0, 0), (0, 1)]


def test_summarized_turns_go_and_older_replay_data_is_dropped() -> None:
    view = build_view([SYSTEM, *CONVERSATION], record(turns=1, made_turn=2, made_offset=1))
    assert view[0] == SYSTEM
    assert view[1].role == "user"
    assert view[1].text.startswith("[Summary of turns 1 to 1")
    assert [m.text for m in view[2:]] == ["two", "", "", "r2", "three", "r3"]
    # Made in turn 3 after its first message: everything before lost its replay data …
    assert [m.native("anthropic") is not None for m in view[2:]] == [
        False, False, False, False, False, True
    ]  # fmt: skip
    # … and the tool call keeps its pair, word for word.
    assert view[3].tool_calls == (CALL,)
    assert view[4].parts[0] == CONVERSATION[4].parts[0]


def test_no_summary_means_the_conversation_as_it_is() -> None:
    assert build_view([SYSTEM, *CONVERSATION], None) == [SYSTEM, *CONVERSATION]


def test_a_reply_that_was_only_replay_data_is_left_out() -> None:
    assert without_replay(Message(role="assistant", parts=(NATIVE,))) is None
    plain = Message.user("x")
    assert without_replay(plain) is plain


def test_a_long_paste_is_shortened_the_same_way_every_time(tmp_path: Path) -> None:
    store = OutputStore(
        tmp_path, tool_inline_chars=500, paste_inline_chars=1_000, excerpt_chars=100
    )
    shorten = store.shorten_paste
    text = "doc " * 1_000 + "So what does it say about rent?"
    paste = Message(role="user", parts=(TextPart(text=text), TextPart(text="[sent]")))
    first = build_view([SYSTEM, paste], None, paste_limit=1_000, shorten=shorten)
    again = build_view([SYSTEM, paste], None, paste_limit=1_000, shorten=shorten)
    assert first == again  # byte-identical: the cached prefix holds
    words = first[1].parts[0]
    assert isinstance(words, TextPart)
    assert words.text.startswith("doc doc")
    assert "stored text s_" in words.text
    assert "So what does it say about rent?" in words.text  # the question after the document
    assert len(words.text) < 700
    assert first[1].parts[1] == TextPart(text="[sent]")
    short = Message.user("short")
    assert build_view([SYSTEM, short], None, paste_limit=1_000, shorten=shorten)[1] is short
