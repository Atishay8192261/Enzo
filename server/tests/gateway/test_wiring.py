"""The real app wiring: brains use the user's connected key, and key changes apply at once."""

from pathlib import Path

import pytest

from enzo.accounts.service import AccountsService
from enzo.agent.service import ConversationService
from enzo.brain import AuthError
from enzo.brain.adapters.anthropic_brain import AnthropicBrain
from enzo.brain.config import BrainConfig, load_config
from enzo.brain.router import Router
from enzo.config import Settings
from enzo.gateway.app import create_app
from tests.accounts.vaults import file_store


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
        await accounts.connect_api_key("anthropic", "sk-first-0123456")
        assert anthropic_key(router) == "sk-first-0123456"
        await accounts.connect_api_key("anthropic", "sk-second-0123456")
        assert anthropic_key(router) == "sk-second-0123456"
        accounts.disconnect("anthropic")
        with pytest.raises(AuthError, match="run /login anthropic"):
            router.brain("claude-opus")
