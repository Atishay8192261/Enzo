"""Privacy tests share one database: rules for every chat would leak into the next test, so each
test's rules are removed when it ends."""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import delete

from enzo.privacy import Rules
from enzo.store.repository import Store
from enzo.store.schema import DiscretionRuleRow


@pytest.fixture
async def rules(store: Store) -> AsyncIterator[Rules]:
    yield Rules(store.engine)
    async with store.engine.begin() as conn:
        await conn.execute(delete(DiscretionRuleRow))
