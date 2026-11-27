"""`enzo deploy`: the same private Enzo on an always-on machine the user owns (ADR 0034).

Everything goes over the user's own SSH: check the machine, run Enzo's installer there (the
exact version installed here, or a wheel built here), set it up as a service, check it answers
through the tunnel, and remember where it is. Accounts are signed in again there, through the
tunnel; chats and memory are copied only if the user says yes.
"""

import hashlib
import json
import re
import shlex
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

from enzo import __version__
from enzo.config import Settings
from enzo.deploy import move, probe, record, tunnel
from enzo.deploy.record import Deployment
from enzo.deploy.service import DEFAULT_PORT, FIRST_START_S
from enzo.deploy.ssh import Remote, SshError
from enzo.paths import state_dir

OK, BAD, NOTE = "✓", "✗", "·"
#: Finds the `enzo` that uv installed there, and its version.
FIND_ENZO = r"""
for c in "$(command -v uv 2>/dev/null)" "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
  if [ -n "$c" ] && [ -x "$c" ]; then uv=$c; break; fi
done
[ -n "${uv:-}" ] || exit 3
bin=$("$uv" tool dir --bin) || exit 3
[ -x "$bin/enzo" ] || exit 4
echo "$bin/enzo"
"$bin/enzo" --version
"""
UNINSTALL = r"""
for c in "$(command -v uv 2>/dev/null)" "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
  if [ -n "$c" ] && [ -x "$c" ]; then "$c" tool uninstall enzo-agent; exit $?; fi
done
exit 0
"""
_WHEEL = re.compile(r"^[A-Za-z0-9_.+-]+\.whl$")


class DeployError(Exception):
    """Why the deploy stopped. The message is shown to the user as is."""


type Ask = Callable[[str], bool]


def ask_terminal(question: str) -> bool:
    if not sys.stdin.isatty():
        return False
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


@dataclass(frozen=True, slots=True)
class Options:
    port: int = DEFAULT_PORT
    #: A wheel built here, or a requirement (enzo-agent==0.1.1). Default: this exact version.
    package: str | None = None
    assume_yes: bool = False
    #: Copy chats and memory: True / False, or None to ask.
    move: bool | None = None
    ssh: str = "ssh"


def installer() -> str:
    return (resources.files("enzo") / "deploy" / "install.sh").read_text(encoding="utf-8")


def deploy(
    destination: str,
    opts: Options,
    *,
    say: Callable[[str], None] = print,
    ask: Ask = ask_terminal,
) -> int:
    remote = Remote(destination, ssh=opts.ssh)
    config = Settings().config_dir
    say(f"Checking {destination} over SSH…")
    host = probe.parse(remote.script(probe.SCRIPT, timeout=60).out)
    problems = probe.problems(host)
    if problems:
        for line in problems:
            say(f"{BAD} {line}")
        return 1
    say(f"{OK} {destination}: {host.summary}")
    for line in probe.notes(host, __version__):
        say(f"{NOTE} {line}")
    say(f"Installing Enzo {__version__} there (a minute or two)…")
    exe = install(remote, host.home, opts.package)
    say(f"{OK} Installed: {exe}")
    deployment = _set_up(remote, exe, host.os, host.arch, opts.port, say=say)
    record.save(config, deployment)
    _move_data(remote, deployment, opts, say=say, ask=ask)
    say("")
    say(f"{OK} Enzo is always on at {deployment.name}: reminders and tasks run there 24/7.")
    say("  Open it from this computer with:  enzo     (this computer's own Enzo: enzo --local)")
    say(
        "  Sign in to your accounts there once: in enzo, type /login chatgpt (or /login "
        "anthropic, …).\n  Sign-in pages open in this computer's browser and come back through "
        "SSH; keys go\n  straight into its encrypted vault."
    )
    return 0


def install(remote: Remote, home: str, package: str | None) -> str:
    """Run Enzo's installer there; returns the `enzo` command it installed."""
    spec = package or f"enzo-agent=={__version__}"
    wheel = Path(spec).expanduser()
    if wheel.is_file():
        if not _WHEEL.match(wheel.name):
            msg = f"{wheel.name} isn't a wheel file (.whl) Enzo can send."
            raise DeployError(msg)
        data = wheel.read_bytes()
        folder = f".cache/enzo/deploy/{hashlib.sha256(data).hexdigest()[:16]}"
        remote.run(
            [
                "sh",
                "-c",
                f'umask 077 && mkdir -p "$HOME/{folder}" && cat > "$HOME/{folder}/{wheel.name}"',
            ],
            data=data,
            timeout=600,
        )
        spec = f"{home}/{folder}/{wheel.name}"
    script = f"ENZO_PACKAGE={shlex.quote(spec)}\nexport ENZO_PACKAGE\n{installer()}"
    done = remote.script(script, timeout=1200)
    if done.code != 0:
        tail = "\n".join((done.out + done.err).strip().splitlines()[-12:])
        msg = f"Enzo's installer failed there:\n{tail}"
        if package is None:
            msg += (
                f"\n(Is enzo-agent {__version__} released? A copy built here: --package FILE.whl)"
            )
        raise DeployError(msg)
    found = remote.script(FIND_ENZO, timeout=60)
    lines = found.out.strip().splitlines()
    if found.code != 0 or len(lines) < 2:
        msg = "Enzo was installed there, but its enzo command can't be found."
        raise DeployError(msg)
    exe, version = lines[0], lines[1].split()[-1]
    if version != __version__:
        msg = f"Enzo {version} was installed there, not {__version__}."
        raise DeployError(msg)
    return exe


def _set_up(
    remote: Remote, exe: str, os_name: str, arch: str, port: int, *, say: Callable[[str], None]
) -> Deployment:
    say("Setting it up to stay on (starts at boot, restarts if it fails); the first start sets up")
    say("its database…")
    done = remote.run(
        [exe, "service", "install", "--port", str(port), "--json"], timeout=FIRST_START_S + 120
    )
    result = _json(done.out)
    if result is None:
        tail = "\n".join((done.out + done.err).strip().splitlines()[-8:])
        msg = f"Setting up the service there failed:\n{tail}"
        raise DeployError(msg)
    for note in result.get("notes", []):
        say(f"{NOTE} {note}")
    if not result.get("ok"):
        raise DeployError(str(result.get("problem") or "the service did not start"))
    boot = "at boot" if result.get("manager") == "systemd" else "at login"
    say(f"{OK} Running there on 127.0.0.1:{port} ({result.get('manager')}, starts {boot})")
    deployment = Deployment(
        destination=remote.destination,
        port=port,
        enzo=exe,
        os=os_name,
        arch=arch,
        version=__version__,
        deployed_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    # The real path the apps take: a forward from here, nothing listening beyond 127.0.0.1.
    state = state_dir()
    answered = tunnel.attach(state, deployment, ssh=remote.ssh)
    tunnel.release(state)
    say(f"{OK} Reachable from here through SSH: Enzo {answered.get('version')} answered")
    return deployment


def _move_data(
    remote: Remote, dep: Deployment, opts: Options, *, say: Callable[[str], None], ask: Ask
) -> None:
    if opts.move is False:
        return
    url = move.database_url(create=False)
    if url is None:
        return
    here = move.counts(url)
    if here.empty:
        return
    question = (
        f"Copy your {here.words()} from this computer to {dep.name}? Reminders and tasks then "
        "run only there (they are turned off here). Accounts are not copied."
    )
    if not (opts.move or (not opts.assume_yes and ask(question))):
        say(f"{NOTE} Chats and memory stay on this computer (copy them later: enzo deploy move).")
        return
    copy(remote, dep, url, say=say)


def copy(remote: Remote, dep: Deployment, url: str, *, say: Callable[[str], None]) -> None:
    say(f"Copying to {dep.name}…")
    payload = move.pack(url)
    done = remote.run([dep.enzo, "service", "restore", "--json"], data=payload, timeout=900)
    result = _json(done.out)
    if result is None or not result.get("ok"):
        why = result.get("problem") if result else (done.out + done.err).strip()[-300:]
        raise DeployError(f"Nothing was copied: {why}")
    off = move.turn_off_scheduled(url)
    counts = move.Counts(**result["counts"])
    say(f"{OK} Copied {counts.words()}." + (f" Turned off {off} here." if off else ""))


def _json(out: str) -> dict[str, Any] | None:
    for line in reversed(out.strip().splitlines()):
        try:
            found = json.loads(line)
        except ValueError:
            continue
        if isinstance(found, dict):
            return found
    return None


def remote_status(dep: Deployment, *, ssh: str = "ssh", timeout: float = 30) -> dict[str, Any]:
    done = Remote(dep.destination, ssh=ssh).run(
        [dep.enzo, "service", "status", "--json"], timeout=timeout
    )
    found = _json(done.out)
    if found is None:
        msg = f"Enzo on {dep.name} didn't answer: {(done.out + done.err).strip()[-200:]}"
        raise DeployError(msg)
    return found


def summary(found: dict[str, Any], dep: Deployment) -> tuple[bool, str]:
    """One line for `enzo doctor` and the first line of `enzo deploy status`."""
    if found.get("state") == "running" and found.get("gateway"):
        names = found.get("accounts")
        accounts = ", ".join(str(a) for a in names) if isinstance(names, list) else ""
        accounts = accounts or "none yet"
        nxt = (
            f" · scheduled: {found['scheduled']}, next {found['next']}"
            if found.get("scheduled")
            else ""
        )
        boot = "" if found.get("boot") else " · ✗ does not start at boot"
        line = f"running {found['gateway']} on {dep.name} · accounts: {accounts}{nxt}{boot}"
        return bool(found.get("boot")), line
    return False, f"{found.get('state')} on {dep.name}: see enzo deploy status"


def status(dep: Deployment, *, ssh: str = "ssh", say: Callable[[str], None] = print) -> int:
    found = remote_status(dep, ssh=ssh)
    good, line = summary(found, dep)
    say(f"{OK if good else BAD} Always-on Enzo: {line}")
    if found.get("state") != "running":
        for text in list(found.get("journal") or [])[-8:]:
            say(f"    {text}")
    if found.get("linger") is False:
        say(
            f"{BAD} Lingering is off there: Enzo stops when you log out "
            "(fix: sudo loginctl enable-linger)"
        )
    if found.get("gateway") and found.get("gateway") != __version__:
        say(f"{NOTE} It runs {found['gateway']}, this computer {__version__}: enzo deploy update")
    if found.get("disk_free_gb") is not None:
        say(f"{NOTE} Disk there: {found['disk_free_gb']} GB free · vault: {found.get('vault')}")
    return 0 if good else 1


def logs(dep: Deployment, *, lines: int, follow: bool, as_json: bool, ssh: str = "ssh") -> int:
    words = [dep.enzo, "logs", "-n", str(lines)]
    words += ["-f"] if follow else []
    words += ["--json"] if as_json else []
    return Remote(dep.destination, ssh=ssh).attach(words, tty=follow and sys.stdout.isatty())


def update(dep: Deployment, opts: Options, *, say: Callable[[str], None] = print) -> int:
    remote = Remote(dep.destination, ssh=opts.ssh)
    say(f"Updating Enzo on {dep.name} to {__version__}…")
    exe = install(remote, _home(remote), opts.package)
    updated = _set_up(remote, exe, dep.os, dep.arch, dep.port, say=say)
    record.save(Settings().config_dir, updated)
    return 0


def _home(remote: Remote) -> str:
    return remote.script('echo "$HOME"', timeout=60).out.strip()


def stop(dep: Deployment, *, ssh: str = "ssh", say: Callable[[str], None] = print) -> int:
    Remote(dep.destination, ssh=ssh).run([dep.enzo, "service", "stop"], timeout=120)
    tunnel.close(state_dir())
    say(f"Stopped Enzo on {dep.name}. It stays off, even after a restart, until: enzo deploy start")
    return 0


def start(dep: Deployment, *, ssh: str = "ssh", say: Callable[[str], None] = print) -> int:
    done = Remote(dep.destination, ssh=ssh).run([dep.enzo, "service", "start"], timeout=180)
    say(done.out.strip() or done.err.strip())
    return done.code


def remove(
    dep: Deployment,
    *,
    delete_data: bool,
    assume_yes: bool,
    ssh: str = "ssh",
    say: Callable[[str], None] = print,
    ask: Ask = ask_terminal,
) -> int:
    what = (
        "Its service and the program go, and everything Enzo stored there is DELETED (chats, "
        "memory, accounts)."
        if delete_data
        else "Its service and the program go; its chats and memory stay there."
    )
    if not assume_yes and not ask(f"Remove Enzo from {dep.name}? {what}"):
        say("Nothing was changed.")
        return 1
    remote = Remote(dep.destination, ssh=ssh)
    tunnel.close(state_dir())
    words = [dep.enzo, "service", "remove", *(["--delete-data"] if delete_data else [])]
    done = remote.run(words, timeout=180)
    if done.code != 0:
        raise DeployError(f"Could not remove the service: {(done.out + done.err).strip()[-300:]}")
    remote.script(UNINSTALL, timeout=300)
    record.forget(Settings().config_dir)
    say(f"{OK} Removed Enzo from {dep.name}. `enzo` opens this computer's own Enzo again.")
    return 0


def check_destination_matches(dep: Deployment | None, destination: str | None) -> Deployment:
    """The deployment a command is about: the one saved here (a name must match it)."""
    if dep is None:
        msg = "Enzo isn't deployed anywhere from this computer yet: enzo deploy user@host"
        raise DeployError(msg)
    if destination and destination not in (dep.destination, dep.name):
        msg = f"Enzo is deployed to {dep.destination}, not {destination}."
        raise DeployError(msg)
    return dep


__all__ = ["DEFAULT_PORT", "DeployError", "Options", "Remote", "SshError", "deploy", "move"]
