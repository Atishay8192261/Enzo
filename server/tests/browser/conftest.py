"""Browser tests drive a real browser (ADR 0027): the installed Chrome, Edge or Chromium."""

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from enzo.browser.session import Browsers, find_browser
from tests.browser.shop import Running, serve


@pytest.fixture(scope="session")
def real_browser() -> None:
    """Skip where no browser is installed; in CI that is a failure, never a silent skip."""
    if find_browser() is None and not os.environ.get("ENZO_BROWSER_PATH"):
        if os.environ.get("CI"):
            pytest.fail("no browser installed: CI must run the browser tests")
        pytest.skip("no Chrome, Edge or Chromium installed")


@pytest.fixture
def attacker() -> Iterator[Running]:
    with serve() as running:
        yield running


@pytest.fixture
def shop(attacker: Running) -> Iterator[Running]:
    with serve(evil=f"http://127.0.0.1:{attacker.port}") as running:
        yield running


@pytest.fixture
async def browsers(real_browser: None, tmp_path: Path) -> AsyncIterator[Browsers]:
    path = os.environ.get("ENZO_BROWSER_PATH")
    b = Browsers(
        tmp_path / "profile",
        executable=Path(path) if path else None,
        headless=True,
        own_port=8787,
        # Chrome's sandbox is a property of Chrome, not of Enzo's rules: tests run without it so
        # they work on CI runners that cannot create one (the default is checked on its own).
        sandbox=False,
    )
    try:
        yield b
    finally:
        await b.close_all()
