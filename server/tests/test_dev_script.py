"""``scripts/dev.sh doctor`` says exactly what is wrong with the database and how to fix it
(#32, #33). Postgres and the tools are faked on PATH, so every state can be set up."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "dev.sh"
BASH = shutil.which("bash") or "/bin/bash"

FAKE_PSQL = r"""#!/bin/bash
# A Postgres server in the state the test sets: FAKE_ROLE, FAKE_PASSWORD, FAKE_DB, FAKE_LOGIN,
# FAKE_VERSION, FAKE_VECTOR (pgvector installed).
[ "$1" = "--version" ] && { echo "psql (PostgreSQL) 14.19 (Homebrew)"; exit 0; }
target="" prev=""
for a in "$@"; do [ "$prev" = "-d" ] && target="$a"; prev="$a"; done
case "$target" in
  postgres) ok="$FAKE_LOGIN" ;;                                # the login user (Homebrew)
  */postgres) [ "$FAKE_ROLE$FAKE_PASSWORD" = 11 ] && ok=1 || ok=0 ;;  # the enzo role signs in
  */enzo) [ "$FAKE_ROLE$FAKE_PASSWORD$FAKE_DB" = 111 ] && ok=1 || ok=0 ;;
  *) ok=0 ;;
esac
[ "$ok" = 1 ] || { echo "psql: error: connection to server failed" >&2; exit 2; }
case "$*" in
  *server_version_num*) echo "$FAKE_VERSION" ;;
  *"SHOW server_version"*) echo "$((FAKE_VERSION / 10000)).$((FAKE_VERSION % 10000)) (Fake)" ;;
  *pg_roles*) [ "$FAKE_ROLE" = 1 ] && echo 1 ;;
  *pg_database*) [ "$FAKE_DB" = 1 ] && echo 1 ;;
  *pg_available_extensions*) [ "$FAKE_VECTOR" = 1 ] && echo 1 ;;
esac
exit 0
"""


def pinned(name: str) -> str:
    if name == "bun":
        return (ROOT / ".bun-version").read_text().strip()
    text = (ROOT / "package.json").read_text()
    return text.split('"packageManager": "pnpm@', 1)[1].split('"', 1)[0]


@pytest.fixture
def fake_bin(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    scripts = {
        "psql": FAKE_PSQL,
        "pg_isready": "#!/bin/sh\nexit 0\n",
        "createdb": "#!/bin/sh\nexit 0\n",
        "uname": '#!/bin/sh\necho "$FAKE_OS"\n',
        "node": '#!/bin/sh\necho "$FAKE_NODE"\n',
        "pnpm": f"#!/bin/sh\necho {pinned('pnpm')}\n",
        "bun": f"#!/bin/sh\necho {pinned('bun')}\n",
        "uv": "#!/bin/sh\necho uv 0.9.0\n",
    }
    for name, body in scripts.items():
        path = bin_dir / name
        path.write_text(body)
        path.chmod(0o755)
    return bin_dir


def doctor(  # noqa: PLR0913 - one keyword per faked state
    fake_bin: Path,
    *,
    role: bool,
    db: bool,
    password: bool = True,
    version: int = 160010,
    mac: bool = True,
    node: str = "v24.1.0",
    vector: bool = True,
) -> str:
    env = {
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "HOME": str(fake_bin.parent),
        "FAKE_ROLE": "1" if role else "0",
        "FAKE_PASSWORD": "1" if password else "0",
        "FAKE_DB": "1" if db else "0",
        # Homebrew makes the login user a superuser; on Linux only `sudo -u postgres` can look.
        "FAKE_LOGIN": "1" if mac else "0",
        "FAKE_VERSION": str(version),
        "FAKE_OS": "Darwin" if mac else "Linux",
        "FAKE_NODE": node,
        "FAKE_VECTOR": "1" if vector else "0",
    }
    run = subprocess.run(  # noqa: S603 - the repo's own script, fixed arguments
        [BASH, str(SCRIPT), "doctor"],
        env={**env, "LANG": os.environ.get("LANG", "C.UTF-8")},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return run.stdout + run.stderr


@pytest.mark.parametrize("mac", [True, False])
def test_a_missing_database_with_its_role_in_place_asks_only_for_the_database(
    fake_bin: Path, *, mac: bool
) -> None:
    out = doctor(fake_bin, role=True, db=False, mac=mac)
    assert "The enzo database is missing (the enzo role is there)" in out
    assert (
        "Run: createdb -O enzo enzo" if mac else "Run: sudo -u postgres createdb -O enzo enzo"
    ) in out
    assert "CREATE ROLE" not in out  # #32: it said to create the role, which then failed


def test_a_missing_role_and_database_are_two_separate_steps(fake_bin: Path) -> None:
    out = doctor(fake_bin, role=False, db=False)
    assert "The enzo role and database are missing" in out
    assert "Run: psql -d postgres -c \"CREATE ROLE enzo LOGIN SUPERUSER PASSWORD 'enzo'\"" in out
    assert "Then: createdb -O enzo enzo" in out
    assert "&&" not in out  # a failing first step must never hide the second


def test_a_role_with_another_password_gets_its_password_set_not_created_again(
    fake_bin: Path,
) -> None:
    out = doctor(fake_bin, role=True, password=False, db=False)
    assert "The enzo role cannot sign in with its password" in out
    assert "ALTER ROLE enzo LOGIN PASSWORD 'enzo'" in out
    assert "CREATE ROLE" not in out


def test_on_linux_an_unknown_role_state_gives_steps_that_work_either_way(fake_bin: Path) -> None:
    out = doctor(fake_bin, role=False, db=False, mac=False)
    assert "CREATE ROLE enzo LOGIN SUPERUSER PASSWORD 'enzo'" in out
    assert "if it says the role already exists, go on" in out
    assert "Then: sudo -u postgres createdb -O enzo enzo" in out


def test_the_server_version_is_checked_not_the_psql_client(fake_bin: Path) -> None:
    out = doctor(fake_bin, role=True, db=True, version=140019)
    assert "PostgreSQL server 14.19 is too old: Enzo needs 16 or newer" in out  # #33
    assert "brew install postgresql@16" in out
    assert "database reachable" not in out


def test_a_server_16_is_shown_by_its_own_version(fake_bin: Path) -> None:
    out = doctor(fake_bin, role=True, db=True, version=160010)
    assert "ok    PostgreSQL server 16.10" in out
    assert "ok    psql client 14.19" in out  # the client is fine; its version is not the server's
    assert "FIX" not in out


def test_the_server_version_is_found_before_the_database_exists(fake_bin: Path) -> None:
    out = doctor(fake_bin, role=True, db=False, version=150004)
    assert "PostgreSQL server 15.4 is too old" in out


def test_an_old_node_points_to_the_current_lts_installer(fake_bin: Path) -> None:
    out = doctor(fake_bin, role=True, db=True, node="v20.11.0")
    assert "Install the current Node LTS (22.12 or newer) from https://nodejs.org" in out
    assert "macOS Installer (.pkg)" in out
    assert "Node 24" not in out  # #32: nodejs.org's LTS moved on


@pytest.mark.parametrize("mac", [True, False])
def test_a_server_without_pgvector_says_how_to_add_it(fake_bin: Path, *, mac: bool) -> None:
    """Search by meaning keeps its vectors in pgvector (ADR 0030): migrations need it."""
    out = doctor(fake_bin, role=True, db=True, mac=mac, vector=False)
    assert "pgvector is missing (Enzo keeps its search by meaning in it)" in out
    if mac:
        assert "https://github.com/pgvector/pgvector#installation" in out
    else:
        assert "Run: sudo apt-get install -y postgresql-16-pgvector" in out
    assert "ok    pgvector" in doctor(fake_bin, role=True, db=True, mac=mac)
