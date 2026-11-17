"""Provider account endpoints: connect, disconnect and check the user's own accounts.

Secrets go in and never come out: responses, errors and logs carry no key.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, params
from pydantic import BaseModel, ConfigDict, Field

from enzo.accounts.check import check_api_key
from enzo.accounts.service import AccountsService, NotConnectedError, UnknownProviderError
from enzo.accounts.store import CredentialStore
from enzo.accounts.vault import VaultCorruptError, VaultUnavailableError, open_vault
from enzo.brain.config import BrainsConfig
from enzo.brain.errors import AuthError, BrainError
from enzo.config import Settings


def get_accounts(request: Request) -> AccountsService:
    accounts: AccountsService = request.app.state.accounts
    return accounts


AccountsDep = Annotated[AccountsService, Depends(get_accounts)]


class ApiKeyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key: str = Field(min_length=8, max_length=512, repr=False)


class ConnectionOut(BaseModel):
    """Never includes the secret."""

    provider: str
    connected: bool
    method: str | None
    connected_at: str | None
    methods: list[str]
    models: list[str]


class CheckOut(BaseModel):
    ok: bool
    detail: str | None = None


def build_accounts(cfg: Settings, config: BrainsConfig) -> AccountsService:
    """Accounts on the real vault; without one, connection endpoints explain how to get one."""
    key = cfg.vault_key.get_secret_value() if cfg.vault_key else None
    try:
        vault = open_vault(cfg.config_dir, key)
    except VaultUnavailableError as exc:
        return AccountsService(config, None, check_api_key, unavailable=str(exc))
    store = CredentialStore(vault, cfg.config_dir / "connections.json")
    return AccountsService(config, store, check_api_key)


def connection_routes(auth: params.Depends) -> APIRouter:
    """The ``/connections`` endpoints, behind the gateway's auth dependency."""
    router = APIRouter(prefix="/connections", dependencies=[auth])

    @router.get("")
    def connections(a: AccountsDep) -> list[ConnectionOut]:
        return [
            ConnectionOut(
                provider=s.provider,
                connected=s.connected,
                method=s.method,
                connected_at=s.connected_at.isoformat() if s.connected_at else None,
                methods=list(s.methods),
                models=list(s.models),
            )
            for s in a.status()
        ]

    @router.put("/{provider}", status_code=204)
    async def connect(provider: str, body: ApiKeyIn, a: AccountsDep) -> None:
        with account_errors(provider):
            await a.connect_api_key(provider, body.api_key)

    @router.delete("/{provider}", status_code=204)
    def disconnect(provider: str, a: AccountsDep) -> None:
        with account_errors(provider):
            a.disconnect(provider)

    @router.post("/{provider}/check")
    async def check(provider: str, a: AccountsDep) -> CheckOut:
        try:
            with account_errors(provider):
                await a.check(provider)
        except HTTPException as exc:
            if exc.status_code in (422, 502):
                return CheckOut(ok=False, detail=str(exc.detail))
            raise
        return CheckOut(ok=True)

    return router


@contextmanager
def account_errors(provider: str) -> Iterator[None]:
    """Plain, actionable errors. Messages never contain the key."""
    try:
        yield
    except UnknownProviderError as exc:
        raise HTTPException(404, f"unknown provider {provider!r}") from exc
    except NotConnectedError as exc:
        raise HTTPException(409, f"{provider} is not connected: run /login {provider}") from exc
    except VaultUnavailableError as exc:
        raise HTTPException(503, str(exc)) from exc
    except VaultCorruptError as exc:
        raise HTTPException(500, str(exc)) from exc
    except AuthError as exc:
        raise HTTPException(422, f"{provider} rejected this key") from exc
    except BrainError as exc:
        raise HTTPException(502, f"could not check the key with {provider} ({exc.kind})") from exc
