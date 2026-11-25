"""`enzo eval` end to end: the real loop, tools, memory, approvals and Postgres; a scripted agent
plays the model (spec §13.2). Every task must pass for a capable agent and fail for a flawed one."""

import asyncio
from collections.abc import Sequence
from typing import Any

import asyncpg
import pytest

from enzo.brain.config import BrainConfig, BrainsConfig
from enzo.brain.errors import AuthError, RateLimitError
from enzo.brain.provider import BrainProvider
from enzo.evals.harness import run_suite
from enzo.evals.results import RunResult, TaskResult, markdown
from enzo.evals.suite import SUITE, EvalTask, select
from enzo.store.repository import Store
from tests.evals.agent import BAD, GOOD, UNGATED, SuiteAgent, bad_agent, offline_http, tools_for
from tests.fakes import fake_key
from tests.pg import ADMIN_URL

PRICED = {"source": "test", "rates": {"input": 1.0, "output": 2.0}}


def config() -> BrainsConfig:
    brain = {"api": "openai-responses", "provider": "fake", "api_key_env": "KEY"}
    return BrainsConfig.model_validate(
        {
            "brains": {
                "suite-priced": {
                    **brain,
                    "model": "suite-1",
                    "tier": "balanced",
                    "pricing": PRICED,
                },
                "suite-unpriced": {**brain, "model": "suite-2", "tier": "balanced"},
                # Another provider, connected: an eval must never fall back to it.
                "other": {
                    "api": "anthropic",
                    "model": "other-1",
                    "api_key_env": "K2",
                    "tier": "balanced",
                },
            }
        }
    )


def run(
    database_url: str,
    brains: dict[str, BrainProvider],
    *,
    name: str = "suite-priced",
    tasks: Sequence[EvalTask] = SUITE,
    online: bool = True,
    ungated: dict[str, str] | None = None,
    secrets: Sequence[str] = (),
) -> RunResult:
    async def go() -> RunResult:
        def build(c: BrainConfig) -> BrainProvider:
            return brains[c.model]

        async with offline_http() as http:
            return await run_suite(
                [name],
                config=config(),
                build=build,
                database_url=database_url,
                tools=tools_for(http, ungated=ungated),
                tasks=tasks,
                online=online,
                secrets=secrets,
            )

    return asyncio.run(go())


def by_id(result: RunResult) -> dict[str, TaskResult]:
    return {t.id: t for t in result.brains[0].tasks}


@pytest.fixture(scope="module")
def good(database_url: str) -> RunResult:
    return run(database_url, {"suite-1": SuiteAgent(GOOD)})


@pytest.fixture(scope="module")
def bad(database_url: str) -> RunResult:
    return run(database_url, {"suite-1": SuiteAgent(bad_agent())}, ungated=UNGATED)


def test_a_capable_agent_passes_every_task(good: RunResult) -> None:
    failed = {t.id: t.reason for t in good.brains[0].tasks if t.status != "passed"}
    assert failed == {}
    summary = good.brains[0].summary
    assert (summary.passed, summary.pass_rate, summary.skipped) == (20, 1.0, 0)
    assert set(summary.by_category) == {
        "tool_accuracy",
        "grounding",
        "memory",
        "safety",
        "multi_step",
    }


@pytest.mark.parametrize("task", [t.id for t in SUITE])
def test_every_task_fails_a_flawed_agent_and_says_why(bad: RunResult, task: str) -> None:
    """The grading has teeth: each task has a flawed run that it fails, for the right reason."""
    result = by_id(bad)[task]
    assert result.status == "failed", result.actual
    assert BAD[task][1] in result.reason


def test_results_carry_the_spec_columns(good: RunResult) -> None:
    """Spec §15.5: task, expected, actual, pass/fail, latency, cost; §13.2 adds first token."""
    tasks = by_id(good)
    create = tasks["files.create"]
    assert create.latency_ms is not None
    assert create.latency_ms > 0
    assert create.first_token_ms is not None
    assert create.first_token_ms <= create.latency_ms
    assert create.input_tokens > 0
    assert create.output_tokens > 0
    expected_cost = (create.input_tokens * 1.0 + create.output_tokens * 2.0) / 1e6
    assert create.cost_usd == pytest.approx(expected_cost)
    assert create.tool_calls[0].tool == "create_file"
    assert '"path": "shopping.md"' in create.tool_calls[0].arguments
    assert all(b.model == "suite-1" for b in create.brain_calls)  # pinned: no other brain
    assert create.runs
    assert all(b.run in create.runs for b in create.brain_calls)
    # The trace of an approved call shows its approval.
    (deleted,) = [c for c in tasks["safety.approved_exact"].tool_calls if c.tool == "delete_file"]
    assert deleted.approval == "approved"
    assert tasks["safety.approved_exact"].stops == ["approval_pending", "final_answer"]
    table = markdown(good)
    assert (
        "| Task | Expected | Actual | Result | First token | Latency | Tokens in/out | Cost |"
        in table
    )
    assert all(f"`{t.id}`" in table for t in SUITE)
    assert RunResult.model_validate_json(good.model_dump_json()) == good


def test_a_run_never_touches_the_users_data(database_url: str) -> None:
    """Throwaway database and workspace: the user's tables stay empty, nothing is left behind."""

    async def databases() -> set[str]:
        conn = await asyncpg.connect(ADMIN_URL)
        try:
            return {r["datname"] for r in await conn.fetch("SELECT datname FROM pg_database")}
        finally:
            await conn.close()

    before = asyncio.run(databases())
    result = run(
        database_url,
        {"suite-1": SuiteAgent(GOOD)},
        tasks=select(["memory", "safety.gate_schedule"]),
    )
    assert result.brains[0].summary.passed == 4
    assert asyncio.run(databases()) == before  # the run's own database was dropped

    async def counts() -> list[int]:
        store = Store.from_url(database_url)
        try:
            async with store.engine.connect() as conn:
                return [
                    (await conn.exec_driver_sql(f"SELECT count(*) FROM {table}")).scalar_one()  # noqa: S608
                    for table in ("conversations", "memories", "jobs", "tool_calls")
                ]
        finally:
            await store.close()

    assert asyncio.run(counts()) == [0, 0, 0, 0]


def test_offline_the_internet_tasks_are_skipped_not_failed(database_url: str) -> None:
    tasks = select(["grounding"])
    result = run(database_url, {"suite-1": SuiteAgent(GOOD)}, tasks=tasks, online=False)
    statuses = {t.id: t.status for t in result.brains[0].tasks}
    assert statuses == {
        "grounding.price": "skipped",
        "grounding.date": "skipped",
        "grounding.read_page": "skipped",
        "grounding.failed_fetch": "passed",  # needs no internet: the address cannot work
    }
    summary = result.brains[0].summary
    assert (summary.skipped, summary.pass_rate) == (3, 1.0)


def test_an_unpriced_brain_costs_unknown_never_zero(database_url: str) -> None:
    result = run(
        database_url,
        {"suite-2": SuiteAgent(GOOD, model="suite-2")},
        name="suite-unpriced",
        tasks=select(["files.create"]),
    )
    (task,) = result.brains[0].tasks
    assert task.input_tokens > 0
    assert (task.cost_usd, task.plan, result.brains[0].summary.cost_usd) == (None, False, None)
    assert "| unknown |" in markdown(result)


def test_a_plan_paid_brain_says_so_instead_of_a_price(database_url: str) -> None:
    result = run(
        database_url,
        {"suite-1": SuiteAgent(GOOD, billing="plan")},
        tasks=select(["files.create"]),
    )
    (task,) = result.brains[0].tasks
    assert (task.cost_usd, task.plan) == (None, True)
    assert "your plan" in markdown(result)


class Rejected(SuiteAgent):
    """A key the provider refuses; its error echoes the key, as some providers do."""

    def __init__(self, key: str) -> None:
        super().__init__(GOOD)
        self.key = key
        self.calls = 0

    async def chat(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        msg = f"Incorrect API key provided: {self.key} (sk-proj-****{'ab' * 2})"
        raise AuthError(msg, provider="fake", model="suite-1")


def test_a_rejected_key_stops_the_run_and_never_reaches_the_results(database_url: str) -> None:
    key = fake_key("rejected")
    brain = Rejected(key)
    result = run(database_url, {"suite-1": brain}, tasks=SUITE[:3], secrets=[key])
    tasks = result.brains[0].tasks
    assert [t.status for t in tasks] == ["error", "error", "error"]
    assert brain.calls == 1  # not tried again for every task
    assert result.brains[0].stopped is not None
    assert tasks[1].reason.startswith("not run: suite-priced cannot answer")
    saved = result.model_dump_json() + markdown(result)
    assert key not in saved
    assert "sk-proj-" not in saved
    assert "[redacted]" in saved


class Busy(SuiteAgent):
    async def chat(self, *args: Any, **kwargs: Any) -> Any:
        raise RateLimitError("slow down", provider="fake", model="suite-1")


def test_a_failing_brain_is_never_rescued_by_another(database_url: str) -> None:
    """Results are the tested brain's own: no fallback to another connected provider."""
    other = SuiteAgent(GOOD, model="other-1")
    result = run(database_url, {"suite-1": Busy(GOOD), "other-1": other}, tasks=SUITE[:1])
    (task,) = result.brains[0].tasks
    assert task.status == "failed"
    assert other.seen == []
    assert [b.outcome for b in task.brain_calls] == ["rate_limit"]
