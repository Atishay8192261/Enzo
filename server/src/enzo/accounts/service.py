"""Connect, list, check and disconnect provider accounts."""

from collections.abc import Awaitable, Callable
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


@dataclass(frozen=True, slots=True)
class ProviderStatus:
    provider: str
    connected: bool
    method: str | None
    connected_at: datetime | None
    methods: tuple[str, ...]
    models: tuple[str, ...]


class AccountsService:
    def __init__(
        self,
        config: BrainsConfig,
        store: CredentialStore | None,
        checker: KeyChecker,
        *,
        on_change: Callable[[str], None] = lambda _provider: None,
        unavailable: str | None = None,
    ) -> None:
        self._config = config
        self._store = store
        self._checker = checker
        self._listeners = [on_change]
        self._unavailable = unavailable or "no credential vault"

    def on_change(self, listener: Callable[[str], None]) -> None:
        """Called with the provider name whenever its connection changes."""
        self._listeners.append(listener)

    def _changed(self, provider: str) -> None:
        for listener in self._listeners:
            listener(provider)

    def providers(self) -> dict[str, BrainConfig]:
        """One representative brain per provider (same endpoint for all its models)."""
        seen: dict[str, BrainConfig] = {}
        for cfg in self._config.brains.values():
            seen.setdefault(cfg.provider_name, cfg)
        return seen

    def status(self) -> list[ProviderStatus]:
        connections = self._store.connections() if self._store else {}
        out = []
        for provider in self.providers():
            c = connections.get(provider)
            models = tuple(
                b.model for b in self._config.brains.values() if b.provider_name == provider
            )
            out.append(
                ProviderStatus(
                    provider=provider,
                    connected=c is not None,
                    method=c.method if c else None,
                    connected_at=c.connected_at if c else None,
                    methods=("api_key",),
                    models=models,
                )
            )
        return out

    async def connect_api_key(self, provider: str, api_key: str) -> Connection:
        """Check the key with the provider first; save it only if it works (§8.2)."""
        cfg = self._provider(provider)
        store = self._require_store()
        credential = ApiKeyCredential(api_key=SecretStr(api_key.strip()))
        await self._checker(cfg, credential.api_key.get_secret_value())
        connection = store.connect(provider, credential)
        self._changed(provider)
        return connection

    def disconnect(self, provider: str) -> None:
        self._provider(provider)
        self._require_store().disconnect(provider)
        self._changed(provider)

    async def check(self, provider: str) -> None:
        """Re-check a saved connection, e.g. after the key was revoked (spec §8.4)."""
        cfg = self._provider(provider)
        key = self._require_store().api_key(provider)
        if key is None:
            msg = f"{provider} is not connected"
            raise NotConnectedError(msg)
        await self._checker(cfg, key)

    def connected(self, provider: str) -> bool:
        """From the non-secret index: routing asks often, and must not read the vault for it."""
        return self._store is not None and provider in self._store.connections()

    def api_key(self, provider: str) -> str | None:
        return self._store.api_key(provider) if self._store else None

    def _provider(self, provider: str) -> BrainConfig:
        cfg = self.providers().get(provider)
        if cfg is None:
            raise UnknownProviderError(provider)
        return cfg

    def _require_store(self) -> CredentialStore:
        if self._store is None:
            raise VaultUnavailableError(self._unavailable)
        return self._store
