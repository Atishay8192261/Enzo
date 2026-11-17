from datetime import date

import pytest

from enzo.agent.prompt import MemorySnippet, compose_prompt
from enzo.brain import Message


def test_layers_come_in_the_spec_order() -> None:
    history = [Message.user("earlier"), Message.assistant("reply")]
    prompt = compose_prompt(
        system="SYSTEM",
        developer="DEVELOPER",
        memory=[MemorySnippet("Prefers aisle seats", "chat 2026-11-10", date(2026, 11, 10))],
        history=history,
        user_message="NOW",
    )
    assert [m.role for m in prompt] == ["system", "system", "system", "user", "assistant", "user"]
    assert (prompt[0].text, prompt[1].text) == ("SYSTEM", "DEVELOPER")
    assert "Prefers aisle seats (source: chat 2026-11-10, recorded 2026-11-10)" in prompt[2].text
    assert "verify against the source" in prompt[2].text
    assert prompt[3:5] == history
    assert prompt[-1].text == "NOW"


def test_empty_layers_are_left_out() -> None:
    prompt = compose_prompt(system="S", developer=None, memory=[], history=[], user_message="U")
    assert [m.role for m in prompt] == ["system", "user"]


def test_history_cannot_smuggle_in_system_instructions() -> None:
    with pytest.raises(ValueError, match="system"):
        compose_prompt(
            system="S", developer=None, memory=[], history=[Message.system("x")], user_message="U"
        )
