"""The task set itself: fixed, versioned, and covering every metric of spec §13.2 and §17."""

from collections import Counter
from pathlib import Path

import httpx2
import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from enzo.agent.toolset import core_tools
from enzo.evals.suite import (
    CATEGORIES,
    FINGERPRINT,
    SUITE,
    TOOL_NAMES,
    fingerprint,
    select,
)
from enzo.memory import Memory
from enzo.schedule import FinishedTasks, Schedule


def test_twenty_tasks_with_unique_ids_and_prompts() -> None:
    assert len(SUITE) == 20  # spec §13.3: "the same 20 tasks"
    assert len({t.id for t in SUITE}) == 20
    prompts = [turn.text for t in SUITE for turn in t.turns]
    assert len(set(prompts)) == len(prompts)  # each message names its task
    assert all(t.checks for t in SUITE)


def test_every_metric_of_the_spec_is_measured() -> None:
    counts = Counter(t.category for t in SUITE)
    assert set(counts) == set(CATEGORIES)
    ids = {t.id for t in SUITE}
    # §17: a 3-tool sequential task, a tool-loop task stopped at max steps; §12.7 safety checks.
    assert {"multistep.three_tools", "multistep.max_steps"} <= ids
    assert {"safety.gate_delete", "safety.injection_file", "safety.hard_boundary"} <= ids
    # Only live checks need the internet; everything else runs offline.
    online = {t.id for t in SUITE if t.needs_network}
    assert online == {
        "grounding.price",
        "grounding.date",
        "grounding.read_page",
        "safety.private_search",
    }


def test_a_changed_task_needs_a_new_version() -> None:
    """Results of different task sets must never be compared silently: edit a task, then bump
    VERSION and set FINGERPRINT to the new value."""
    assert fingerprint() == FINGERPRINT, (
        f"the tasks changed: bump VERSION and set FINGERPRINT = {fingerprint()!r}"
    )


async def test_the_tool_names_list_is_complete(tmp_path: Path) -> None:
    """A report that names a tool fails the max-steps task: the list must know every tool."""
    engine = create_async_engine("postgresql+asyncpg://nobody@localhost/none")  # never connects
    async with httpx2.AsyncClient() as http:
        tools = core_tools(
            workspace=tmp_path,
            memory=Memory(engine),
            schedule=Schedule(engine),
            finished=FinishedTasks(),
            http=http,
            tavily_key=lambda: None,
        )
    await engine.dispose()
    assert {t.name for t in tools} == set(TOOL_NAMES)


@pytest.mark.parametrize(
    ("wanted", "expected"),
    [
        (["memory"], ["memory.new_chat", "memory.correction", "memory.latest_decision"]),
        (["safety.gate_delete"], ["safety.gate_delete"]),
        (["multi_step"], ["multistep.three_tools", "multistep.max_steps"]),
    ],
)
def test_tasks_can_be_picked_by_id_prefix_or_category(
    wanted: list[str], expected: list[str]
) -> None:
    assert [t.id for t in select(wanted)] == expected


def test_an_unknown_task_is_refused_with_the_choices() -> None:
    with pytest.raises(KeyError, match=r"no task nope: choose from files\.create"):
        select(["nope"])
