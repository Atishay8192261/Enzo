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
    #: Search by meaning (ADR 0030): "local" runs a small model on this computer (downloaded
    #: once, nothing sent anywhere); "off" finds memories and past chats by their words only.
    embeddings: Literal["local", "off"] = "local"
    #: Show reminders as desktop notifications too (ENZO_NOTIFY=0 turns them off).
    notify: bool = True
    #: A Google OAuth client (Desktop app type) for Gmail and Calendar, when it is not set up in
    #: the app (/login google). An installed app's client secret is not secret (ADR 0026).
    google_client_id: str | None = None
    google_client_secret: SecretStr | None = None
    #: The browser Enzo drives (ADR 0027). None: the installed Google Chrome, Edge or Chromium.
    browser_path: Path | None = None
    #: Show the browser window (False) or keep it hidden (True). None: shown where there is a
    #: screen (a Mac, a Linux desktop), hidden on a server.
    browser_headless: bool | None = None
    #: Host names the gateway answers to (DNS-rebinding defence). Local only unless changed.
    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost", "[::1]", "::1")

    @model_validator(mode="after")
    def _prod_requires_a_token(self) -> Self:
        if self.env == "prod" and self.api_token is None:
            msg = "ENZO_API_TOKEN must be set when ENZO_ENV=prod"
            raise ValueError(msg)
        return self
