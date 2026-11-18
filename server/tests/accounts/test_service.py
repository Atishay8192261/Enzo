"""Accounts: a key is checked with the provider before it is saved."""

from pathlib import Path

import pytest

from enzo.accounts.service import (
    AccountsService,
    EnvironmentKeyError,
    NotConnectedError,
    Service,
    UnknownProviderError,
)
from enzo.accounts.vault import VaultUnavailableError
from enzo.brain import AuthError
from enzo.brain.config import BrainConfig, load_config
from tests.accounts.vaults import file_store
from tests.fakes import fake_key

KEY = fake_key("anthropic")


class Checker:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, cfg: BrainConfig, api_key: str) -> None:
        self.calls.append((cfg.provider_name, api_key))
        if self.error:
            raise self.error


def accounts(tmp_path: Path, checker: Checker) -> tuple[AccountsService, list[str]]:
    changed: list[str] = []
    service = AccountsService(
        load_config(), file_store(tmp_path), checker, on_change=changed.append
    )
    return service, changed


def connected(service: AccountsService) -> dict[str, bool]:
    return {s.provider: s.connected for s in service.status()}


async def test_a_working_key_is_checked_then_saved(tmp_path: Path) -> None:
    checker = Checker()
    service, changed = accounts(tmp_path, checker)
    await service.connect_api_key("anthropic", f"  {KEY}\n")  # pasted with whitespace
    assert checker.calls == [("anthropic", KEY)]
    assert service.api_key("anthropic") == KEY
    assert changed == ["anthropic"]
    assert connected(service)["anthropic"]


async def test_a_rejected_key_is_not_saved(tmp_path: Path) -> None:
    error = AuthError("invalid x-api-key", provider="anthropic", model="m")
    service, changed = accounts(tmp_path, Checker(error))
    with pytest.raises(AuthError):
        await service.connect_api_key("anthropic", KEY)
    assert service.api_key("anthropic") is None
    assert changed == []
    assert not connected(service)["anthropic"]


async def test_unknown_providers_are_refused_before_any_check(tmp_path: Path) -> None:
    checker = Checker()
    service, _ = accounts(tmp_path, checker)
    with pytest.raises(UnknownProviderError):
        await service.connect_api_key("ghost", KEY)
    with pytest.raises(UnknownProviderError):
        service.disconnect("ghost")
    assert checker.calls == []


def test_status_lists_every_configured_provider_and_its_models(tmp_path: Path) -> None:
    service, _ = accounts(tmp_path, Checker())
    status = {s.provider: s for s in service.status()}
    config = load_config()
    assert set(status) == {b.provider_name for b in config.brains.values()}
    for provider, s in status.items():
        expected = {b.model for b in config.brains.values() if b.provider_name == provider}
        assert set(s.models) == expected
        assert (s.connected, s.method, s.connected_at, s.methods) == (
            False,
            None,
            None,
            ("api_key",),
        )


async def test_disconnect_and_recheck(tmp_path: Path) -> None:
    checker = Checker()
    service, changed = accounts(tmp_path, checker)
    with pytest.raises(NotConnectedError):
        await service.check("xai")
    await service.connect_api_key("xai", fake_key("xai"))
    await service.check("xai")
    assert checker.calls == [("xai", fake_key("xai"))] * 2
    service.disconnect("xai")
    assert changed == ["xai", "xai"]
    assert service.api_key("xai") is None


async def test_without_a_vault_nothing_can_be_saved_and_the_reason_is_given() -> None:
    service = AccountsService(load_config(), None, Checker(), unavailable="set ENZO_VAULT_KEY")
    assert not any(connected(service).values())
    assert service.api_key("anthropic") is None
    with pytest.raises(VaultUnavailableError, match="set ENZO_VAULT_KEY"):
        await service.connect_api_key("anthropic", KEY)


async def test_environment_keys_show_as_connected_only_when_opted_in(tmp_path: Path) -> None:
    env = {"XAI_API_KEY": fake_key("env")}
    checker = Checker()
    hidden = AccountsService(load_config(), file_store(tmp_path / "a"), checker)
    assert not connected(hidden)["xai"]  # not opted in: not used, not shown
    shown = AccountsService(load_config(), file_store(tmp_path / "b"), checker, env=env)
    xai = next(s for s in shown.status() if s.provider == "xai")
    assert (xai.connected, xai.method, xai.connected_at) == (True, "environment", None)
    await shown.check("xai")  # checks the environment key
    assert checker.calls == [("xai", fake_key("env"))]
    with pytest.raises(EnvironmentKeyError, match="comes from XAI_API_KEY: unset it"):
        shown.disconnect("xai")
    await shown.connect_api_key("xai", fake_key("vault"))  # a connected account wins
    xai = next(s for s in shown.status() if s.provider == "xai")
    assert xai.method == "api_key"
    shown.disconnect("xai")  # disconnects the account; the env key then shows again
    assert next(s for s in shown.status() if s.provider == "xai").method == "environment"


async def test_services_are_accounts_too(tmp_path: Path) -> None:
    checked: list[str] = []

    async def check(key: str) -> None:
        checked.append(key)

    search = Service("tavily", "raises search limits", ("web_search",), "TAVILY_API_KEY", check)
    service = AccountsService(
        load_config(), file_store(tmp_path), Checker(), services={"tavily": search}
    )
    tavily = next(s for s in service.status() if s.provider == "tavily")
    assert (tavily.connected, tavily.models, tavily.tools, tavily.purpose) == (
        False,
        (),
        ("web_search",),
        "raises search limits",
    )
    await service.connect_api_key("tavily", fake_key("tavily"))  # the service's own check
    assert checked == [fake_key("tavily")]
    assert service.api_key("tavily") == fake_key("tavily")
    providers = [s.provider for s in service.status()]
    assert providers[-1] == "tavily"  # after the model providers, which keep catalog order
    assert providers[:2] == ["anthropic", "openai"]


def test_a_service_cannot_shadow_a_model_provider(tmp_path: Path) -> None:
    async def check(_key: str) -> None:
        return None

    clash = Service("openai", "x", (), "X_KEY", check)
    with pytest.raises(ValueError, match="clashes with a model provider"):
        AccountsService(load_config(), file_store(tmp_path), Checker(), services={"openai": clash})
