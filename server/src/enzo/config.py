"""Runtime configuration, loaded from environment variables prefixed with ``ENZO_``."""

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process-wide settings. Secrets never live here; they come from the vault layer."""

    model_config = SettingsConfigDict(env_prefix="ENZO_", env_file=".env", extra="ignore")

    env: Literal["dev", "test", "prod"] = "dev"
    host: str = "127.0.0.1"
    port: int = 8787
    #: Local development default. Real deployments set ENZO_DATABASE_URL.
    database_url: str = "postgresql+asyncpg://enzo:enzo@localhost:5432/enzo"
