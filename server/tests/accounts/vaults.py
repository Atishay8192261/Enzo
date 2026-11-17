"""Shared helpers for account tests."""

from pathlib import Path

from enzo.accounts.store import CredentialStore
from enzo.accounts.vault import EncryptedFileVault, generate_key, open_vault


def file_store(config_dir: Path) -> CredentialStore:
    vault = open_vault(config_dir, generate_key())
    assert isinstance(vault, EncryptedFileVault)
    return CredentialStore(vault, config_dir / "connections.json")
