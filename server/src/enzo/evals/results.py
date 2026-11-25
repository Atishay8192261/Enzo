"""Eval results: per task and per brain, saved as JSON plus a Markdown table (spec §15.5), and
compared run against run (§13.2: "a drop in pass rate must block the change").

Cost is never invented: a call with no published price makes the total unknown (``null``), and a
call paid by the user's ChatGPT plan is marked as such instead of priced (§1.3.5).
"""

import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from enzo.evals.suite import CATEGORIES
from enzo.tracing import BrainStep, ToolStep

SCHEMA = 1
type Status = Literal["passed", "failed", "skipped", "error"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CheckResult(_Model):
    check: str
    ok: bool
    note: str


class TaskResult(_Model):
    id: str
    title: str
    category: str
    needs_network: bool
    expected: str
    status: Status
    #: Why it failed (the failing checks), was skipped, or errored; "all checks passed" otherwise.
    reason: str
    #: What was seen, in a few words (the "actual outcome" column).
    actual: str
    checks: list[CheckResult] = []
    #: The last reply, cut short.
    reply: str | None = None
    stops: list[str] = []
    #: From sending the first message to the first word of a reply on screen.
    first_token_ms: float | None = None
    #: From sending the first message to the end of the last reply (approvals included).
    latency_ms: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    #: None: unknown (a call with no published price); plan-paid calls are not priced.
    cost_usd: float | None = None
    plan: bool = False
    #: Tool calls that were valid on the first try (§13.2 tool accuracy), out of ``tool_calls``.
    valid_calls: int = 0
    runs: list[str] = []
    brain_calls: list[BrainStep] = []
    tool_calls: list[ToolStep] = []

    @property
    def graded(self) -> bool:
        return self.status != "skipped"


class Summary(_Model):
    passed: int
    failed: int
    errors: int
    skipped: int
    #: passed / (passed + failed + errors); None when nothing was graded.
    pass_rate: float | None
    by_category: dict[str, list[int]]  # category -> [passed, graded]
    tool_calls: int
    valid_calls: int
    latency_ms: float
    median_latency_ms: float | None
    input_tokens: int
    output_tokens: int
    cost_usd: float | None
    plan: bool


class BrainResult(_Model):
    brain: str
    provider: str
    model: str
    summary: Summary
    tasks: list[TaskResult]
    #: Set when the run on this brain stopped early (e.g. the key was rejected).
    stopped: str | None = None


class RunResult(_Model):
    schema_version: int = SCHEMA
    run_id: str
    suite: str
    suite_version: str
    fingerprint: str
    enzo_version: str
    started_at: datetime
    finished_at: datetime
    online: bool
    timezone: str
    brains: list[BrainResult]


def summarize(tasks: Sequence[TaskResult]) -> Summary:
    graded = [t for t in tasks if t.graded]
    passed = sum(t.status == "passed" for t in graded)
    by_category: dict[str, list[int]] = {}
    for t in graded:
        row = by_category.setdefault(t.category, [0, 0])
        row[0] += t.status == "passed"
        row[1] += 1
    latencies = [t.latency_ms for t in graded if t.latency_ms is not None]
    return Summary(
        passed=passed,
        failed=sum(t.status == "failed" for t in graded),
        errors=sum(t.status == "error" for t in graded),
        skipped=len(tasks) - len(graded),
        pass_rate=passed / len(graded) if graded else None,
        by_category=by_category,
        tool_calls=sum(len(t.tool_calls) for t in graded),
        valid_calls=sum(t.valid_calls for t in graded),
        latency_ms=sum(latencies),
        median_latency_ms=statistics.median(latencies) if latencies else None,
        input_tokens=sum(t.input_tokens for t in graded),
        output_tokens=sum(t.output_tokens for t in graded),
        cost_usd=total_cost(graded),
        plan=any(t.plan for t in graded),
    )


def total_cost(tasks: Iterable[TaskResult]) -> float | None:
    """The tasks' per-token cost; None when any of them is unknown, or all were plan-paid."""
    spent = [t for t in tasks if t.input_tokens or t.output_tokens]
    priced = [t for t in spent if not t.plan]
    if not priced or any(t.cost_usd is None for t in priced):
        return None
    return sum(t.cost_usd or 0.0 for t in priced)


# ---------- Markdown (spec §15.5: task, expected, actual, pass/fail, latency, cost) ----------

_MARK = {"passed": "✅ pass", "failed": "❌ fail", "skipped": "⏭ skipped", "error": "⚠️ error"}


def markdown(run: RunResult) -> str:
    out = [
        f"# Enzo eval: {run.suite} v{run.suite_version}",
        "",
        f"Run `{run.run_id}` · Enzo {run.enzo_version} · "
        f"{run.started_at:%Y-%m-%d %H:%M} UTC · {'online' if run.online else 'offline'} · "
        f"suite fingerprint `{run.fingerprint}`",
        "",
    ]
    if len(run.brains) > 1:
        out += [
            "## Brains side by side",
            "",
            "| Brain | Passed | Pass rate | Valid tool calls | Median latency | Cost |",
            "|---|---|---|---|---|---|",
        ]
        for b in run.brains:
            s = b.summary
            graded = s.passed + s.failed + s.errors
            out.append(
                f"| {b.brain} ({b.provider}/{b.model}) | {s.passed}/{graded}"
                f" | {_rate(s.pass_rate)} | {s.valid_calls}/{s.tool_calls} | "
                f"{seconds(s.median_latency_ms)} | {money(s.cost_usd, s.plan)} |"
            )
        out.append("")
    for b in run.brains:
        s = b.summary
        graded = s.passed + s.failed + s.errors
        title = f"{b.brain} ({b.provider}/{b.model})"
        out += [f"## {title}: {s.passed}/{graded} passed ({_rate(s.pass_rate)})", ""]
        if b.stopped:
            out += [f"**Stopped early:** {_cell(b.stopped)}", ""]
        cats = " · ".join(
            f"{CATEGORIES.get(c, c)} {p}/{g}"  # type: ignore[call-overload]
            for c, (p, g) in s.by_category.items()
        )
        out += [
            f"{cats} · valid tool calls on the first try {s.valid_calls}/{s.tool_calls}"
            + (f" · skipped {s.skipped} (offline)" if s.skipped else ""),
            "",
            f"Total: {seconds(s.latency_ms)} · {s.input_tokens:,} tokens in / "
            f"{s.output_tokens:,} out · cost {money(s.cost_usd, s.plan)}",
            "",
            "| Task | Expected | Actual | Result | First token | Latency | Tokens in/out | Cost |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for t in b.tasks:
            actual = t.actual if t.status in ("passed", "skipped") else t.reason
            out.append(
                f"| `{t.id}` {_cell(t.title)} | {_cell(t.expected)} | {_cell(actual)} | "
                f"{_MARK[t.status]} | {seconds(t.first_token_ms)} | {seconds(t.latency_ms)} | "
                f"{t.input_tokens:,} / {t.output_tokens:,} | {money(t.cost_usd, t.plan, t)} |"
            )
        out.append("")
    return "\n".join(out)


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def _rate(rate: float | None) -> str:
    return "—" if rate is None else f"{rate:.0%}"


def seconds(ms: float | None) -> str:
    if ms is None:
        return "—"
    return f"{ms:.0f} ms" if ms < 1000 else f"{ms / 1000:.1f} s"


def money(cost: float | None, plan: bool, task: TaskResult | None = None) -> str:
    if task is not None and not (task.input_tokens or task.output_tokens):
        return "—"
    if cost is None:
        return "your plan" if plan else "unknown"
    shown = f"${cost:.4f}"
    return f"{shown} + your plan" if plan else shown


# ---------- before / after ----------


@dataclass(frozen=True, slots=True)
class Change:
    task: str
    before: Status
    after: Status
    reason: str


@dataclass(frozen=True, slots=True)
class Comparison:
    before: str
    after: str
    #: Tasks graded in both runs.
    common: int
    passed_before: int
    passed_after: int
    changes: list[Change]
    latency_before: float
    latency_after: float
    cost_before: float | None
    cost_after: float | None
    plan_before: bool
    plan_after: bool
    suite_changed: bool

    @property
    def dropped(self) -> bool:
        return self.passed_after < self.passed_before


def pair(a: RunResult, b: RunResult) -> list[tuple[BrainResult, BrainResult]]:
    """Same brain in both runs; or, when each ran one brain, those two (a brain swap)."""
    if len(a.brains) == 1 and len(b.brains) == 1:
        return [(a.brains[0], b.brains[0])]
    after = {x.brain: x for x in b.brains}
    return [(x, after[x.brain]) for x in a.brains if x.brain in after]


def compare(a: RunResult, b: RunResult, before: BrainResult, after: BrainResult) -> Comparison:
    old = {t.id: t for t in before.tasks if t.graded}
    new = {t.id: t for t in after.tasks if t.graded}
    common = [i for i in old if i in new]
    changes = [
        Change(i, old[i].status, new[i].status, new[i].reason)
        for i in common
        if (old[i].status == "passed") != (new[i].status == "passed")
    ]
    return Comparison(
        before=f"{a.run_id} {before.brain}",
        after=f"{b.run_id} {after.brain}",
        common=len(common),
        passed_before=sum(old[i].status == "passed" for i in common),
        passed_after=sum(new[i].status == "passed" for i in common),
        changes=changes,
        latency_before=sum(old[i].latency_ms or 0.0 for i in common),
        latency_after=sum(new[i].latency_ms or 0.0 for i in common),
        cost_before=total_cost(old[i] for i in common),
        cost_after=total_cost(new[i] for i in common),
        plan_before=any(old[i].plan for i in common),
        plan_after=any(new[i].plan for i in common),
        suite_changed=a.fingerprint != b.fingerprint,
    )


def render_comparison(c: Comparison) -> str:
    rate_before = c.passed_before / c.common if c.common else 0.0
    rate_after = c.passed_after / c.common if c.common else 0.0
    verdict = (
        "▼ dropped" if c.dropped else ("▲ rose" if c.passed_after > c.passed_before else "= same")
    )
    out = [f"{c.before}  →  {c.after}"]
    if c.suite_changed:
        out.append("! The task set changed between these runs: only tasks in both are compared.")
    out.append(
        f"Pass rate on the {c.common} tasks graded in both: "
        f"{c.passed_before}/{c.common} ({rate_before:.0%}) → {c.passed_after}/{c.common} "
        f"({rate_after:.0%})  {verdict}"
    )
    for ch in c.changes:
        arrow = "✓→✗" if ch.before == "passed" else "✗→✓"
        why = f"  ({ch.reason})" if ch.after != "passed" else ""
        out.append(f"  {arrow} {ch.task}{why}")
    out.append(
        f"Latency {seconds(c.latency_before)} → {seconds(c.latency_after)} · "
        f"cost {money(c.cost_before, c.plan_before)} → {money(c.cost_after, c.plan_after)}"
    )
    return "\n".join(out)


def save(run: RunResult, folder: Path) -> tuple[Path, Path]:
    """``<run>.json`` and ``<run>.md``, readable by the user only."""
    from enzo.paths import private_dir  # noqa: PLC0415

    private_dir(folder)
    paths = folder / f"{run.run_id}.json", folder / f"{run.run_id}.md"
    for path, text in zip(paths, (run.model_dump_json(indent=2), markdown(run)), strict=True):
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text + "\n", encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(path)
    return paths
