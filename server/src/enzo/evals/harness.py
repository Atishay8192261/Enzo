"""Run the suite through the real agent loop and real tools, on one brain at a time.

Nothing touches the user's chats, memory, files or schedule: the run gets its own database
(created beside the user's, migrated, and dropped at the end), every task starts from empty
tables and a fresh temporary workspace, and no reminder or task runner is started, so nothing
scheduled ever fires. The brain under test is pinned with no fallback: every answer is its own.
"""

import asyncio
import contextlib
import contextvars
import json
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from enzo import __version__
from enzo.agent.loop import TurnDone, TurnOutcome
from enzo.agent.prompt import install_note
from enzo.agent.service import ConversationService, TurnFailed, TurnMeta
from enzo.agent.toolset import core_tools
from enzo.brain.config import BrainsConfig
from enzo.brain.models import TextDelta
from enzo.brain.router import BrainFactory, CallRecord, Router
from enzo.evals.checks import (
    ApprovalSeen,
    CallSeen,
    JobSeen,
    MemorySeen,
    Observed,
    TurnSeen,
    all_checks,
)
from enzo.evals.results import (
    BrainResult,
    CheckResult,
    RunResult,
    TaskResult,
    summarize,
)
from enzo.evals.suite import NAME, SUITE, TIMEZONE, VERSION, EvalTask, fingerprint
from enzo.memory import Memory
from enzo.redact import redact
from enzo.schedule import FinishedTasks, Schedule
from enzo.store.migrate import upgrade
from enzo.store.repository import Store
from enzo.store.schema import (
    ApprovalRow,
    Base,
    BrainCallRow,
    JobRow,
    MemoryRow,
    ToolCallRow,
)
from enzo.tools.files import HIDDEN
from enzo.tools.registry import Tool, ToolRegistry, plain_words
from enzo.tracing import brain_step, tool_step

#: A task that takes longer is stopped and counted as an error (a stuck provider, a hung tool).
TASK_TIMEOUT_S = 600.0
#: Approval rounds per message before the harness gives up (a model that keeps asking).
MAX_APPROVAL_ROUNDS = 4
#: Failures that make every later task on this brain fail the same way: stop instead.
FATAL = ("auth", "not_connected")
DEVICE = "enzo eval"
REPLY_CHARS = 300


@dataclass(frozen=True, slots=True)
class TaskEnv:
    """What a task's tools are built on: its own workspace, memory and schedule."""

    task: EvalTask
    workspace: Path
    memory: Memory
    schedule: Schedule
    finished: FinishedTasks


type ToolsFactory = Callable[[TaskEnv], Sequence[Tool[Any, Any]]]


def standard_tools(http: httpx2.AsyncClient, tavily_key: Callable[[], str | None]) -> ToolsFactory:
    """The tools the gateway gives the user (minus connectors), bound to the task's workspace."""

    def make(env: TaskEnv) -> Sequence[Tool[Any, Any]]:
        return core_tools(
            workspace=env.workspace,
            memory=env.memory,
            schedule=env.schedule,
            finished=env.finished,
            http=http,
            tavily_key=tavily_key,
        )

    return make


def pinned(config: BrainsConfig, name: str, build: BrainFactory) -> Router:
    """A router that only ever uses ``name``: no other brain is available, so no fallback."""
    chosen = config.brains[name]
    return Router(config, build=build, available=lambda c: c is chosen)


@asynccontextmanager
async def throwaway_database(base_url: str) -> AsyncIterator[str]:
    """A new, migrated database on the same server as ``base_url``, dropped afterwards."""
    name = f"enzo_eval_{uuid.uuid4().hex[:10]}"
    target = make_url(base_url).set(database=name).render_as_string(hide_password=False)
    admin = create_async_engine(base_url, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.exec_driver_sql(f'CREATE DATABASE "{name}"')
        await asyncio.to_thread(upgrade, target)  # Alembic runs its own event loop
        yield target
    finally:
        async with admin.connect() as conn:
            await conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.dispose()


class _UnusableError(Exception):
    """The brain cannot answer at all (no key, key rejected): later tasks would fail the same."""


@dataclass
class _Run:
    """What one task's messages produced, as they ran."""

    turns: list[TurnSeen] = field(default_factory=list)
    calls: list[CallRecord] = field(default_factory=list)
    #: Tool-call row id reached after each message (rows are numbered in order).
    tool_marks: list[int] = field(default_factory=list)
    first_word_ms: float | None = None
    latency_ms: float = 0.0
    failure: str | None = None
    #: The chat in use now: a turn still running there is stopped if the task fails.
    conversation: uuid.UUID | None = None


async def run_suite(  # noqa: PLR0913 - keyword-only run settings
    brains: Sequence[str],
    *,
    config: BrainsConfig,
    build: BrainFactory,
    database_url: str,
    tools: ToolsFactory,
    tasks: Sequence[EvalTask] = SUITE,
    online: bool = True,
    secrets: Sequence[str] = (),
    progress: Callable[[str, TaskResult], None] = lambda _brain, _result: None,
    run_id: str | None = None,
) -> RunResult:
    """Every task on every brain, one after another; results only ever hold redacted text."""
    started = datetime.now(UTC)
    out: list[BrainResult] = []
    async with throwaway_database(database_url) as url:
        store = Store.from_url(url)
        try:
            for name in brains:
                out.append(
                    await _on_brain(
                        name,
                        config=config,
                        build=build,
                        store=store,
                        tools=tools,
                        tasks=tasks,
                        online=online,
                        secrets=secrets,
                        progress=progress,
                    )
                )
        finally:
            await store.close()
    return RunResult(
        run_id=run_id or started.strftime("%Y%m%dT%H%M%SZ"),
        suite=NAME,
        suite_version=VERSION,
        fingerprint=fingerprint(),
        enzo_version=__version__,
        started_at=started,
        finished_at=datetime.now(UTC),
        online=online,
        timezone=TIMEZONE,
        brains=out,
    )


async def _on_brain(  # noqa: PLR0913
    name: str,
    *,
    config: BrainsConfig,
    build: BrainFactory,
    store: Store,
    tools: ToolsFactory,
    tasks: Sequence[EvalTask],
    online: bool,
    secrets: Sequence[str],
    progress: Callable[[str, TaskResult], None],
) -> BrainResult:
    router = pinned(config, name, build)
    results: list[TaskResult] = []
    stopped: str | None = None
    for task in tasks:
        if stopped is not None:
            result = _ungraded(task, "error", f"not run: {stopped}")
        elif task.needs_network and not online:
            result = _ungraded(task, "skipped", "needs the internet, and Enzo is offline")
        else:
            # Each task in a fresh context: the chat and run of the last one must not leak in.
            runner = asyncio.create_task(
                run_task(task, name, router, store, tools, secrets), context=contextvars.Context()
            )
            try:
                result = await runner
            except _UnusableError as exc:
                stopped = redact(str(exc), secrets)
                result = _ungraded(task, "error", stopped)
        results.append(result)
        progress(name, result)
    brain = config.brains[name]
    return BrainResult(
        brain=name,
        provider=brain.provider_name,
        model=brain.model,
        summary=summarize(results),
        tasks=results,
        stopped=stopped,
    )


async def run_task(
    task: EvalTask,
    brain: str,
    router: Router,
    store: Store,
    tools: ToolsFactory,
    secrets: Sequence[str] = (),
) -> TaskResult:
    """One task from empty tables and an empty workspace, graded from what it left behind."""
    await _empty(store.engine)
    with tempfile.TemporaryDirectory(prefix="enzo-eval-") as tmp:
        workspace = Path(tmp) / "Enzo"
        workspace.mkdir()
        for relative, text in task.files.items():
            path = workspace / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        memory = Memory(store.engine, workspace)
        ids: list[int] = []
        for seed in task.memories:
            replaces = ids[seed.replaces] if seed.replaces is not None else None
            ids.append((await memory.remember(seed.text, kind=seed.kind, replaces=replaces)).id)
        schedule = Schedule(store.engine)
        env = TaskEnv(task, workspace, memory, schedule, FinishedTasks())
        service = ConversationService(
            store,
            router,
            ToolRegistry(tools(env)),
            developer=install_note(workspace),
            memory=memory,
            max_steps=task.max_steps,
        )
        run = _Run()
        try:
            async with asyncio.timeout(TASK_TIMEOUT_S):
                await _converse(task, brain, service, store, run)
        except TimeoutError:
            run.failure = f"timed out after {TASK_TIMEOUT_S:.0f} s"
        except _UnusableError:
            raise
        except Exception as exc:  # a harness or store problem: this task errors, the run goes on
            run.failure = f"{type(exc).__name__}: {exc}"
        finally:
            await _settle(service, run.conversation)
        seen = await _observe(store.engine, workspace, task, run)
        return await _grade(task, run, seen, store.engine, secrets)


async def _settle(service: ConversationService, conversation: uuid.UUID | None) -> None:
    """Stop a turn still running (a timeout, a crash) before the next task empties the tables."""
    if conversation is None:
        return
    service.stop(conversation)
    with contextlib.suppress(Exception):
        async with asyncio.timeout(30):
            await service.wait_idle(conversation)


async def _converse(
    task: EvalTask, brain: str, service: ConversationService, store: Store, run: _Run
) -> None:
    conversation: uuid.UUID | None = None
    began = time.perf_counter()
    for turn in task.turns:
        if conversation is None or turn.new_chat:
            conversation = await store.create_conversation(brain=brain)
            run.conversation = conversation
        sent = datetime.now(UTC)
        start = time.perf_counter()
        meta = TurnMeta(timezone=TIMEZONE, device=DEVICE, received_at=sent)
        outcomes: list[TurnOutcome] = []
        events = await service.send(conversation, turn.text, meta)
        for _round in range(MAX_APPROVAL_ROUNDS + 1):
            outcome = await _consume(events, run, began)
            outcomes.append(outcome)
            run.calls.extend(outcome.brain_calls)
            if outcome.failure in FATAL:
                raise _UnusableError(f"{brain} cannot answer: {outcome.text}")
            if outcome.stop != "approval_pending":
                break
            for request in outcome.pending:
                approved = request.tool in task.approve
                await store.decide(conversation, request.id, approved=approved)
            await service.wait_idle(conversation)
            await asyncio.sleep(0)  # let the finished turn release the chat
            events = await service.resume(conversation)
        run.latency_ms += (time.perf_counter() - start) * 1000
        await service.wait_idle(conversation)
        run.turns.append(
            TurnSeen(
                prompt=turn.text,
                reply=outcomes[-1].text,
                stops=tuple(o.stop for o in outcomes),
                steps=sum(o.steps for o in outcomes),
                sent_at=sent,
                timezone=TIMEZONE,
                memories_after=frozenset(await _active_memories(store.engine)),
            )
        )
        run.tool_marks.append(await _last_tool_row(store.engine))


async def _consume(events: AsyncIterator[Any], run: _Run, began: float) -> TurnOutcome:
    done: TurnOutcome | None = None
    async for event in events:
        if isinstance(event, TextDelta) and run.first_word_ms is None and event.text.strip():
            run.first_word_ms = (time.perf_counter() - began) * 1000
        elif isinstance(event, TurnDone):
            done = event.outcome
        elif isinstance(event, TurnFailed):
            msg = f"the turn crashed: {event.message}"
            raise RuntimeError(msg)
    if done is None:
        msg = "the turn ended without an outcome"
        raise RuntimeError(msg)
    return done


# ---------- reading back what a task left ----------


async def _empty(engine: AsyncEngine) -> None:
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    async with engine.begin() as conn:
        await conn.exec_driver_sql(f"TRUNCATE {tables} RESTART IDENTITY CASCADE")


async def _active_memories(engine: AsyncEngine) -> list[int]:
    async with async_sessionmaker(engine)() as session:
        return list(await session.scalars(select(MemoryRow.id).where(MemoryRow.status == "active")))


async def _last_tool_row(engine: AsyncEngine) -> int:
    async with async_sessionmaker(engine)() as session:
        return int(await session.scalar(select(func.coalesce(func.max(ToolCallRow.id), 0))) or 0)


async def _observe(engine: AsyncEngine, workspace: Path, task: EvalTask, run: _Run) -> Observed:
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        tools = list(await session.scalars(select(ToolCallRow).order_by(ToolCallRow.id)))
        approvals = list(
            await session.scalars(select(ApprovalRow).order_by(ApprovalRow.requested_at))
        )
        memories = list(await session.scalars(select(MemoryRow).order_by(MemoryRow.id)))
        jobs = list(await session.scalars(select(JobRow).order_by(JobRow.id)))
    return Observed(
        turns=tuple(run.turns),
        calls=tuple(
            CallSeen(
                tool=t.tool,
                arguments=_json_object(t.arguments_json),
                status=t.status,
                result=t.result,
                started_at=t.created_at,
                turn=next(
                    (i for i, mark in enumerate(run.tool_marks) if t.id <= mark), len(run.turns) - 1
                ),
            )
            for t in tools
        ),
        approvals=tuple(
            ApprovalSeen(a.tool, a.arguments, a.approved, a.summary) for a in approvals
        ),
        memories=tuple(
            MemorySeen(m.id, m.text, m.kind, m.status, m.said_by, m.replaces) for m in memories
        ),
        jobs=tuple(
            JobSeen(
                j.id,
                j.kind,
                j.text,
                j.status,
                j.first_at,
                j.timezone,
                j.repeat,
                j.max_runs,
                j.ends_on,
            )
            for j in jobs
        ),
        files=_files(workspace),
        trash=_trash(workspace),
        seeded=dict(task.files),
    )


def _json_object(raw: str) -> dict[str, Any] | None:
    try:
        value = json.loads(raw or "{}")
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _files(workspace: Path) -> dict[str, str]:
    out = {}
    for path in sorted(workspace.rglob("*")):
        relative = path.relative_to(workspace)
        if path.is_file() and relative.parts[0] != HIDDEN:
            out[relative.as_posix()] = path.read_text(encoding="utf-8", errors="replace")
    return out


def _trash(workspace: Path) -> tuple[str, ...]:
    """Trashed files keep their path under ``.enzo/trash/<when>/``."""
    trash = workspace / HIDDEN / "trash"
    if not trash.is_dir():
        return ()
    return tuple(
        Path(*p.relative_to(trash).parts[1:]).as_posix()
        for p in sorted(trash.rglob("*"))
        if p.is_file()
    )


# ---------- grading ----------


async def _grade(
    task: EvalTask, run: _Run, seen: Observed, engine: AsyncEngine, secrets: Sequence[str]
) -> TaskResult:
    def clean(text: str) -> str:
        return redact(text, secrets)

    verdicts = all_checks(task.checks, seen)
    failed = [v.note for _, v in verdicts if not v.ok]
    if run.failure is not None:
        status, reason = "error", clean(run.failure)
    elif failed:
        status, reason = "failed", clean("; ".join(failed))
    else:
        status, reason = "passed", "all checks passed"
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        rows = list(await session.scalars(select(BrainCallRow).order_by(BrainCallRow.started_at)))
        tool_rows = list(await session.scalars(select(ToolCallRow).order_by(ToolCallRow.id)))
        approvals = {
            a.call_id: a
            for a in await session.scalars(select(ApprovalRow))
            if a.call_id is not None
        }
    run_of = {r.started_at: r.turn_id for r in rows}
    runs = list(dict.fromkeys(r.turn_id for r in rows if r.turn_id))
    calls = run.calls
    plan = any(c.billing == "plan" for c in calls)
    usage = [c for c in calls if c.billing != "plan"]
    cost = (
        None
        if not usage or any(c.cost_usd is None for c in usage)
        else sum(c.cost_usd or 0.0 for c in usage)
    )
    invalid = ("invalid_arguments", "unknown_tool")
    return TaskResult(
        id=task.id,
        title=task.title,
        category=task.category,
        needs_network=task.needs_network,
        expected=task.expected,
        status=status,
        reason=reason,
        actual=clean("; ".join(v.note for _, v in verdicts)),
        checks=[CheckResult(check=c.spec, ok=v.ok, note=clean(v.note)) for c, v in verdicts],
        reply=clean(plain_words(seen.reply, REPLY_CHARS)) if seen.turns else None,
        stops=[s for t in run.turns for s in t.stops],
        first_token_ms=run.first_word_ms,
        latency_ms=run.latency_ms if run.turns else None,
        input_tokens=sum(c.input_tokens for c in calls),
        output_tokens=sum(c.output_tokens for c in calls),
        cost_usd=cost,
        plan=plan,
        valid_calls=sum(r.status not in invalid for r in tool_rows),
        runs=runs,
        brain_calls=[
            brain_step(
                c, run_of.get(c.started_at), first_token_ms=c.first_token_ms, secrets=secrets
            )
            for c in calls
        ],
        tool_calls=[tool_step(r, approvals.get(r.call_id), secrets=secrets) for r in tool_rows],
    )


def _ungraded(task: EvalTask, status: Any, reason: str) -> TaskResult:  # noqa: ANN401
    return TaskResult(
        id=task.id,
        title=task.title,
        category=task.category,
        needs_network=task.needs_network,
        expected=task.expected,
        status=status,
        reason=reason,
        actual=reason,
    )
