"""Where this computer's always-on Enzo is: ``~/.config/enzo/deploy.json`` (nothing secret).

With a record, `enzo`, `enzo -c` and `enzo web` open that Enzo (through the tunnel); ``--local``
opens this computer's own.
"""

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from enzo.accounts.vault import write_private

FILE = "deploy.json"


@dataclass(frozen=True, slots=True)
class Deployment:
    #: As the user typed it for ssh: ``me@myserver``, an alias, or ``ssh://me@host:2222``.
    destination: str
    #: The gateway's port there, and the tunnel's here (the same number: sign-in callbacks).
    port: int
    #: The `enzo` command there.
    enzo: str
    os: str
    arch: str
    version: str
    deployed_at: str

    @property
    def name(self) -> str:
        """The machine, as shown to the user: ``myserver``."""
        rest = self.destination.removeprefix("ssh://").split("@")[-1]
        return rest.split(":")[0].split("/")[0] or self.destination


def load(config: Path) -> Deployment | None:
    try:
        raw = json.loads((config / FILE).read_text(encoding="utf-8"))
        return Deployment(**raw)
    except (OSError, ValueError, TypeError):
        return None


def save(config: Path, deployment: Deployment) -> None:
    write_private(config / FILE, json.dumps(asdict(deployment), indent=2) + "\n")


def forget(config: Path) -> None:
    (config / FILE).unlink(missing_ok=True)
