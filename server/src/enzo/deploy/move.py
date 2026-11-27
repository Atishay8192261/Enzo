"""Copy chats, memory, goals and scheduled work to the always-on Enzo (asked first, ADR 0034).

What goes: Enzo's database (``pg_dump``), the long results it keeps beside chats
(``data/context``), and ``Memory.md`` / ``Goals.md``. What never goes: accounts (the user signs in
again there, through the tunnel), the browser profile (its cookies are sign-ins) and logs.
Reminders and scheduled tasks then run only there: they are turned off here, so nothing arrives
twice. The copy travels inside the SSH connection, never through a file on another machine.
"""

import asyncio
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import BinaryIO

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from enzo import bundled_db
from enzo.config import Settings
from enzo.paths import data_dir, private_dir
from enzo.schedule import Schedule

DUMP = "enzo.dump"
WORKSPACE_FILES = ("Memory.md", "Goals.md")


class MoveError(Exception):
    """Why nothing was copied. The message is shown to the user as is."""


@dataclass(frozen=True, slots=True)
class Counts:
    chats: int
    memories: int
    goals: int
    scheduled: int

    @property
    def empty(self) -> bool:
        return not (self.chats or self.memories or self.goals or self.scheduled)

    def words(self) -> str:
        parts = [
            _n(self.chats, "chat"),
            _n(self.memories, "memory", "memories"),
            _n(self.goals, "goal"),
            _n(self.scheduled, "reminder or task", "reminders and tasks"),
        ]
        return ", ".join(parts[:-1]) + " and " + parts[-1]


@dataclass(frozen=True, slots=True)
class Restored:
    ok: bool
    counts: Counts | None = None
    problem: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    def words(self) -> str:
        if not self.ok or self.counts is None:
            return f"✗ {self.problem}"
        return f"✓ Copied {self.counts.words()}."


def _n(count: int, one: str, many: str | None = None) -> str:
    return f"{count} {one if count == 1 else many or one + 's'}"


def database_url(*, create: bool) -> str | None:
    """This machine's Enzo database: the bundled one, or ``ENZO_DATABASE_URL``."""
    return bundled_db.url_for_command(data_dir(), create=create)


def tool(name: str) -> str:
    """``pg_dump`` / ``pg_restore``: the bundled Postgres's own, or the system's beside a
    developer's ``ENZO_DATABASE_URL``."""
    if os.environ.get("ENZO_DATABASE_URL"):
        found = shutil.which(name)
        if found is None:
            msg = f"{name} isn't installed here (it comes with Postgres)."
            raise MoveError(msg)
        return found
    from pixeltable_pgserver.utils import (  # noqa: PLC0415 - heavy
        POSTGRES_VERSIONS,
        TARGET_POSTGRES_VERSION,
    )

    return str(POSTGRES_VERSIONS[TARGET_POSTGRES_VERSION] / name)


def libpq(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


def counts(url: str) -> Counts:
    async def read() -> Counts:
        engine = create_async_engine(url)
        try:
            async with engine.connect() as conn:

                async def one(sql: str) -> int:
                    return int((await conn.execute(text(sql))).scalar_one())

                return Counts(
                    chats=await one("SELECT count(*) FROM conversations"),
                    memories=await one("SELECT count(*) FROM memories WHERE status = 'active'"),
                    goals=await one("SELECT count(*) FROM goals"),
                    scheduled=await one("SELECT count(*) FROM jobs WHERE status = 'active'"),
                )
        finally:
            await engine.dispose()

    return asyncio.run(read())


def pack(url: str) -> bytes:
    """Everything that moves, as one tar (gzip) stream."""
    out = io.BytesIO()
    with tempfile.TemporaryDirectory(prefix="enzo-move-") as tmp:
        dump = Path(tmp) / DUMP
        done = subprocess.run(  # noqa: S603 - Postgres's own tool, fixed arguments
            [tool("pg_dump"), "-Fc", "--no-owner", "--no-privileges", "-f", str(dump), libpq(url)],
            capture_output=True,
            text=True,
            check=False,
        )
        if done.returncode != 0:
            msg = f"Could not read Enzo's database here: {done.stderr.strip()[-300:]}"
            raise MoveError(msg)
        with tarfile.open(fileobj=out, mode="w:gz") as tar:
            tar.add(dump, arcname=DUMP)
            context = data_dir() / "context"
            if context.is_dir():
                tar.add(context, arcname="context")
            workspace = Settings().workspace_dir
            for name in WORKSPACE_FILES:
                if (workspace / name).is_file():
                    tar.add(workspace / name, arcname=f"workspace/{name}")
    return out.getvalue()


def turn_off_scheduled(url: str) -> int:
    """After a move: reminders and tasks run there now, so none of them also runs here."""

    async def cancel() -> int:
        engine = create_async_engine(url)
        try:
            schedule = Schedule(engine)
            jobs = await schedule.active()
            for job in jobs:
                await schedule.cancel(job.id)
            return len(jobs)
        finally:
            await engine.dispose()

    return asyncio.run(cancel())


def restore(stream: BinaryIO, *, replace: bool = False) -> Restored:
    """On the always-on machine (`enzo service restore`, the tar on stdin): stop the service,
    load the copy into its database, put the files in place, start it again."""
    from enzo.deploy import service  # noqa: PLC0415
    from enzo.store.migrate import upgrade  # noqa: PLC0415

    payload = stream.read()
    mgr = service.manager()
    was_on = service.recorded_port() is not None
    if was_on:
        mgr.stop()
    try:
        url = database_url(create=True)
        assert url is not None  # noqa: S101 - create=True always gives one
        upgrade(url)
        before = counts(url)
        if before.chats and not replace:
            msg = f"Enzo here already has {_n(before.chats, 'chat')}, so nothing was copied."
            return Restored(ok=False, problem=msg)
        with tempfile.TemporaryDirectory(prefix="enzo-move-") as tmp:
            with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
                tar.extractall(tmp, filter="data")  # no paths outside tmp, no devices or links
            done = subprocess.run(  # noqa: S603 - Postgres's own tool, fixed arguments
                [
                    tool("pg_restore"),
                    "--clean",
                    "--if-exists",
                    "--no-owner",
                    "--no-privileges",
                    "--single-transaction",
                    "--exit-on-error",
                    "-d",
                    libpq(url),
                    str(Path(tmp) / DUMP),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if done.returncode != 0:
                msg = f"The copy could not be loaded: {done.stderr.strip()[-300:]}"
                return Restored(ok=False, problem=msg)
            upgrade(url)
            if (Path(tmp) / "context").is_dir():
                shutil.copytree(
                    Path(tmp) / "context", private_dir(data_dir()) / "context", dirs_exist_ok=True
                )
            workspace = Settings().workspace_dir
            for name in WORKSPACE_FILES:
                copied = Path(tmp) / "workspace" / name
                if copied.is_file():
                    workspace.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(copied, workspace / name)
        return Restored(ok=True, counts=counts(url))
    finally:
        if was_on:
            service.start(mgr=mgr)
