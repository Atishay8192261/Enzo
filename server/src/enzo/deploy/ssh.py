"""The user's own SSH, as they set it up: their keys or ssh-agent, ~/.ssh/config, known hosts.

Enzo runs the system ``ssh`` with ``BatchMode=yes``, so ssh never asks for a password or a key's
passphrase: Enzo never sees, asks for or stores one. A machine ssh has not met before is refused
(the user confirms its identity once with a plain ``ssh``), never accepted silently.
"""

import shlex
import subprocess
from dataclasses import dataclass

#: Keys or the agent only; give up on a machine that does not answer; notice a dead connection.
OPTIONS = (
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=15",
    "-o",
    "ServerAliveInterval=5",
    "-o",
    "ServerAliveCountMax=3",
)
#: ssh's own exit code for "could not connect" (a remote command's codes pass through).
SSH_FAILED = 255


class SshError(Exception):
    """SSH could not reach the machine, or was refused. The message says what to do."""


@dataclass(frozen=True, slots=True)
class Result:
    code: int
    out: str
    err: str


def check_destination(dest: str) -> str:
    """``user@host``, ``host`` (an alias from ~/.ssh/config) or ``ssh://user@host:port``."""
    if (
        not dest
        or dest.startswith("-")
        or any(ch.isspace() or ord(ch) < 32 for ch in dest)
        or "'" in dest
        or '"' in dest
    ):
        msg = f"“{dest}” isn't a machine to connect to. Use user@host, like me@myserver.example."
        raise SshError(msg)
    return dest


def remote_words(words: list[str]) -> str:
    """A command for the remote login shell, each word quoted."""
    return " ".join(shlex.quote(w) for w in words)


@dataclass(frozen=True, slots=True)
class Remote:
    destination: str
    ssh: str = "ssh"

    def argv(self, *options: str, command: str | None = None, tty: bool = False) -> list[str]:
        out = [
            self.ssh,
            *OPTIONS,
            "-t" if tty else "-T",
            *options,
            check_destination(self.destination),
        ]
        return [*out, command] if command is not None else out

    def script(self, text: str, *, timeout: float = 120) -> Result:
        """Run a POSIX sh script there (sent on stdin, so the user's own shell never parses it)."""
        return self._run(self.argv(command="sh -s"), text.encode(), timeout)

    def run(self, words: list[str], *, data: bytes | None = None, timeout: float = 120) -> Result:
        """Run one command there; ``data`` goes to its stdin."""
        return self._run(self.argv(command=remote_words(words)), data or b"", timeout)

    def attach(self, words: list[str], *, tty: bool) -> int:
        """Run a command there with this terminal (`enzo deploy logs -f`): its exit code."""
        done = subprocess.run(self.argv(command=remote_words(words), tty=tty), check=False)  # noqa: S603
        if done.returncode == SSH_FAILED:
            raise SshError(f"Lost the SSH connection to {self.destination}.")
        return done.returncode

    def _run(self, argv: list[str], data: bytes, timeout: float) -> Result:
        try:
            done = subprocess.run(  # noqa: S603 - the user's own ssh
                argv, input=data, capture_output=True, timeout=timeout, check=False
            )
        except FileNotFoundError:
            msg = "Enzo needs the ssh command, which isn't installed on this computer."
            raise SshError(msg) from None
        except subprocess.TimeoutExpired:
            msg = f"{self.destination} stopped answering over SSH (waited {round(timeout)} s)."
            raise SshError(msg) from None
        out = done.stdout.decode(errors="replace")
        err = done.stderr.decode(errors="replace")
        if done.returncode == SSH_FAILED:
            raise SshError(explain(err, self.destination))
        return Result(done.returncode, out, err)


def explain(stderr: str, dest: str) -> str:  # noqa: PLR0911 - one answer per ssh error
    """What ssh's own error means for the user, and what to do about it."""
    text = stderr.lower()
    host = dest.removeprefix("ssh://").split("@")[-1].split(":")[0].split("/")[0]
    if "remote host identification has changed" in text:
        return (
            f"{host} doesn't prove the identity your computer remembers for it, so Enzo won't "
            "connect: someone could be in between. If you rebuilt that machine, forget its old "
            f"identity with `ssh-keygen -R {host}`, connect once with `ssh {dest}`, then try again."
        )
    if "host key verification failed" in text or "no ed25519 host key is known" in text:
        return (
            f"Your computer hasn't confirmed {host}'s identity yet. Connect once with "
            f"`ssh {dest}`, check the fingerprint it shows, then try again."
        )
    if "permission denied" in text:
        return (
            f"{host} didn't accept your SSH key. Enzo signs in with your SSH keys or ssh-agent and "
            f"never asks for a password. Set up key login (`ssh-copy-id {dest}`), check that "
            f"`ssh {dest}` works without a password, then try again."
        )
    if "could not resolve hostname" in text:
        return f"Can't find a machine called {host}: check the name (or your ~/.ssh/config)."
    if "connection refused" in text:
        return f"{host} refuses SSH connections: is its SSH server on, and on this port?"
    if any(
        word in text
        for word in ("timed out", "no route to host", "network is unreachable", "host is down")
    ):
        return f"Can't reach {host}: is it on and connected to the internet?"
    if "remote port forwarding failed" in text or "administratively prohibited" in text:
        return (
            f"{host}'s SSH server doesn't allow port forwarding, which Enzo uses to reach itself "
            "there. Allow it (AllowTcpForwarding yes in its sshd_config) and try again."
        )
    last = next((line for line in reversed(stderr.splitlines()) if line.strip()), "")
    return f"SSH to {host} failed: {last.strip() or 'no reason given'}"
