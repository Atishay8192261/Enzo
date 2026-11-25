"""The Postgres that ships with Enzo, so a user installs nothing else (#23, ADR 0016).

``pixeltable-pgserver`` carries Postgres 16 and pgvector as wheels for macOS and Linux. It listens
only on a Unix socket inside the data folder (no network port), and the folder is the user's alone.
It stops when the gateway that started it exits; a stale lock from a crash is cleaned on the next
start.
"""

import os
import warnings
from pathlib import Path

from enzo.paths import private_dir

DATABASE = "enzo"


def set_up(data: Path) -> bool:
    """The database exists already: a start takes seconds, not the first start's minute (#56)."""
    return (data / "postgres" / "PG_VERSION").is_file()


def url_for_command(data: Path, *, create: bool) -> str | None:
    """Enzo's database for a command beside the gateway (`enzo trace`, `enzo eval`): the one
    ``ENZO_DATABASE_URL`` names, else the bundled one, which several processes share. None when
    there is none yet and ``create`` is off."""
    if url := os.environ.get("ENZO_DATABASE_URL"):
        return url
    if not create and not set_up(data):
        return None
    return start(data)


def start(data: Path) -> str:
    """Start (or reuse) the bundled server and return the ``asyncpg`` URL of Enzo's database."""
    with warnings.catch_warnings():
        # platformdirs warns when XDG_RUNTIME_DIR is unset (it then uses a temp dir): harmless.
        warnings.simplefilter("ignore")
        from pixeltable_pgserver.postgres_server import get_server  # noqa: PLC0415 - heavy

    server = get_server(private_dir(data) / "postgres", cleanup_mode="stop")
    exists = server.psql("SELECT 1 FROM pg_database WHERE datname = 'enzo';")
    if "(1 row)" not in exists:
        server.psql("CREATE DATABASE enzo;")
    url: str = server.get_uri(database=DATABASE, driver="asyncpg")
    return url
