"""Secret storage. OS keychain when one exists, else an encrypted file; never plaintext (§11.2)."""

import base64
import contextlib
import json
import os
import secrets
import tempfile
from pathlib import Path
from typing import Protocol

import keyring
import keyring.backend
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

SERVICE = "enzo"
KEY_BYTES = 32
#: The vault key of an always-on Enzo (``enzo deploy``, ADR 0034): a machine that starts Enzo at
#: boot has no unlocked keychain, so the key lives in this owner-only file beside the vault.
KEY_FILE = "vault.key"


class VaultUnavailableError(RuntimeError):
    """No secure place to keep secrets. Enzo refuses to fall back to plaintext."""


class VaultCorruptError(RuntimeError):
    """The vault could not be decrypted: wrong key, or the file was changed."""


class Vault(Protocol):
    def get(self, name: str) -> str | None: ...
    def set(self, name: str, value: str) -> None: ...
    def delete(self, name: str) -> None: ...


class KeyringVault:
    """The OS keychain (macOS Keychain, Windows Credential Locker, Secret Service on Linux)."""

    def __init__(self, backend: keyring.backend.KeyringBackend) -> None:
        self._backend = backend

    def get(self, name: str) -> str | None:
        return self._backend.get_password(SERVICE, name)

    def set(self, name: str, value: str) -> None:
        self._backend.set_password(SERVICE, name, value)

    def delete(self, name: str) -> None:
        with contextlib.suppress(keyring.errors.PasswordDeleteError):
            self._backend.delete_password(SERVICE, name)


class EncryptedFileVault:
    """AES-256-GCM per entry; the entry name is the associated data, so entries can't be swapped."""

    def __init__(self, path: Path, key: bytes) -> None:
        if len(key) != KEY_BYTES:
            msg = f"vault key must be {KEY_BYTES} bytes"
            raise ValueError(msg)
        self._path = path
        self._aead = AESGCM(key)

    def get(self, name: str) -> str | None:
        entry = self._read().get(name)
        if entry is None:
            return None
        try:
            plain = self._aead.decrypt(_b64d(entry["nonce"]), _b64d(entry["data"]), name.encode())
        except InvalidTag as exc:
            msg = f"cannot decrypt vault entry {name!r}: wrong ENZO_VAULT_KEY or a modified file"
            raise VaultCorruptError(msg) from exc
        return plain.decode()

    def set(self, name: str, value: str) -> None:
        nonce = secrets.token_bytes(12)
        data = self._aead.encrypt(nonce, value.encode(), name.encode())
        entries = self._read()
        entries[name] = {"nonce": _b64e(nonce), "data": _b64e(data)}
        self._write(entries)

    def delete(self, name: str) -> None:
        entries = self._read()
        if entries.pop(name, None) is not None:
            self._write(entries)

    def _read(self) -> dict[str, dict[str, str]]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        entries: dict[str, dict[str, str]] = raw["entries"]
        return entries

    def _write(self, entries: dict[str, dict[str, str]]) -> None:
        write_private(self._path, json.dumps({"version": 1, "entries": entries}, indent=2))


def write_private(path: Path, text: str) -> None:
    """Atomic write, owner-only (0600) from the first byte: never a moment of wider permissions.

    ``mkstemp`` creates the file 0600 itself; ``os.replace`` keeps that mode on the final path.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def generate_key() -> str:
    """A new ENZO_VAULT_KEY value."""
    return _b64e(secrets.token_bytes(KEY_BYTES))


def open_vault(config_dir: Path, vault_key: str | None) -> Vault:
    """ENZO_VAULT_KEY → encrypted file; else the key file of an always-on Enzo → encrypted file;
    else a real OS keychain; else refuse (no plaintext)."""
    vault_key = vault_key or read_key_file(config_dir)
    if vault_key:
        return EncryptedFileVault(config_dir / "vault.json", _b64d(vault_key))
    backend = keyring.get_keyring()
    if _is_secure(backend):
        return KeyringVault(backend)
    msg = (
        "no secure place for credentials: this machine has no OS keychain. "
        "Set ENZO_VAULT_KEY (generate one with: enzo-server --new-vault-key), keep it secret."
    )
    raise VaultUnavailableError(msg)


def read_key_file(config_dir: Path) -> str | None:
    """The key in ``vault.key``, or None when there is none. A key other users of the computer
    could read is refused, as ssh refuses such a private key."""
    path = config_dir / KEY_FILE
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        return None
    if mode & 0o077:
        msg = f"{path} can be read by other users of this computer. Fix it with: chmod 600 {path}"
        raise VaultUnavailableError(msg)
    return path.read_text(encoding="utf-8").strip() or None


def create_key_file(config_dir: Path) -> Path:
    """A new key in ``vault.key`` (owner-only from the first byte). Never replaces one."""
    path = config_dir / KEY_FILE
    if path.exists():
        msg = f"{path} exists already"
        raise FileExistsError(msg)
    write_private(path, generate_key() + "\n")
    return path


def _is_secure(backend: keyring.backend.KeyringBackend) -> bool:
    module = type(backend).__module__
    if module.startswith(("keyring.backends.fail", "keyring.backends.null", "keyrings.alt")):
        return False  # "alt" backends include plaintext files
    return float(backend.priority) >= 1


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode()


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text.encode())
