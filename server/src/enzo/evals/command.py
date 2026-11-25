"""`enzo eval`: run the 20 tasks on the user's connected models, save the results, compare runs.

    enzo eval                      each connected account's everyday model
    enzo eval --brain gpt-sol      one model (repeat --brain for more; --brain all for every one)
    enzo eval --task memory        only some tasks (an id, a prefix, or a category)
    enzo eval --offline            skip the tasks that need the internet
    enzo eval --compare A B        before/after per task; exits 1 if the pass rate dropped
    enzo eval --runs               the saved runs

Results go to ``~/.local/share/enzo/evals/<run>.json`` and ``.md`` (the folder is the user's
only). The run's own log is ``<run>.log`` there, never mixed into the gateway's.
"""

import asyncio
import os
from collections.abc import Callable, Sequence
from pathlib import Path

import httpx2

from enzo import bundled_db, logs
from enzo.accounts.chatgpt import host_id
from enzo.brain.config import TIERS, BrainConfig, BrainsConfig, load_config
from enzo.brain.factory import build_brain, has_key
from enzo.config import Settings
from enzo.evals.harness import run_suite, standard_tools
from enzo.evals.results import (
    BrainResult,
    RunResult,
    TaskResult,
    compare,
    money,
    pair,
    render_comparison,
    save,
    seconds,
)
from enzo.evals.suite import CATEGORIES, EvalTask, select
from enzo.gateway.connections import build_accounts
from enzo.paths import data_dir

OK, BAD, NOTE = "✓", "✗", "·"
#: Hosts the tasks that need the internet use: web search, and the page one task reads.
PROBES = ("https://api.tavily.com/", "https://example.com/")


def results_dir() -> Path:
    return data_dir() / "evals"


def run(brains: Sequence[str], tasks: Sequence[str], *, offline: bool) -> int:
    settings = Settings()
    config = load_config(settings.brains_config)
    try:
        chosen = select(tasks)
    except KeyError as exc:
        print(f"{BAD} {exc.args[0]}")
        return 1
    folder = results_dir()
    run_id = new_run_id(folder)
    logs.configure(file=folder / f"{run_id}.log", stream=None)
    url = bundled_db.url_for_command(data_dir(), create=True)
    assert url is not None  # noqa: S101 - create=True always gives one
    return asyncio.run(_run(settings, config, brains, chosen, url, run_id, offline=offline))


async def _run(
    settings: Settings,
    config: BrainsConfig,
    wanted: Sequence[str],
    tasks: Sequence[EvalTask],
    url: str,
    run_id: str,
    *,
    offline: bool,
) -> int:
    async with httpx2.AsyncClient(timeout=httpx2.Timeout(20.0), follow_redirects=False) as http:
        accounts = build_accounts(settings, config)
        if accounts.chatgpt is None:
            accounts.enable_chatgpt(host_id(settings.config_dir), http)
        env = os.environ if settings.use_env_keys else None  # opt-in only (#24)

        def usable(c: BrainConfig) -> bool:
            return accounts.usable(c) or has_key(c, env)

        try:
            names = choose(config, wanted, usable)
        except LookupError as exc:
            print(f"{BAD} {exc.args[0]}")
            return 1
        providers = {c.provider_name for c in config.brains.values()} | {"tavily", "github"}
        secrets = [key for p in sorted(providers) if (key := accounts.api_key(p))]
        online = False if offline else await reachable(http)
        shown = ", ".join(
            f"{n} ({config.brains[n].provider_name}/{config.brains[n].model})" for n in names
        )
        where = "online" if online else "offline: the tasks that need the internet are skipped"
        print(f"Running {len(tasks)} tasks on {shown} · {where}")
        print(f"{NOTE} Throwaway workspace and database: your chats, memory and files are safe.")
        result = await run_suite(
            names,
            config=config,
            build=lambda c: build_brain(c, env, keys=accounts.auth),
            database_url=url,
            tools=standard_tools(http, lambda: accounts.api_key("tavily")),
            tasks=tasks,
            online=online,
            secrets=secrets,
            progress=show_progress,
            run_id=run_id,
        )
    json_path, md_path = save(result, results_dir())
    print()
    for brain in result.brains:
        print(summary_line(brain))
    print(f"\nResults: {md_path}\n         {json_path}")
    print(f"Compare with an earlier run: enzo eval --compare previous {result.run_id}")
    return 0


def choose(
    config: BrainsConfig, wanted: Sequence[str], usable: Callable[[BrainConfig], bool]
) -> list[str]:
    """The brains to test: named ones, ``all`` connected ones, or by default each connected
    provider's everyday brain (its recommended one of the default tier)."""
    connected = [n for n, c in config.brains.items() if usable(c)]
    if not connected:
        providers = list(dict.fromkeys(c.provider_name for c in config.brains.values()))
        logins = ", ".join(f"/login {p}" for p in providers)
        msg = f"No model account is connected, so there is nothing to test. Connect one: {logins}"
        raise LookupError(msg)
    if "all" in wanted:
        return connected
    if wanted:
        unknown = [n for n in wanted if n not in config.brains]
        if unknown:
            msg = f"No model named {', '.join(unknown)}. Models: {', '.join(config.brains)}"
            raise LookupError(msg)
        off = [n for n in wanted if n not in connected]
        if off:
            how = ", ".join(f"/login {config.brains[n].provider_name}" for n in off)
            msg = f"{', '.join(off)} cannot be used: its account is not connected ({how})"
            raise LookupError(msg)
        return list(dict.fromkeys(wanted))
    tier = config.routing.default
    order = [tier, *[t for t in reversed(TIERS) if t != tier]]  # then stronger before weaker
    out = []
    for provider in dict.fromkeys(config.brains[n].provider_name for n in connected):
        own = [n for n in connected if config.brains[n].provider_name == provider]
        ranked = sorted(
            own,
            key=lambda n: (order.index(config.brains[n].tier), not config.brains[n].recommended),
        )
        out.append(ranked[0])
    return out


async def reachable(http: httpx2.AsyncClient) -> bool:
    """Whether the hosts the internet tasks need answer at all (any HTTP status counts)."""

    async def answers(url: str) -> bool:
        try:
            await http.head(url, timeout=5.0)
        except httpx2.HTTPError:
            return False
        return True

    return all(await asyncio.gather(*(answers(u) for u in PROBES)))


def show_progress(brain: str, result: TaskResult) -> None:
    if result.status == "passed":
        print(f"  {OK} {result.id}  {seconds(result.latency_ms)} · {_cost(result)}", flush=True)
    elif result.status == "skipped":
        print(f"  {NOTE} {result.id} skipped: {result.reason}", flush=True)
    else:
        mark = BAD if result.status == "failed" else "!"
        print(f"  {mark} {result.id}  {result.reason}", flush=True)


def _cost(result: TaskResult) -> str:
    return money(result.cost_usd, result.plan, result)


def summary_line(brain: BrainResult) -> str:
    s = brain.summary
    graded = s.passed + s.failed + s.errors
    rate = "—" if s.pass_rate is None else f"{s.pass_rate:.0%}"
    cats = ", ".join(
        f"{CATEGORIES.get(c, c).lower()} {p}/{g}"  # type: ignore[call-overload]
        for c, (p, g) in s.by_category.items()
    )
    stopped = f"\n  {BAD} stopped early: {brain.stopped}" if brain.stopped else ""
    return (
        f"{brain.brain}: {s.passed}/{graded} passed ({rate}) · {cats} · valid tool calls "
        f"{s.valid_calls}/{s.tool_calls} · {seconds(s.latency_ms)} · cost "
        f"{money(s.cost_usd, s.plan)}{stopped}"
    )


# ---------- saved runs ----------


def new_run_id(folder: Path) -> str:
    from datetime import UTC, datetime  # noqa: PLC0415

    base = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_id, n = base, 1
    while (folder / f"{run_id}.json").exists() or (folder / f"{run_id}.log").exists():
        n += 1
        run_id = f"{base}-{n}"
    return run_id


def saved(folder: Path | None = None) -> list[Path]:
    folder = folder or results_dir()
    return sorted(folder.glob("*.json")) if folder.is_dir() else []


def load(ref: str, folder: Path | None = None) -> RunResult:
    """A run by path, by id, or ``last`` / ``previous``."""
    folder = folder or results_dir()
    runs = saved(folder)
    if ref in ("last", "previous"):
        index = -1 if ref == "last" else -2
        if len(runs) < -index:
            msg = f"There is no {ref} run yet (saved runs: {len(runs)})."
            raise LookupError(msg)
        path = runs[index]
    else:
        path = Path(ref) if ref.endswith(".json") else folder / f"{ref}.json"
    if not path.is_file():
        msg = f"No saved run {ref}. See them with: enzo eval --runs"
        raise LookupError(msg)
    return RunResult.model_validate_json(path.read_text(encoding="utf-8"))


def compare_runs(before: str, after: str, folder: Path | None = None) -> int:
    """Before/after per task. 1 when the pass rate dropped on any brain (§13.2), else 0."""
    try:
        a, b = load(before, folder), load(after, folder)
    except LookupError as exc:
        print(f"{BAD} {exc.args[0]}")
        return 1
    pairs = pair(a, b)
    if not pairs:
        print(f"{BAD} The two runs tested no model in common: compare runs of the same model.")
        return 1
    dropped = False
    for old, new in pairs:
        result = compare(a, b, old, new)
        print(render_comparison(result))
        dropped = dropped or result.dropped
    if dropped:
        print(
            f"\n{BAD} The pass rate dropped. Find out why before shipping this change (spec §13.2)."
        )
        return 1
    print(f"\n{OK} No drop in the pass rate.")
    return 0


def list_runs(folder: Path | None = None) -> int:
    runs = saved(folder)
    if not runs:
        print(f"{NOTE} No saved runs yet: run `enzo eval`.")
        return 0
    for path in runs:
        try:
            r = RunResult.model_validate_json(path.read_text(encoding="utf-8"))
        except ValueError:
            print(f"  {path.stem}  (unreadable)")
            continue
        brains = ", ".join(
            f"{b.brain} {b.summary.passed}/{b.summary.passed + b.summary.failed + b.summary.errors}"
            for b in r.brains
        )
        print(f"  {r.run_id}  {r.suite} v{r.suite_version} · {brains}")
    return 0


def main(
    *,
    brains: Sequence[str],
    tasks: Sequence[str],
    offline: bool,
    compare_with: Sequence[str] | None,
    runs: bool,
) -> int:
    if runs:
        return list_runs()
    if compare_with:
        before, after = compare_with
        return compare_runs(before, after)
    return run(brains, tasks, offline=offline)
