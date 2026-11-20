"""``enzo``: the one command an installed Enzo needs (#23, ADR 0016).

enzo           the terminal app, in a new chat (the gateway and its database start with it)
enzo -c        the terminal app, in the last chat (--continue)
enzo web       the web app in your browser
enzo doctor    check this installation, with a fix for each problem
enzo logs      what Enzo did, from its log (-f to keep watching; attach it to a bug report)
enzo update    update to the latest release
enzo serve     run the gateway in the foreground (for an always-on machine)
"""

import argparse
import atexit
import contextlib
import logging
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
import webbrowser
from collections.abc import Callable, Iterator
from importlib import resources
from pathlib import Path

import httpx2

from enzo import __version__, launcher, logs, tui
from enzo.config import Settings
from enzo.paths import data_dir, private_dir, state_dir

INSTALL = "curl -fsSL https://raw.githubusercontent.com/Atishay8192261/Enzo/main/install.sh | sh"
OK, BAD, NOTE = "✓", "✗", "·"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="enzo", description="Enzo, your personal AI agent.")
    parser.add_argument("--version", action="version", version=f"enzo {__version__}")
    parser.add_argument(
        "-c", "--continue", dest="last", action="store_true", help="reopen your last chat"
    )
    commands = parser.add_subparsers(dest="command", metavar="command")
    chat_parser = commands.add_parser("chat", help="open the terminal app (what `enzo` alone does)")
    chat_parser.add_argument(
        "-c", "--continue", dest="last", action="store_true", help="reopen your last chat"
    )
    commands.add_parser("web", help="open the web app in your browser")
    commands.add_parser("doctor", help="check this installation and say how to fix problems")
    logs_parser = commands.add_parser("logs", help="show what Enzo did, from its log")
    logs_parser.add_argument(
        "-n", "--lines", type=int, default=40, metavar="N", help="how many lines (default 40)"
    )
    logs_parser.add_argument("-f", "--follow", action="store_true", help="keep showing new lines")
    logs_parser.add_argument("--json", action="store_true", help="the lines as written (JSON)")
    logs_parser.add_argument("--path", action="store_true", help="only say where the log is")
    commands.add_parser("update", help="update Enzo to the latest release")
    serve = commands.add_parser("serve", help="run the gateway in the foreground")
    serve.add_argument("--managed", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    run: dict[str | None, Callable[[], int]] = {
        None: lambda: chat(last=args.last),
        "chat": lambda: chat(last=args.last),
        "web": web,
        "doctor": doctor,
        "logs": lambda: show_logs(
            lines=args.lines, keep=args.follow, as_json=args.json, path_only=args.path
        ),
        "update": update,
        "serve": lambda: serve_gateway(managed=args.managed),
    }
    try:
        return run[args.command]()
    except (launcher.LauncherError, tui.TuiError) as exc:
        print(f"{BAD} {exc}", file=sys.stderr)
        return 1


def chat(*, last: bool = False) -> int:
    """The terminal app: a new chat, or with ``last`` the chat used last (#57)."""
    with httpx2.Client(timeout=httpx2.Timeout(30.0, connect=15.0)) as http:
        app = tui.ensure(data_dir(), http, progress=print)
    with _gateway() as gateway, _ignoring_interrupts():
        env = {**os.environ, "ENZO_GATEWAY_URL": gateway.url}
        argv = [str(app), "--continue"] if last else [str(app)]
        return subprocess.call(argv, env=env)  # noqa: S603 - our own verified binary


def web() -> int:
    if not web_bundled():
        print(f"{BAD} This copy of Enzo has no web app built in. From a clone: scripts/dev.sh web")
        return 1
    with _gateway() as gateway:
        print(f"Enzo is open at {gateway.url}  (Ctrl+C here stops it)")
        webbrowser.open(gateway.url)
        with contextlib.suppress(KeyboardInterrupt):
            while True:
                time.sleep(3600)
    return 0


def serve_gateway(*, managed: bool) -> int:
    """The gateway in this process, on the bundled database unless ENZO_DATABASE_URL is set."""
    import uvicorn  # noqa: PLC0415 - only the gateway process needs the server stack

    from enzo import bundled_db  # noqa: PLC0415
    from enzo.gateway.app import create_app  # noqa: PLC0415
    from enzo.store.migrate import upgrade  # noqa: PLC0415

    # An installed gateway (started by the apps) writes only its log file (#48); `enzo serve`
    # also shows it on screen.
    logs.configure(file=log_file(), stream=None if managed else sys.stderr)
    log = logging.getLogger("enzo.cli")
    began = time.perf_counter()
    url = os.environ.get("ENZO_DATABASE_URL")
    if url is None:
        first = not bundled_db.set_up(data_dir())
        url = bundled_db.start(data_dir())
        log.info("database.ready", extra={"first_start": first, "ms": _ms_since(began)})
    upgrade(url)  # every start: `enzo update` needs no separate migration step
    settings = Settings(database_url=url)
    if managed:
        launcher.write_own_pid(state_dir())
        atexit.register(launcher.forget_own_pid, state_dir())
    # Uvicorn shuts down on SIGTERM, then raises the signal again. Python's default action would
    # end the process on the spot and skip the exit hooks that stop the bundled Postgres: exit
    # normally instead, so they run.
    signal.signal(signal.SIGTERM, _exit_normally)
    log.info(
        "gateway.start",
        extra={"version": __version__, "port": settings.port, "ms": _ms_since(began)},
    )
    # log_config=None: uvicorn keeps the logging set up above instead of installing its own.
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_config=None)
    return 0


def _ms_since(began: float) -> int:
    return round((time.perf_counter() - began) * 1000)


def log_file() -> Path:
    return logs.log_dir(state_dir()) / logs.LOG_FILE


def show_logs(
    *, lines: int = 40, keep: bool = False, as_json: bool = False, path_only: bool = False
) -> int:
    """The end of Enzo's log, readable (or as written, to attach to a bug report)."""
    path = log_file()
    if path_only:
        print(path)
        return 0
    if not path.exists() and not keep:
        print(f"{NOTE} No log yet: Enzo writes {path} once it has started.")
        return 0

    def show(raw: str) -> str:
        return raw.rstrip("\n") if as_json else logs.readable(raw)

    if not keep:
        for raw in logs.last_lines(path, max(lines, 0)):
            print(show(raw))
        return 0
    with contextlib.suppress(KeyboardInterrupt):
        for raw in logs.follow(path, lines=max(lines, 0)):
            print(show(raw), flush=True)
    return 0


def _exit_normally(_signum: int, _frame: object) -> None:
    raise SystemExit(0)


def doctor() -> int:
    """Each check: ✓ fine, · worth knowing, ✗ a problem with how to fix it."""
    print(f"Enzo {__version__} · Python {platform.python_version()} · {_platform()}")
    results = [
        _check_terminal_app(),
        _check_web_app(),
        _check_folders(),
        _check_vault(),
        _check_gateway(),
        _check_log(),
    ]
    for mark, line in results:
        print(f"{mark} {line}")
    return 1 if any(mark == BAD for mark, _ in results) else 0


def update() -> int:
    uv = shutil.which("uv")
    if uv is None or "uv" not in sys.prefix.split(os.sep):
        print(f"Update by running the install command again:\n  {INSTALL}")
        return 0
    code = subprocess.call([uv, "tool", "upgrade", "enzo-agent"])  # noqa: S603 - uv itself
    if code != 0:
        return code
    now = installed_version()
    if now == __version__:
        print(f"Enzo {__version__} is already the latest.")
    else:
        print(
            f"Updated Enzo {__version__} → {now}. The next `enzo` fetches the matching terminal "
            "app and updates your data."
        )
    return 0


def installed_version() -> str | None:
    """The version on disk now (this process still runs the one it started with)."""
    found = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import importlib.metadata as m; print(m.version('enzo-agent'))",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    version = found.stdout.strip()
    return version if found.returncode == 0 and version else None


def web_bundled() -> bool:
    return (resources.files("enzo") / "webapp" / "index.html").is_file()


@contextlib.contextmanager
def _gateway() -> Iterator[launcher.Gateway]:
    """The gateway for this app, released however the app ends: quitting, Ctrl+C, a closed
    terminal window (SIGHUP) or a plain kill (SIGTERM). Otherwise it would run on unseen."""
    from enzo import bundled_db  # noqa: PLC0415

    state = state_dir()
    first = "ENZO_DATABASE_URL" not in os.environ and not bundled_db.set_up(data_dir())
    gateway = launcher.attach(state, Settings().port, first_start=first)
    previous = {sig: signal.signal(sig, _exit_on_signal) for sig in (signal.SIGHUP, signal.SIGTERM)}
    try:
        yield gateway
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        launcher.release(state)


def _exit_on_signal(signum: int, _frame: object) -> None:
    raise SystemExit(128 + signum)


@contextlib.contextmanager
def _ignoring_interrupts() -> Iterator[None]:
    """Ctrl+C belongs to the terminal app while it runs, not to this launcher."""
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


def _platform() -> str:
    try:
        return tui.current_platform()
    except tui.TuiError:
        return f"{platform.system()} {platform.machine()} (not supported)"


def _check_terminal_app() -> tuple[str, str]:
    try:
        target = tui.current_platform()
    except tui.TuiError as exc:
        return BAD, str(exc)
    binary = tui.path(data_dir())
    if binary.is_file():
        return OK, f"Terminal app: {binary}"
    if tui.release_assets() is None:
        return NOTE, "Terminal app: not part of this copy (a source checkout: use scripts/dev.sh)"
    return NOTE, f"Terminal app: downloads on the first `enzo` ({target})"


def _check_web_app() -> tuple[str, str]:
    if web_bundled():
        return OK, "Web app: built in (`enzo web`)"
    return NOTE, "Web app: not built into this copy (from a clone: scripts/dev.sh web)"


def _check_folders() -> tuple[str, str]:
    try:
        for folder in (data_dir(), state_dir(), Settings().config_dir):
            probe = private_dir(folder) / ".enzo-doctor"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
    except OSError as exc:
        return BAD, f"Folders: cannot write ({exc}). Check the permissions of {exc.filename}."
    return OK, f"Folders: data {data_dir()} · settings {Settings().config_dir}"


def _check_vault() -> tuple[str, str]:
    from enzo.accounts.vault import (  # noqa: PLC0415
        EncryptedFileVault,
        VaultUnavailableError,
        open_vault,
    )

    cfg = Settings()
    key = cfg.vault_key.get_secret_value() if cfg.vault_key else None
    try:
        vault = open_vault(cfg.config_dir, key)
    except VaultUnavailableError as exc:
        return BAD, f"Credential vault: {exc}"
    kind = "encrypted file" if isinstance(vault, EncryptedFileVault) else "system keychain"
    return OK, f"Credential vault: {kind}"


def _check_gateway() -> tuple[str, str]:
    port = Settings().port
    info = launcher.health(port)
    if info is None:
        if launcher.port_taken(port):
            return BAD, f"Port {port} is used by another program: close it or set ENZO_PORT."
        return NOTE, "Gateway: not running (it starts with `enzo` or `enzo web`)"
    try:
        rows = httpx2.get(f"{launcher.url_for(port)}/connections", timeout=2.0).json()
        connected = [r["provider"] for r in rows if r.get("connected")]
    except (httpx2.HTTPError, ValueError, KeyError, TypeError):
        connected = []
    accounts = ", ".join(connected) if connected else "none yet (/login chatgpt in the app)"
    where = f"{info.get('version')} at {launcher.url_for(port)}"
    return OK, f"Gateway: running {where} · accounts: {accounts}"


def _check_log() -> tuple[str, str]:
    path = log_file()
    if path.exists():
        return OK, f"Log: {path} (`enzo logs` shows it; attach it to a bug report)"
    return NOTE, f"Log: none yet (Enzo writes {path} once it has started)"


if __name__ == "__main__":
    sys.exit(main())
