"""MCP connectors against an in-process fake of GitHub's MCP server (real MCP protocol)."""

import json
from typing import Any

import pytest

from enzo.agent.loop import ApprovalNeeded, ToolFinished, ToolStarted, TurnDone, run_turn
from enzo.brain import Message, ToolCall
from enzo.brain.config import BrainsConfig
from enzo.brain.fake import ScriptedBrain
from enzo.brain.router import Router
from enzo.connectors.github import github_server
from enzo.connectors.mcp import (
    RATE_LIMIT_PAUSE_S,
    ConnectorError,
    ConnectorStoppedError,
    McpConnector,
)
from enzo.tools import ToolRegistry
from tests.connectors.fake_github import PLANTED, State, client_for, fake_github


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def connector(clock: Clock | None = None) -> tuple[McpConnector, State]:
    server, state = fake_github()
    return McpConnector(github_server(), lambda: client_for(server), clock=clock or Clock()), state


def call(name: str, cid: str = "c1", **args: Any) -> ToolCall:
    return ToolCall(id=cid, name=name, arguments_json=json.dumps(args))


async def test_only_allowed_tools_with_the_right_gates() -> None:
    hub, _ = connector()
    tools = {t.name: t for t in await hub.tools()}
    assert set(tools) == {
        "github_get_me",
        "github_pull_request_read",
        "github_merge_pull_request",
        "github_issue_read",
    }  # delete_repository is not on the allowlist
    assert not tools["github_get_me"].requires_approval  # marked read-only by the server
    assert tools["github_merge_pull_request"].requires_approval  # unmarked: ask (safe default)
    assert all(t.untrusted_output for t in tools.values())  # §12.4
    assert tools["github_merge_pull_request"].description.startswith("GitHub: ")
    schema = tools["github_pull_request_read"].spec().parameters
    assert set(schema["required"]) == {"method", "owner", "repo", "pullNumber"}


async def test_arguments_are_checked_before_github_is_called() -> None:
    hub, state = connector()
    registry = ToolRegistry(await hub.tools())
    bad = await registry.execute(call("github_issue_read", owner="o", repo="r"))  # no number
    assert bad.status == "invalid_arguments"
    assert "issue_number" in bad.result.content
    wrong_type = await registry.execute(
        call("github_issue_read", owner="o", repo="r", issue_number="seven")
    )
    assert wrong_type.status == "invalid_arguments"
    assert state.calls == []  # nothing reached GitHub


async def test_results_are_fenced_as_untrusted() -> None:
    hub, _ = connector()
    registry = ToolRegistry(await hub.tools())
    out = await registry.execute(call("github_issue_read", owner="o", repo="r", issue_number=7))
    assert out.status == "ok"
    assert out.result.content.startswith('<untrusted-data source="github_issue_read" id="')
    assert PLANTED in out.result.content


async def test_a_rate_limit_stops_the_whole_connector_without_retrying() -> None:
    clock = Clock()
    hub, state = connector(clock)
    state.rate_limited = True
    with pytest.raises(ConnectorError, match="rate limit"):
        await hub.call("get_me", {})
    state.rate_limited = False
    with pytest.raises(ConnectorStoppedError, match=r"paused for 15 minutes\. Nothing was retried"):
        await hub.call("issue_read", {"owner": "o", "repo": "r", "issue_number": 1})
    assert state.calls == ["get_me"]  # §8.3: the stopped scope sent nothing more
    clock.now += RATE_LIMIT_PAUSE_S + 1
    await hub.call("get_me", {})  # back after the pause
    assert state.calls == ["get_me", "get_me"]


async def test_a_rejected_token_stops_until_reconnected() -> None:
    clock = Clock()
    hub, state = connector(clock)
    state.bad_token = True
    with pytest.raises(ConnectorError):
        await hub.call("get_me", {})
    state.bad_token = False
    clock.now += 10 * RATE_LIMIT_PAUSE_S
    with pytest.raises(ConnectorStoppedError, match="reconnect with /login github"):
        await hub.call("get_me", {})


async def test_numbers_in_ordinary_errors_do_not_stop_the_connector() -> None:
    hub, _ = connector()
    hub._notice_failure("Pull request #429 not found; issue 401 is closed")
    await hub.call("get_me", {})  # still running


# ---------- the merge flow through a real agent turn (issue #2 "done when") ----------


def router_for(brain: ScriptedBrain) -> Router:
    config = BrainsConfig.model_validate(
        {
            "brains": {
                "b": {"api": "anthropic", "model": "m", "api_key_env": "K", "tier": "balanced"}
            }
        }
    )
    return Router(config, build=lambda _c: brain, available=lambda _c: True)


MERGE = call("github_merge_pull_request", owner="octo", repo="site", pullNumber=12)


async def turn(
    brain: ScriptedBrain, registry: ToolRegistry, history: list[Message], decided: dict[str, bool]
) -> list[Any]:
    return [e async for e in run_turn(router_for(brain), registry, history, decisions=decided)]


async def test_a_merge_is_pinned_to_the_head_and_shows_the_diff() -> None:
    hub, state = connector()
    registry = ToolRegistry(await hub.tools())
    brain = ScriptedBrain([Message.assistant(tool_calls=(MERGE,)), Message.assistant("Merged.")])
    history = [Message.user("merge PR 12")]
    events = await turn(brain, registry, history, {})
    (asked,) = [e.request for e in events if isinstance(e, ApprovalNeeded)]
    assert asked.arguments["expectedHeadSha"] == state.head  # pinned before asking
    assert asked.detail is not None
    assert f"at commit {state.head[:12]}" in asked.detail
    assert "\n+hello from" in asked.detail  # the diff is on the card, as real lines
    assert '{"diff"' not in asked.detail  # not GitHub's JSON wrapper
    assert asked.summary == "GitHub: Merge pull request (octo/site #12)"
    assert state.merged == []  # nothing merged without approval

    done = events[-1]
    assert isinstance(done, TurnDone)
    events = await turn(brain, registry, list(done.outcome.messages), {asked.id: True})
    assert state.merged == [12]
    finished = [e.outcome for e in events if isinstance(e, ToolFinished)]
    assert finished[0].status == "ok"
    # The chat's step line says it in the same plain words as the card (#40).
    (started,) = [e.label for e in events if isinstance(e, ToolStarted)]
    assert started == "GitHub: Merge pull request (octo/site #12)"
    assert json.loads(finished[0].call.arguments_json)["expectedHeadSha"] == state.head


async def test_if_the_pr_changes_after_asking_the_user_is_asked_again() -> None:
    hub, state = connector()
    registry = ToolRegistry(await hub.tools())
    brain = ScriptedBrain([Message.assistant(tool_calls=(MERGE,))])
    events = await turn(brain, registry, [Message.user("merge PR 12")], {})
    (first,) = [e.request for e in events if isinstance(e, ApprovalNeeded)]
    state.head = "ffffeeeeddddcccc"  # someone pushed after the card was shown
    done = events[-1]
    assert isinstance(done, TurnDone)
    events = await turn(brain, registry, list(done.outcome.messages), {first.id: True})
    (again,) = [e.request for e in events if isinstance(e, ApprovalNeeded)]
    assert again.id != first.id  # a new exact action: the old yes does not cover it
    assert again.arguments["expectedHeadSha"] == "ffffeeeeddddcccc"
    assert state.merged == []


async def test_a_declined_merge_never_runs() -> None:
    hub, state = connector()
    registry = ToolRegistry(await hub.tools())
    brain = ScriptedBrain([Message.assistant(tool_calls=(MERGE,)), Message.assistant("OK.")])
    events = await turn(brain, registry, [Message.user("merge PR 12")], {})
    (asked,) = [e.request for e in events if isinstance(e, ApprovalNeeded)]
    done = events[-1]
    assert isinstance(done, TurnDone)
    await turn(brain, registry, list(done.outcome.messages), {asked.id: False})
    assert state.merged == []
    assert "merge_pull_request" not in state.calls


async def test_a_merge_that_cannot_be_checked_is_not_run() -> None:
    hub, state = connector()
    registry = ToolRegistry(await hub.tools())
    state.rate_limited = True  # the PR cannot be read, so the merge cannot be pinned
    brain = ScriptedBrain([Message.assistant(tool_calls=(MERGE,)), Message.assistant("Stopped.")])
    events = await turn(brain, registry, [Message.user("merge PR 12")], {})
    assert not [e for e in events if isinstance(e, ApprovalNeeded)]
    finished = [e.outcome for e in events if isinstance(e, ToolFinished)]
    assert finished[0].status == "failed"
    assert "Could not check this action, so it was not run" in finished[0].result.content
    assert state.merged == []
