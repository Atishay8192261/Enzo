"""`enzo eval` as a command: which brains, the saved runs, before/after, and a whole run on a
mock OpenAI-compatible provider through the real adapter (no keys, no network)."""

import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2
import openai
import pytest

from enzo import cli, logs
from enzo.accounts.vault import generate_key
from enzo.brain.config import BrainsConfig
from enzo.evals import command
from enzo.evals.results import BrainResult, RunResult, TaskResult, save, summarize
from enzo.evals.suite import SUITE
from tests.brain.test_openai_brain import response, sse
from tests.fakes import fake_key


def catalog() -> BrainsConfig:
    def brain(provider: str, tier: str, **extra: Any) -> dict[str, Any]:
        api = "anthropic" if provider == "anthropic" else "openai-responses"
        return {
            "api": api,
            "provider": provider,
            "model": f"{provider}-{tier}",
            "api_key_env": "K",
            "tier": tier,
            **extra,
        }

    return BrainsConfig.model_validate(
        {
            "brains": {
                "a-top": brain("anthropic", "powerful", recommended=True),
                "a-mid": brain("anthropic", "balanced", recommended=True),
                "a-low": brain("anthropic", "fast"),
                "o-top": brain("openai", "powerful", recommended=True),
                "o-low": brain("openai", "fast"),
            }
        }
    )


def test_by_default_each_account_is_tested_on_its_everyday_brain() -> None:
    config = catalog()
    assert command.choose(config, [], lambda _c: True) == [
        "a-mid",
        "o-top",
    ]  # no balanced: stronger
    assert command.choose(config, ["all"], lambda _c: True) == list(config.brains)
    assert command.choose(config, ["o-low", "o-low"], lambda _c: True) == ["o-low"]
    only_openai = command.choose(config, [], lambda c: c.provider_name == "openai")
    assert only_openai == ["o-top"]


@pytest.mark.parametrize(
    ("wanted", "usable", "error"),
    [
        ([], False, "No model account is connected"),
        (["ghost"], True, "No model named ghost"),
        (
            ["a-mid"],
            "openai",
            "a-mid cannot be used: its account is not connected (/login anthropic)",
        ),
    ],
)
def test_a_brain_that_cannot_be_tested_says_why(
    wanted: list[str], usable: object, error: str
) -> None:
    def ok(c: Any) -> bool:
        return usable is True or c.provider_name == usable

    with pytest.raises(LookupError, match=error.replace("(", r"\(").replace(")", r"\)")):
        command.choose(catalog(), wanted, ok)


def a_run(run_id: str, passed: set[str], brain: str = "b") -> RunResult:
    tasks = [
        TaskResult(
            id=t.id,
            title=t.title,
            category=t.category,
            needs_network=t.needs_network,
            expected=t.expected,
            status="passed" if t.id in passed else "failed",
            reason="all checks passed" if t.id in passed else f"{t.id} went wrong",
            actual="…",
            latency_ms=1000.0,
            input_tokens=10,
            output_tokens=5,
            cost_usd=0.001,
        )
        for t in SUITE
    ]
    now = datetime.now(UTC)
    return RunResult(
        run_id=run_id,
        suite="enzo-core",
        suite_version="1",
        fingerprint="f",
        enzo_version="0",
        started_at=now,
        finished_at=now,
        online=True,
        timezone="UTC",
        brains=[
            BrainResult(brain=brain, provider="p", model="m", summary=summarize(tasks), tasks=tasks)
        ],
    )


ALL = {t.id for t in SUITE}


def test_a_drop_in_the_pass_rate_exits_non_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Spec §13.2: a drop in pass rate must block the change until it is understood."""
    monkeypatch.setattr(command, "results_dir", lambda: tmp_path)
    save(a_run("20261125T100000Z", ALL), tmp_path)
    save(a_run("20261125T110000Z", ALL - {"safety.injection_file"}), tmp_path)
    assert cli.main(["eval", "--compare", "previous", "last"]) == 1
    out = capsys.readouterr().out
    assert "20/20 (100%) → 19/20 (95%)  ▼ dropped" in out
    assert "✓→✗ safety.injection_file  (safety.injection_file went wrong)" in out
    assert "The pass rate dropped" in out
    # The other way round it rose: no block.
    assert (
        cli.main(["eval", "--compare", "20261125T110000Z", str(tmp_path / "20261125T100000Z.json")])
        == 0
    )
    out = capsys.readouterr().out
    assert "✗→✓ safety.injection_file" in out
    assert "No drop in the pass rate" in out


def test_a_brain_swap_is_compared_task_by_task(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """§15.5: "run it before and after a brain swap"."""
    save(a_run("r1", ALL, brain="claude-sonnet"), tmp_path)
    save(a_run("r2", ALL, brain="gpt-sol"), tmp_path)
    assert command.compare_runs("r1", "r2", tmp_path) == 0
    assert "r1 claude-sonnet  →  r2 gpt-sol" in capsys.readouterr().out


def test_skipped_tasks_do_not_count_as_a_drop(tmp_path: Path) -> None:
    online = a_run("r1", ALL)
    offline = a_run("r2", ALL)
    for t in offline.brains[0].tasks:
        if t.needs_network:
            t.status = "skipped"
    save(online, tmp_path)
    save(offline, tmp_path)
    assert command.compare_runs("r1", "r2", tmp_path) == 0


def test_only_tasks_graded_in_both_runs_are_compared(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An offline run, then an online one whose internet tasks fail: 16 compared, no drop."""
    offline = a_run("r1", ALL)
    for t in offline.brains[0].tasks:
        if t.needs_network:
            t.status = "skipped"
    online = a_run("r2", {t.id for t in SUITE if not t.needs_network})
    save(offline, tmp_path)
    save(online, tmp_path)
    assert command.compare_runs("r1", "r2", tmp_path) == 0
    assert "on the 16 tasks graded in both: 16/16 (100%) → 16/16" in capsys.readouterr().out


def test_saved_runs_are_listed_and_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(command, "results_dir", lambda: tmp_path / "evals")
    assert cli.main(["eval", "--runs"]) == 0
    assert "No saved runs yet" in capsys.readouterr().out
    json_path, md_path = save(a_run("r1", ALL - {"files.create"}), tmp_path / "evals")
    assert cli.main(["eval", "--runs"]) == 0
    assert "r1  enzo-core v1 · b 19/20" in capsys.readouterr().out
    assert (tmp_path / "evals").stat().st_mode & 0o777 == 0o700
    assert json_path.stat().st_mode & 0o777 == 0o600
    assert md_path.stat().st_mode & 0o777 == 0o600
    assert cli.main(["eval", "--compare", "r1", "nope"]) == 1
    assert "No saved run nope" in capsys.readouterr().out


# ---------- a whole `enzo eval` on a mock provider ----------

KEY = fake_key("mock")
CREATE = {
    "id": "fc_1",
    "type": "function_call",
    "call_id": "call_1",
    "name": "create_file",
    "arguments": json.dumps({"path": "shopping.md", "content": "eggs\nmilk\nbread\n"}),
    "status": "completed",
}
SAVED = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "status": "completed",
    "content": [{"type": "output_text", "text": "Saved| to shopping.md.", "annotations": []}],
}


def mock_responses_api(request: httpx2.Request) -> httpx2.Response:
    """Creates the file, then says so: like a model would for files.create."""
    if request.headers["authorization"] != f"Bearer {KEY}":
        return httpx2.Response(401, json={"error": {"message": "Incorrect API key provided"}})
    items = json.loads(request.content)["input"]
    answered = isinstance(items[-1], dict) and items[-1].get("type") == "function_call_output"
    body = sse(response([SAVED] if answered else [CREATE]))
    return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=body)


@pytest.fixture
def quiet_logs() -> Iterator[None]:
    """`enzo eval` writes its own log file; give pytest its logging back afterwards."""
    yield
    for name in logs.LOGGERS:
        logger = logging.getLogger(name)
        for handler in [h for h in logger.handlers if getattr(h, "_enzo", False)]:
            logger.removeHandler(handler)
            handler.close()
        logger.setLevel(logging.NOTSET)
        logger.propagate = True


def test_enzo_eval_runs_a_real_adapter_against_a_mock_provider(
    database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    quiet_logs: None,
) -> None:
    brains = tmp_path / "brains.toml"
    brains.write_text(
        '[brains.mock]\napi = "openai-responses"\nprovider = "localmock"\n'
        'base_url = "http://mock.invalid/v1"\nmodel = "mock-1"\napi_key_env = "LOCALMOCK_KEY"\n'
        'tier = "balanced"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("ENZO_BRAINS_CONFIG", str(brains))
    monkeypatch.setenv("ENZO_USE_ENV_KEYS", "1")
    monkeypatch.setenv("LOCALMOCK_KEY", KEY)
    monkeypatch.setenv("ENZO_DATABASE_URL", database_url)
    monkeypatch.setenv("ENZO_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ENZO_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ENZO_VAULT_KEY", generate_key())
    monkeypatch.setattr(
        "enzo.brain.factory.brain_http_client",
        lambda: openai.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(mock_responses_api)),
    )
    assert cli.main(["eval", "--task", "files.create", "--task", "files.edit", "--offline"]) == 0
    out = capsys.readouterr().out
    assert "Running 2 tasks on mock (localmock/mock-1) · offline" in out
    assert "  ✓ files.create" in out
    assert "  ✗ files.edit" in out  # the mock only ever creates shopping.md
    assert "mock: 1/2 passed (50%)" in out
    folder = tmp_path / "data" / "evals"
    (saved,) = folder.glob("*.json")
    result = RunResult.model_validate_json(saved.read_text(encoding="utf-8"))
    (create, edit) = result.brains[0].tasks
    assert create.first_token_ms is not None
    assert create.cost_usd is None  # the mock has no published price: unknown, not 0
    assert "edit_file never ran" in edit.reason or "never used edit_file" in edit.reason
    for written in folder.iterdir():  # results and the run's own log
        assert KEY not in written.read_text(encoding="utf-8")
    log = next(folder.glob("*.log")).read_text(encoding="utf-8")
    assert '"event": "brain.call"' in log
    assert '"ttft_ms"' in log
