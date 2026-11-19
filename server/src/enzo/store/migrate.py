"""Bring a database to the latest schema, from the migrations shipped inside the package.

An installed Enzo has no ``alembic.ini``: the gateway migrates on every start, so ``enzo update``
needs no separate step.
"""

from importlib import resources

from alembic import command
from alembic.config import Config


def upgrade(url: str) -> None:
    """Run every migration not yet applied. Synchronous: call it before the event loop starts."""
    config = Config()
    config.set_main_option("script_location", str(resources.files("enzo") / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
