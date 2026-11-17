"""FastAPI application factory for the agent gateway."""

from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel

from enzo import __version__
from enzo.config import Settings


class Health(BaseModel):
    """Liveness payload. Clients use it to confirm they reached a compatible gateway."""

    status: Literal["ok"]
    version: str
    env: str


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the gateway app. Takes settings explicitly so tests never read the real env."""
    cfg = settings or Settings()
    app = FastAPI(title="Enzo Gateway", version=__version__)

    @app.get("/healthz")
    def healthz() -> Health:
        return Health(status="ok", version=__version__, env=cfg.env)

    return app
