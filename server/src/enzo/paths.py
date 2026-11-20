"""Where an installed Enzo keeps its files (XDG layout, on macOS too, like uv).

- data:   ``~/.local/share/enzo``  the database, the terminal app (``ENZO_DATA_DIR`` overrides)
- state:  ``~/.local/state/enzo``  the running gateway's pid and log (``ENZO_STATE_DIR`` overrides)
- config: ``~/.config/enzo``       accounts index, vault file, host id (``Settings.config_dir``)

Files the user works with live in ``~/Enzo`` (``Settings.workspace_dir``), never in these.
"""

import os
from pathlib import Path


def _xdg(variable: str, fallback: str, override: str) -> Path:
    if value := os.environ.get(override):
        return Path(value).expanduser()
    base = os.environ.get(variable)
    root = Path(base).expanduser() if base else Path.home() / fallback
    return root / "enzo"


def data_dir() -> Path:
    return _xdg("XDG_DATA_HOME", ".local/share", "ENZO_DATA_DIR")


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state", "ENZO_STATE_DIR")


def private_dir(path: Path) -> Path:
    """The folder, created if needed, readable by the user only (even if it already existed)."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def shown(path: Path) -> str:
    """A path as the user would type it: ``~/Enzo/plan.md`` under their home, else in full."""
    home = Path.home()
    return f"~/{path.relative_to(home).as_posix()}" if path.is_relative_to(home) else str(path)
