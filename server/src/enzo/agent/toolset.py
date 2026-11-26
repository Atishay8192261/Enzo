"""Enzo's own tools, as one set: the gateway runs them for the user, and `enzo eval` runs the
same set in a throwaway workspace and database, so the evals measure what users get."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx2

from enzo.goals import Goals, goal_tools
from enzo.memory import Memory, memory_tools
from enzo.schedule import FinishedTasks, Schedule, schedule_tools
from enzo.tools.files import file_tools
from enzo.tools.registry import Tool
from enzo.tools.web import web_tools


def core_tools(  # noqa: PLR0913 - keyword-only, one per store
    *,
    workspace: Path,
    memory: Memory,
    schedule: Schedule,
    finished: FinishedTasks,
    goals: Goals,
    http: httpx2.AsyncClient,
    tavily_key: Callable[[], str | None],
    on_set: Callable[[], None] = lambda: None,
) -> list[Tool[Any, Any]]:
    """Web, files, memory, scheduling and goals. Connectors (GitHub, Google) come and go with
    their accounts."""
    return [
        *web_tools(tavily_key=tavily_key, http=http),
        *file_tools(workspace),
        *memory_tools(memory),
        *schedule_tools(schedule, finished, on_set=on_set),
        *goal_tools(goals),
    ]
