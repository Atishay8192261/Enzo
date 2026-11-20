"""Enzo's log (#48): one JSON line per step, tied to its chat and run, written to a rotated file
that `enzo logs` reads back. Lines name what happened, never what was said (spec §11.3)."""

import io
import json
import logging
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from enzo import cli, logs
from enzo.brain import Message, ToolCall
from enzo.brain.fake import ScriptedBrain
from enzo.store.repository import Store
from tests.gateway.test_api import client, new_conversation, service


def reset() -> None:
    """Undo `logs.configure`, so other tests see the default logging (pytest's caplog)."""
    for name in logs.LOGGERS:
        logger = logging.getLogger(name)
        for handler in [h for h in logger.handlers if getattr(h, "_enzo", False)]:
            logger.removeHandler(handler)
            handler.close()
        logger.setLevel(logging.NOTSET)
        logger.propagate = True


@pytest.fixture
def captured() -> Iterator[io.StringIO]:
    out = io.StringIO()
    logs.configure(stream=out)
    try:
        yield out
    finally:
        reset()


def lines(out: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(raw) for raw in out.getvalue().splitlines()]


async def test_a_turn_is_logged_step_by_step_and_tied_to_its_ledger_rows(
    store: Store, captured: io.StringIO
) -> None:
    step = ToolCall(id="c1", name="step", arguments_json='{"value": "XYZZY plan"}')
    email = ToolCall(id="c2", name="send_email", arguments_json='{"value": "to bob"}')
    brain = ScriptedBrain(
        [
            Message.assistant(tool_calls=(step,)),
            Message.assistant("PLUGH reply"),
            Message.assistant(tool_calls=(email,)),
        ]
    )
    async with client(service(store, {"m-main": brain})) as c:
        cid = await new_conversation(c)
        await c.post(f"/conversations/{cid}/turns", json={"message": "my secret FROTZ"})
        await c.post(f"/conversations/{cid}/turns", json={"message": "email bob"})

    text = captured.getvalue()
    for said in ("FROTZ", "XYZZY", "PLUGH", "to bob"):
        assert said not in text  # no message text, tool arguments or replies
    found = [x for x in lines(captured) if x.get("conversation") == cid]
    events = [x["event"] for x in found]
    assert events == [
        "turn.start",
        "route",
        "brain.call",
        "tool.call",
        "route",
        "brain.call",
        "turn.end",
        "turn.start",
        "route",
        "brain.call",
        "approval.wait",
        "turn.end",
    ]
    first, second = found[0]["turn"], found[7]["turn"]
    assert first != second
    assert all(x["turn"] == first for x in found[:7])
    assert all(x["turn"] == second for x in found[7:])
    # Spec §1.4: provider, model, tokens and latency on one line.
    call = found[2]
    assert (call["provider"], call["model"], call["outcome"]) == ("fake", "scripted-1", "ok")
    assert {"in", "out", "ms", "billing"} <= call.keys()
    assert (found[3]["tool"], found[3]["status"]) == ("step", "ok")
    assert (found[6]["stop"], found[6]["steps"], found[6]["tools"]) == ("final_answer", 2, 1)
    assert found[10]["tool"] == "send_email"
    assert found[11]["stop"] == "approval_pending"

    # The same run id is on the ledger rows: one query joins the log and the database.
    conv = uuid.UUID(cid)
    assert [r.turn_id for r in await store.brain_calls(conv)] == [first, first, second]
    assert [r.turn_id for r in await store.tool_calls(conv)] == [first]


def test_http_lines_keep_the_path_but_never_a_query(captured: io.StringIO) -> None:
    access = logging.getLogger("uvicorn.access")
    line = '%s - "%s %s HTTP/%s" %d'  # how uvicorn logs a request
    access.info(line, "127.0.0.1:5000", "GET", "/auth/callback?code=ONE-TIME&state=s", "1.1", 200)
    access.info(line, "127.0.0.1:5000", "GET", "/healthz", "1.1", 200)  # routine: left out
    access.info(line, "127.0.0.1:5000", "GET", "/api/healthz", "1.1", 200)
    access.info(line, "127.0.0.1:5000", "GET", "/healthz", "1.1", 503)  # a problem: kept
    assert "ONE-TIME" not in captured.getvalue()
    assert [(x["event"], x["path"], x["status"]) for x in lines(captured)] == [
        ("http", "/auth/callback", 200),
        ("http", "/healthz", 503),
    ]
    assert all("logger" not in x and "127.0.0.1" not in str(x) for x in lines(captured))


def test_other_libraries_lines_name_their_logger_and_errors_keep_the_traceback(
    captured: io.StringIO,
) -> None:
    coloured = {"color_message": "Started server process [\x1b[36m%d\x1b[0m]"}  # uvicorn's
    logging.getLogger("uvicorn.error").warning("Application startup failed", extra=coloured)
    try:
        raise ValueError("bad config")
    except ValueError:
        logging.getLogger("enzo.agent.service").exception("turn.failed", extra={"ms": 12})
    warning, failed = lines(captured)
    assert (warning["level"], warning["logger"]) == ("warning", "uvicorn.error")
    assert "color_message" not in warning  # escape codes stay out of the file
    assert (failed["event"], failed["ms"], failed["level"]) == ("turn.failed", 12, "error")
    assert "ValueError: bad config" in str(failed["error"])
    assert "logger" not in failed  # Enzo's own lines need no logger name


def test_configuring_again_replaces_the_handlers_instead_of_doubling_them(
    captured: io.StringIO,
) -> None:
    again = io.StringIO()
    logs.configure(stream=again)
    logging.getLogger("enzo.test").info("once")
    assert captured.getvalue() == ""
    assert [x["event"] for x in lines(again)] == ["once"]


def test_the_file_rotates_and_keeps_three_old_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(logs, "MAX_BYTES", 300)
    path = tmp_path / "logs" / logs.LOG_FILE
    logs.configure(file=path, stream=None)
    try:
        for n in range(40):
            logging.getLogger("enzo.test").info("step", extra={"n": n})
    finally:
        reset()
    assert sorted(p.name for p in path.parent.iterdir()) == [
        "gateway.log",
        "gateway.log.1",
        "gateway.log.2",
        "gateway.log.3",
    ]
    assert path.parent.stat().st_mode & 0o777 == 0o700  # private, like the rest of Enzo's data
    for log in path.parent.iterdir():
        assert log.stat().st_mode & 0o777 == 0o600, log.name  # each file too, after rotations
    assert json.loads(logs.last_lines(path, 1)[0])["n"] == 39


def test_following_survives_a_rotation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(logs, "MAX_BYTES", 400)
    path = tmp_path / logs.LOG_FILE
    logs.configure(file=path, stream=None)
    log = logging.getLogger("enzo.test")
    log.info("before", extra={"n": -1})
    seen: list[int] = []
    done = threading.Event()

    def watch() -> None:
        for raw in logs.follow(path, lines=1, poll_s=0.005, stop=done.is_set):
            seen.append(int(json.loads(raw)["n"]))

    watcher = threading.Thread(target=watch)
    watcher.start()
    try:
        deadline = time.monotonic() + 5
        while not seen and time.monotonic() < deadline:  # it has read the file once
            time.sleep(0.005)
        for n in range(20):  # several rotations
            log.info("step", extra={"n": n})
            time.sleep(0.02)
        deadline = time.monotonic() + 5
        while seen[-1:] != [19] and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        done.set()
        watcher.join(timeout=5)
        reset()
    assert seen == list(range(-1, 20))  # the last line before, then every line, in order


def test_readable_lines_for_people() -> None:
    raw = json.dumps(
        {
            "ts": "2026-11-20T14:23:52.120+00:00",
            "level": "warning",
            "event": "brain.call",
            "conversation": "e74f5a1c-0000-4000-8000-000000000000",
            "turn": "t_9f00aa11bb22",
            "model": "gpt-6.2-sol",
            "in": 1596,
            "error": "ReadTimeout\n\x1b[31mred\x1b[0m line",
            "reason": "two words",
        }
    )
    text = logs.readable(raw)
    head, *rest = text.splitlines()
    assert head.endswith(
        "! brain.call conversation=e74f5a1c turn=t_9f00aa11bb22 model=gpt-6.2-sol in=1596 "
        'reason="two words"'
    )
    assert rest == ["    ReadTimeout", "    [31mred[0m line"]  # escape codes never reach a terminal
    assert "\x1b" not in text
    assert logs.readable("Traceback (most recent call last):\x1b[2J\n") == (
        "Traceback (most recent call last):[2J"
    )


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ENZO_STATE_DIR", str(tmp_path / "state"))
    return tmp_path / "state"


def test_enzo_logs_before_anything_was_logged(
    state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["logs"]) == 0
    expected = state / "logs" / "gateway.log"
    assert capsys.readouterr().out == f"· No log yet: Enzo writes {expected} once it has started.\n"
    assert cli.main(["logs", "--path"]) == 0
    assert capsys.readouterr().out == f"{expected}\n"


def test_enzo_logs_shows_the_last_lines(state: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = state / "logs" / "gateway.log"
    path.parent.mkdir(parents=True)
    rows = [
        {"ts": f"2026-11-20T14:23:5{n}.000+00:00", "level": "info", "event": f"e{n}"}
        for n in range(5)
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    assert cli.main(["logs", "-n", "2"]) == 0
    shown = capsys.readouterr().out.splitlines()
    assert [s.split()[-1] for s in shown] == ["e3", "e4"]
    assert cli.main(["logs", "-n", "1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == rows[-1]  # as written, for a bug report


def test_doctor_says_where_the_log_is(state: Path) -> None:
    assert cli._check_log() == (
        "·",
        f"Log: none yet (Enzo writes {state / 'logs' / 'gateway.log'} once it has started)",
    )
    (state / "logs").mkdir(parents=True)
    (state / "logs" / "gateway.log").write_text("{}\n")
    mark, line = cli._check_log()
    assert mark == "✓"
    assert line.startswith(f"Log: {state / 'logs' / 'gateway.log'} (`enzo logs` shows it")
