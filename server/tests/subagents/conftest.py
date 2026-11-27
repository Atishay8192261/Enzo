from collections.abc import AsyncIterator

import pytest

from enzo.store.repository import Store
from tests.subagents.fakes import close_all


@pytest.fixture(autouse=True)
async def _no_turn_outlives_its_test(store: Store) -> AsyncIterator[None]:
    """Helpers' turns run in the background: end them before the database is emptied."""
    yield
    await close_all()
