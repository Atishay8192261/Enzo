"""Provider connections: secrets in the vault, a non-secret index for listing (§11.2, §8.2)."""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, TypeAdapter

from enzo.accounts.vault import Vault, write_private

type Method = Literal["api_key", "chatgpt"]


class ApiKeyCredential(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    method: Literal["api_key"] = "api_key"
    api_key: SecretStr = Field(min_length=8)

    def secret_json(self) -> str:
        return json.dumps({"method": self.method, "api_key": self.api_key.get_secret_value()})


class ChatGPTCredential(BaseModel):
    """Signed in with ChatGPT: OAuth tokens that let Enzo use the user's ChatGPT plan (ADR 0013)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    method: Literal["chatgpt"] = "chatgpt"
    #: The client OpenAI issued to this user on first sign-in (public: no secret exists).
    client_id: str = Field(min_length=1)
    #: The verified ID token's subject: the account this registration belongs to.
    subject: str = Field(min_length=1)
    email: str | None = None
    id_token: SecretStr
    access_token: SecretStr
    refresh_token: SecretStr
    scopes: tuple[str, ...]
    expires_at: datetime

    def secret_json(self) -> str:
        tokens = {"id_token", "access_token", "refresh_token"}
        return json.dumps(
            {
                **self.model_dump(mode="json", exclude=tokens),
                "id_token": self.id_token.get_secret_value(),
                "access_token": self.access_token.get_secret_value(),
                "refresh_token": self.refresh_token.get_secret_value(),
            }
        )


type Credential = Annotated[ApiKeyCredential | ChatGPTCredential, Field(discriminator="method")]
_CREDENTIAL: TypeAdapter[Credential] = TypeAdapter(Credential)


class _IndexEntry(BaseModel):
    """Non-secret facts about one connection, validated when read back."""

    model_config = ConfigDict(extra="forbid")
    method: Method
    connected_at: datetime
    #: Shown to the user to tell accounts apart, e.g. the ChatGPT account's email.
    label: str | None = None
    #: Models this account may use, when the provider lists them per account (a ChatGPT plan).
    models: list[str] | None = None


_INDEX: TypeAdapter[dict[str, _IndexEntry]] = TypeAdapter(dict[str, _IndexEntry])


@dataclass(frozen=True, slots=True)
class Connection:
    """What may be shown about a connection: never the secret."""

    provider: str
    method: Method
    connected_at: datetime
    label: str | None = None
    models: tuple[str, ...] | None = None


class CredentialStore:
    def __init__(self, vault: Vault, index_path: Path) -> None:
        self._vault = vault
        self._index_path = index_path

    def connect(
        self,
        provider: str,
        credential: Credential,
        *,
        label: str | None = None,
        models: Sequence[str] | None = None,
    ) -> Connection:
        self._vault.set(_entry(provider), credential.secret_json())
        connection = Connection(
            provider,
            credential.method,
            datetime.now(UTC),
            label,
            None if models is None else tuple(models),
        )
        index = self._index()
        index[provider] = _IndexEntry(
            method=connection.method,
            connected_at=connection.connected_at,
            label=label,
            models=None if models is None else list(models),
        )
        self._save(index)
        return connection

    def set_models(self, provider: str, models: Sequence[str]) -> None:
        index = self._index()
        entry = index.get(provider)
        if entry is not None:
            index[provider] = entry.model_copy(update={"models": list(models)})
            self._save(index)

    def replace(self, provider: str, credential: Credential) -> None:
        """New secrets for an existing connection (e.g. refreshed tokens); the index stays."""
        self._vault.set(_entry(provider), credential.secret_json())

    def record(self, name: str) -> str | None:
        """A secret record that is not a connection (e.g. a sign-in registration)."""
        return self._vault.get(f"record:{name}")

    def keep_record(self, name: str, value: str) -> None:
        self._vault.set(f"record:{name}", value)

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
        if not isinstance(credential, ApiKeyCredential):
            return None
        return credential.api_key.get_secret_value()

    def connections(self) -> dict[str, Connection]:
        return {
            p: Connection(
                p,
                e.method,
                e.connected_at,
                e.label,
                None if e.models is None else tuple(e.models),
            )
            for p, e in self._index().items()
        }

    def _index(self) -> dict[str, _IndexEntry]:
        try:
            return _INDEX.validate_json(self._index_path.read_bytes())
        except FileNotFoundError:
            return {}

    def _save(self, index: dict[str, _IndexEntry]) -> None:
        write_private(
            self._index_path, _INDEX.dump_json(index, indent=2, exclude_none=True).decode()
        )


def _entry(provider: str) -> str:
    return f"provider:{provider}"
