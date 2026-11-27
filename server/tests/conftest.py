"""Shared fixtures: one real, migrated Postgres database per test session."""

import os
from collections.abc import AsyncIterator, Iterator

import pytest
from alembic import command

from enzo.store.repository import Store
from tests.pg import TABLES, alembic_config, create_database, drop_database

# A gateway started by a test never downloads the embedding model (ADR 0030): tests that need
# search by meaning give it a fake embedder, or the real model from ENZO_TEST_MODEL_DIR.
os.environ.setdefault("ENZO_EMBEDDINGS", "off")


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
