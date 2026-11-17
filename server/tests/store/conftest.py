"""Real Postgres for store tests: one fresh database per session, migrated with Alembic."""

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import asyncpg
import pytest
from alembic import command
from alembic.config import Config

from enzo.store.repository import Store

ADMIN_URL = os.environ.get("ENZO_TEST_ADMIN_URL", "postgresql://enzo:enzo@localhost:5432/postgres")
ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"
TABLES = "approvals, tool_calls, brain_calls, messages, conversations"


def alembic_config(url: str) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


async def _admin(sql: str) -> None:
    conn = await asyncpg.connect(ADMIN_URL)
    try:
        await conn.execute(sql)
    finally:
        await conn.close()


def create_database() -> tuple[str, str]:
    name = f"enzo_test_{uuid.uuid4().hex[:10]}"
    try:
        asyncio.run(_admin(f'CREATE DATABASE "{name}"'))
    except OSError as exc:  # fail loudly with the fix, never skip
        msg = f"Postgres is required for store tests ({ADMIN_URL}): {exc}"
        raise RuntimeError(msg) from exc
    base = ADMIN_URL.rsplit("/", 1)[0].replace("postgresql://", "postgresql+asyncpg://")
    return name, f"{base}/{name}"


def drop_database(name: str) -> None:
    asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    name, url = create_database()
    try:
        command.upgrade(alembic_config(url), "head")
        yield url
    finally:
        drop_database(name)


@pytest.fixture
async def store(database_url: str) -> AsyncIterator[Store]:
    s = Store.from_url(database_url)
    try:
        yield s
    finally:
        async with s.engine.begin() as conn:
            await conn.exec_driver_sql(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE")
        await s.close()
