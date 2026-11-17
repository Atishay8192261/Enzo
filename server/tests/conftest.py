"""Shared fixtures: one real, migrated Postgres database per test session."""

from collections.abc import AsyncIterator, Iterator

import pytest
from alembic import command

from enzo.store.repository import Store
from tests.pg import TABLES, alembic_config, create_database, drop_database


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
