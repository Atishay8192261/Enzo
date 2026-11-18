"""Runtime configuration, loaded from environment variables prefixed with ``ENZO_``."""

from pathlib import Path
from typing import Literal, Self

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process-wide settings. Secrets never live here; they come from the vault layer."""

    model_config = SettingsConfigDict(env_prefix="ENZO_", env_file=".env", extra="ignore")

    env: Literal["dev", "test", "prod"] = "dev"
    host: str = "127.0.0.1"
    port: int = 8787
    #: Local development default. Real deployments set ENZO_DATABASE_URL.
    database_url: str = "postgresql+asyncpg://enzo:enzo@localhost:5432/enzo"
    #: Bearer token every client must send. Required in prod; optional for local dev and tests.
    api_token: SecretStr | None = None
    #: Brains and routing file; None uses the default shipped with Enzo.
    brains_config: Path | None = None
    #: Where Enzo keeps its local state (connection index, encrypted vault).
    config_dir: Path = Path.home() / ".config" / "enzo"
    #: The user-visible folder for files Enzo reads and writes (spec §11.2).
    workspace_dir: Path = Path.home() / "Enzo"
    #: Key for the encrypted credential vault on machines without an OS keychain (§11.2).
    vault_key: SecretStr | None = None
    #: Also use provider keys from environment variables (OPENAI_API_KEY, …). Off by default: a key
    #: in the shell must never be used, and billed, while the app says "not connected" (#24).
    use_env_keys: bool = False

    @model_validator(mode="after")
    def _prod_requires_a_token(self) -> Self:
        if self.env == "prod" and self.api_token is None:
            msg = "ENZO_API_TOKEN must be set when ENZO_ENV=prod"
            raise ValueError(msg)
        return self
