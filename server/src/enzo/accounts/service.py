"""Connect, list, check and disconnect the user's accounts.

Two kinds of account share one flow (key checked first, then kept in the vault, ADR 0009):

- **model providers** (Anthropic, OpenAI, …), from ``brains.toml``: they unlock models;
- **services** a tool uses (e.g. Tavily for web search): they unlock or extend tools.
"""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime

from pydantic import SecretStr

from enzo.accounts.store import ApiKeyCredential, Connection, CredentialStore
from enzo.accounts.vault import VaultUnavailableError
from enzo.brain.config import BrainConfig, BrainsConfig

type KeyChecker = Callable[[BrainConfig, str], Awaitable[None]]


class UnknownProviderError(KeyError):
    pass


class NotConnectedError(LookupError):
    pass


class EnvironmentKeyError(LookupError):
    """The key comes from an environment variable: only unsetting it disconnects."""


@dataclass(frozen=True, slots=True)
class Service:
    """A non-model account that a tool uses."""

    name: str
    #: Shown to the user: what connecting it does, e.g. "raises web search limits".
    purpose: str
    tools: tuple[str, ...]
    env_var: str
    check: Callable[[str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ProviderStatus:
    provider: str
    connected: bool
    method: str | None
    connected_at: datetime | None
    methods: tuple[str, ...]
    models: tuple[str, ...]
    tools: tuple[str, ...] = ()
    purpose: str | None = None


@dataclass(frozen=True, slots=True)
class _Account:
    """What every account has in common, whichever kind it is."""

    name: str
    env_var: str
    check: Callable[[str], Awaitable[None]]
    models: tuple[str, ...]
    tools: tuple[str, ...]
    purpose: str | None


class AccountsService:
    def __init__(
        self,
        config: BrainsConfig,
        store: CredentialStore | None,
        checker: KeyChecker,
        *,
        services: Mapping[str, Service] | None = None,
        on_change: Callable[[str], None] = lambda _provider: None,
        unavailable: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._store = store
        self._listeners = [on_change]
        self._unavailable = unavailable or "no credential vault"
        #: Environment keys, only when the user opted in (ENZO_USE_ENV_KEYS): then reported as
        #: connected with method "environment", so the app shows what it really uses (#24).
        self._env = env
        self._accounts = _accounts(config, checker, services or {})

    def on_change(self, listener: Callable[[str], None]) -> None:
        """Called with the provider name whenever its connection changes."""
        self._listeners.append(listener)

    def _changed(self, provider: str) -> None:
        for listener in self._listeners:
            listener(provider)

    def status(self) -> list[ProviderStatus]:
        connections = self._store.connections() if self._store else {}
        out = []
        for name, account in self._accounts.items():
            c = connections.get(name)
            from_env = c is None and self._env_key(account) is not None
            out.append(
                ProviderStatus(
                    provider=name,
                    connected=c is not None or from_env,
                    method=c.method if c else ("environment" if from_env else None),
                    connected_at=c.connected_at if c else None,
                    methods=("api_key",),
                    models=account.models,
                    tools=account.tools,
                    purpose=account.purpose,
                )
            )
        return out

    async def connect_api_key(self, provider: str, api_key: str) -> Connection:
        """Check the key with the provider first; save it only if it works (§8.2)."""
        account = self._account(provider)
        store = self._require_store()
        credential = ApiKeyCredential(api_key=SecretStr(api_key.strip()))
        await account.check(credential.api_key.get_secret_value())
        connection = store.connect(provider, credential)
        self._changed(provider)
        return connection

    def disconnect(self, provider: str) -> None:
        account = self._account(provider)
        store = self._require_store()
        if provider not in store.connections() and self._env_key(account) is not None:
            msg = f"{provider}'s key comes from {account.env_var}: unset it to disconnect"
            raise EnvironmentKeyError(msg)
        store.disconnect(provider)
        self._changed(provider)

    async def check(self, provider: str) -> None:
        """Re-check a saved connection, e.g. after the key was revoked (spec §8.4)."""
        account = self._account(provider)
        key = self._require_store().api_key(provider) or self._env_key(account)
        if key is None:
            msg = f"{provider} is not connected"
            raise NotConnectedError(msg)
        await account.check(key)

    def connected(self, provider: str) -> bool:
        """From the non-secret index: routing asks often, and must not read the vault for it."""
        return self._store is not None and provider in self._store.connections()

    def api_key(self, provider: str) -> str | None:
        """The connected account's key, else (opted in only) the environment's."""
        stored = self._store.api_key(provider) if self._store else None
        account = self._accounts.get(provider)
        return stored or (self._env_key(account) if account else None)

    def _env_key(self, account: _Account) -> str | None:
        if self._env is None:
            return None
        return self._env.get(account.env_var) or None

    def _account(self, provider: str) -> _Account:
        account = self._accounts.get(provider)
        if account is None:
            raise UnknownProviderError(provider)
        return account

    def _require_store(self) -> CredentialStore:
        if self._store is None:
            raise VaultUnavailableError(self._unavailable)
        return self._store


def _accounts(
    config: BrainsConfig, checker: KeyChecker, services: Mapping[str, Service]
) -> dict[str, _Account]:
    """Model providers in catalog order (one representative brain each), then services."""
    out: dict[str, _Account] = {}
    for cfg in config.brains.values():
        name = cfg.provider_name
        if name in out:
            continue
        models = tuple(b.model for b in config.brains.values() if b.provider_name == name)

        async def check(key: str, cfg: BrainConfig = cfg) -> None:
            await checker(cfg, key)

        out[name] = _Account(name, cfg.api_key_env, check, models, (), None)
    for name, service in services.items():
        if name in out:
            msg = f"service {name!r} clashes with a model provider of the same name"
            raise ValueError(msg)
        out[name] = _Account(
            name, service.env_var, service.check, (), service.tools, service.purpose
        )
    return out
