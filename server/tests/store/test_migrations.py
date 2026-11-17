import logging

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from enzo.store.schema import Base
from tests.store.conftest import alembic_config, create_database, drop_database


def test_migrations_upgrade_downgrade_and_upgrade_again() -> None:
    name, url = create_database()
    try:
        cfg = alembic_config(url)
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "base")
        command.upgrade(cfg, "head")
    finally:
        drop_database(name)


async def test_models_and_migrations_describe_the_same_schema(database_url: str) -> None:
    engine = create_async_engine(database_url)

    def diff(conn: Connection) -> list[object]:
        return list(compare_metadata(MigrationContext.configure(conn), Base.metadata))

    try:
        async with engine.connect() as conn:
            assert await conn.run_sync(diff) == []
    finally:
        await engine.dispose()


def test_running_migrations_keeps_enzo_logging_alive() -> None:
    """Regression: Alembic's logging setup disabled every existing logger, silencing Enzo's logs."""
    probe = logging.getLogger("enzo.probe")
    name, url = create_database()
    try:
        command.upgrade(alembic_config(url), "head")
    finally:
        drop_database(name)
    assert not probe.disabled
