"""Provider connections: secrets in the vault, a non-secret index for listing (§11.2, §8.2)."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, TypeAdapter

from enzo.accounts.vault import Vault, write_private

type Method = Literal["api_key"]


class ApiKeyCredential(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    method: Literal["api_key"] = "api_key"
    api_key: SecretStr = Field(min_length=8)

    def secret_json(self) -> str:
        return json.dumps({"method": self.method, "api_key": self.api_key.get_secret_value()})


type Credential = Annotated[ApiKeyCredential, Field(discriminator="method")]
_CREDENTIAL: TypeAdapter[Credential] = TypeAdapter(Credential)


class _IndexEntry(BaseModel):
    """Non-secret facts about one connection, validated when read back."""

    model_config = ConfigDict(extra="forbid")
    method: Method
    connected_at: datetime


_INDEX: TypeAdapter[dict[str, _IndexEntry]] = TypeAdapter(dict[str, _IndexEntry])


@dataclass(frozen=True, slots=True)
class Connection:
    """What may be shown about a connection: never the secret."""

    provider: str
    method: Method
    connected_at: datetime


class CredentialStore:
    def __init__(self, vault: Vault, index_path: Path) -> None:
        self._vault = vault
        self._index_path = index_path

    def connect(self, provider: str, credential: Credential) -> Connection:
        self._vault.set(_entry(provider), credential.secret_json())
        connection = Connection(provider, credential.method, datetime.now(UTC))
        index = self._index()
        index[provider] = _IndexEntry(
            method=connection.method, connected_at=connection.connected_at
        )
        self._save(index)
        return connection

    def disconnect(self, provider: str) -> None:
        self._vault.delete(_entry(provider))
        index = self._index()
        if index.pop(provider, None) is not None:
            self._save(index)

    def credential(self, provider: str) -> Credential | None:
        raw = self._vault.get(_entry(provider))
        return None if raw is None else _CREDENTIAL.validate_json(raw)

    def api_key(self, provider: str) -> str | None:
        credential = self.credential(provider)
        return credential.api_key.get_secret_value() if credential else None

    def connections(self) -> dict[str, Connection]:
        return {p: Connection(p, e.method, e.connected_at) for p, e in self._index().items()}

    def _index(self) -> dict[str, _IndexEntry]:
        try:
            return _INDEX.validate_json(self._index_path.read_bytes())
        except FileNotFoundError:
            return {}

    def _save(self, index: dict[str, _IndexEntry]) -> None:
        write_private(self._index_path, _INDEX.dump_json(index, indent=2).decode())


def _entry(provider: str) -> str:
    return f"provider:{provider}"
