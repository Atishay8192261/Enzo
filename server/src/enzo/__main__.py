"""``enzo-server`` entry point: run the gateway with uvicorn."""

import uvicorn

from enzo.config import Settings
from enzo.gateway.app import create_app


def main() -> None:
    settings = Settings()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
