"""``enzo-server`` entry point: run the gateway with uvicorn."""

import sys

import uvicorn

from enzo.accounts.vault import generate_key
from enzo.config import Settings
from enzo.gateway.app import create_app


def main() -> None:
    if "--new-vault-key" in sys.argv:
        # A fresh key for the encrypted vault; store it as ENZO_VAULT_KEY, never in the repo.
        print(generate_key())
        return
    settings = Settings()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
