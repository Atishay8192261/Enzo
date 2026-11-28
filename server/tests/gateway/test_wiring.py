"""The real app wiring: brains use the user's connected key, and key changes apply at once."""

import os
from pathlib import Path

import pytest

from enzo.accounts.service import AccountsService
from enzo.agent.service import ConversationService
from enzo.brain import AuthError
from enzo.brain.adapters.anthropic_brain import AnthropicBrain
from enzo.brain.config import BrainConfig, load_config
from enzo.brain.router import NoBrainAvailableError, Router
from enzo.config import Settings
from enzo.gateway.app import create_app
from tests.accounts.vaults import file_store
from tests.fakes import fake_key


async def accept(_cfg: BrainConfig, _key: str) -> None:
    return None


def anthropic_key(router: Router) -> str | None:
    brain = router.brain("claude-opus")
    assert isinstance(brain, AnthropicBrain)
    return brain._client.api_key


async def test_brains_follow_the_connected_account(
    database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    accounts = AccountsService(load_config(), file_store(tmp_path), accept)
    app = create_app(Settings(env="test", database_url=database_url), accounts=accounts)
    async with app.router.lifespan_context(app):
        service = app.state.service
        assert isinstance(service, ConversationService)
        router = service.router
        with pytest.raises(AuthError, match="run /login anthropic"):
            router.brain("claude-opus")
        await accounts.connect_api_key("anthropic", fake_key("first"))
        assert anthropic_key(router) == fake_key("first")
        await accounts.connect_api_key("anthropic", fake_key("second"))
        assert anthropic_key(router) == fake_key("second")
        accounts.disconnect("anthropic")
        with pytest.raises(AuthError, match="run /login anthropic"):
            router.brain("claude-opus")


async def test_shell_keys_are_ignored_unless_opted_in(
    database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (#24): a key exported in the shell was used, and billed, while every screen
    said "not connected". The connected account is the only source unless ENZO_USE_ENV_KEYS."""
    monkeypatch.setenv("OPENAI_API_KEY", fake_key("shell"))
    accounts = AccountsService(load_config(), file_store(tmp_path), accept)
    app = create_app(Settings(env="test", database_url=database_url), accounts=accounts)
    async with app.router.lifespan_context(app):
        router = app.state.service.router
        assert not router.available("gpt-sol")
        with pytest.raises(NoBrainAvailableError, match="no model account is connected"):
            router.plan("streaming", affinity="c1")
        assert all(not s.connected for s in accounts.status())


async def test_opted_in_shell_keys_are_used_and_shown(
    database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", fake_key("shell"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    accounts = AccountsService(load_config(), file_store(tmp_path), accept, env=os.environ)
    settings = Settings(env="test", database_url=database_url, use_env_keys=True)
    app = create_app(settings, accounts=accounts)
    async with app.router.lifespan_context(app):
        router = app.state.service.router
        assert router.available("gpt-sol")
        assert router.plan("streaming", affinity="c1").primary == "gpt-sol"
        status = {s.provider: s for s in app.state.accounts.status()}
        assert (status["openai"].connected, status["openai"].method) == (True, "environment")
        assert not status["anthropic"].connected  # what the app says matches what it uses


async def test_the_app_registers_the_core_tools(
    database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    accounts = AccountsService(load_config(), file_store(tmp_path), accept)
    settings = Settings(env="test", database_url=database_url, workspace_dir=tmp_path / "Enzo")
    app = create_app(settings, accounts=accounts)
    async with app.router.lifespan_context(app):
        registry = app.state.service.registry
        names = [s.name for s in registry.specs()]
        assert names == [
            "web_search",
            "read_page",
            "list_files",
            "read_file",
            "create_file",
            "edit_file",
            "delete_file",
            "remember",
            "forget",
            "search_memory",
            "search_past_chats",
            "remind",
            "schedule_task",
            "finish_task",
            "list_scheduled",
            "cancel_scheduled",
            "create_goal",
            "record_goal_progress",
            "close_goal",
            "list_goals",
            "read_stored_output",
            "view_image",
            "browser_start",
            "browser_open",
            "browser_click",
            "browser_type",
            "browser_select",
            "browser_scroll",
            "browser_back",
            "browser_read",
            "browser_close",
            "browser_screenshot",
            "add_discretion_rule",
            "remove_discretion_rule",
            "watch",
            "enable_watch",
            "list_watches",
            "cancel_watch",
            "delegate",
            "stop_helper",
        ]
        # Tools that show the brain an image are left out for a brain that cannot see.
        blind = [s.name for s in registry.specs(vision=False)]
        assert set(names) - set(blind) == {"view_image", "browser_screenshot"}
        # Images are kept in Enzo's private data folder, and the router sends them (ADR 0033).
        assert app.state.service.images is app.state.service.router.images is not None
        # Long results are kept whole outside the context (ADR 0024).
        assert registry.retain is app.state.service.outputs is not None
        gated = [n for n in names if registry.get(n).requires_approval]
        fenced = [n for n in names if registry.get(n).untrusted_output]
        # The brain knows where the user's files are (#41), as the user would type it.
        assert "Enzo folder is" in app.state.service._developer
        assert app.state.service.memory is not None
        assert app.state.service.goals is not None  # open goals come with each message
        # Search by meaning (ADR 0030): one index for memory, past chats and stored texts.
        index = app.state.service.index
        assert index is not None
        assert app.state.service.memory._index is index
        assert index.embedder.state == "off"  # tests never download the model
        assert app.state.background.runner is not None  # reminders are delivered
        # The browser lives as long as a turn: the service tells it when turns end (ADR 0027).
        (browsers, helpers) = app.state.service._hooks
        assert browsers.own_port == settings.port
        # What goes out is checked for private details, with the user's rules (ADR 0029).
        assert app.state.service.discretion is not None
        outward = [n for n in names if registry.get(n).outbound is not None]
        # Helpers shape each chat's turns and close when their chat is stopped (ADR 0028).
        assert app.state.service.planner is helpers is app.state.helpers
        assert (helpers._max, helpers._steps) == (settings.helpers_max, settings.helper_steps)
        # Watches check on the runner's clock and never read Enzo's own port (ADR 0031).
        watcher = app.state.background.watcher
        assert watcher in app.state.background.runner._workers
        assert watcher.sources.own_site(f"http://127.0.0.1:{settings.port}/healthz")
    # A task acts on its own: approved first. A browser task is approved once, as a whole.
    # Setting up a watch and turning it on are two separate yeses (spec §9.4, §12.4).
    assert gated == ["delete_file", "schedule_task", "browser_start", "watch", "enable_watch"]
    # Everything that sends text to a third party is checked first (§12.5, ADR 0029).
    assert outward == ["web_search", "read_page", "browser_start", "browser_open", "browser_type"]
    assert fenced == [
        "web_search",
        "read_page",
        "read_file",
        "search_past_chats",
        "read_stored_output",
        *(n for n in names if n.startswith("browser_") and n != "browser_close"),
        "list_watches",  # what a watch last read is outside content
    ]
