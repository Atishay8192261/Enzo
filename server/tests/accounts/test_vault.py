"""The vault keeps secrets encrypted or in the OS keychain, and refuses anything weaker."""

import base64
import json
import os
import stat
from pathlib import Path
from typing import Any

import keyring
import keyring.backends.fail
import keyring.backends.null
import pytest

from enzo.accounts import vault as v
from enzo.accounts.vault import (
    EncryptedFileVault,
    KeyringVault,
    VaultCorruptError,
    VaultUnavailableError,
    generate_key,
    open_vault,
    write_private,
)

SECRET = "sk-test-0123456789abcdef"


def key() -> bytes:
    return base64.urlsafe_b64decode(generate_key())


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_round_trips_and_never_writes_plaintext(tmp_path: Path) -> None:
    path = tmp_path / "cfg" / "vault.json"
    vault = EncryptedFileVault(path, key())
    vault.set("provider:anthropic", SECRET)
    assert vault.get("provider:anthropic") == SECRET
    assert SECRET.encode() not in path.read_bytes()
    assert (mode(path), mode(path.parent)) == (0o600, 0o700)


def test_missing_entries_are_none_and_delete_is_idempotent(tmp_path: Path) -> None:
    vault = EncryptedFileVault(tmp_path / "vault.json", key())
    assert vault.get("provider:x") is None
    vault.delete("provider:x")
    vault.set("provider:x", SECRET)
    vault.delete("provider:x")
    assert vault.get("provider:x") is None


def test_a_wrong_key_fails_loudly(tmp_path: Path) -> None:
    path = tmp_path / "vault.json"
    EncryptedFileVault(path, key()).set("provider:x", SECRET)
    with pytest.raises(VaultCorruptError, match="wrong ENZO_VAULT_KEY"):
        EncryptedFileVault(path, key()).get("provider:x")


def test_a_modified_file_fails_loudly(tmp_path: Path) -> None:
    path = tmp_path / "vault.json"
    vault = EncryptedFileVault(path, key())
    vault.set("provider:x", SECRET)
    raw = json.loads(path.read_text())
    data = bytearray(base64.urlsafe_b64decode(raw["entries"]["provider:x"]["data"]))
    data[0] ^= 1
    raw["entries"]["provider:x"]["data"] = base64.urlsafe_b64encode(bytes(data)).decode()
    path.write_text(json.dumps(raw))
    with pytest.raises(VaultCorruptError):
        vault.get("provider:x")


def test_entries_cannot_be_swapped_between_names(tmp_path: Path) -> None:
    """The name is bound into the ciphertext: copying xai's entry under openai's name fails."""
    path = tmp_path / "vault.json"
    vault = EncryptedFileVault(path, key())
    vault.set("provider:xai", SECRET)
    raw = json.loads(path.read_text())
    raw["entries"]["provider:openai"] = raw["entries"]["provider:xai"]
    path.write_text(json.dumps(raw))
    with pytest.raises(VaultCorruptError):
        vault.get("provider:openai")


def test_each_write_uses_a_fresh_nonce(tmp_path: Path) -> None:
    path = tmp_path / "vault.json"
    vault = EncryptedFileVault(path, key())
    vault.set("provider:x", SECRET)
    first = json.loads(path.read_text())["entries"]["provider:x"]
    vault.set("provider:x", SECRET)
    second = json.loads(path.read_text())["entries"]["provider:x"]
    assert first["nonce"] != second["nonce"]


def test_the_key_must_be_32_bytes(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        EncryptedFileVault(tmp_path / "vault.json", b"short")


def test_a_failed_write_leaves_the_old_file_and_no_temp_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "index.json"
    write_private(path, "old")

    def broken(*_: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", broken)
    with pytest.raises(OSError, match="disk full"):
        write_private(path, "new")
    assert path.read_text() == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["index.json"]


def test_open_vault_uses_the_encrypted_file_when_a_key_is_set(tmp_path: Path) -> None:
    vault = open_vault(tmp_path, generate_key())
    assert isinstance(vault, EncryptedFileVault)
    vault.set("provider:x", SECRET)
    assert (tmp_path / "vault.json").exists()


class MemoryKeychain:
    """Stands in for a real OS keychain backend."""

    priority = 5

    def __init__(self) -> None:
        self.saved: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, name: str) -> str | None:
        return self.saved.get((service, name))

    def set_password(self, service: str, name: str, value: str) -> None:
        self.saved[(service, name)] = value

    def delete_password(self, service: str, name: str) -> None:
        if (service, name) not in self.saved:
            raise keyring.errors.PasswordDeleteError(name)
        del self.saved[(service, name)]


def test_open_vault_uses_a_real_keychain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    chain = MemoryKeychain()
    monkeypatch.setattr(keyring, "get_keyring", lambda: chain)
    vault = open_vault(tmp_path, None)
    assert isinstance(vault, KeyringVault)
    vault.set("provider:x", SECRET)
    assert chain.saved == {("enzo", "provider:x"): SECRET}
    assert vault.get("provider:x") == SECRET
    vault.delete("provider:x")
    vault.delete("provider:x")  # already gone: no error
    assert list(tmp_path.iterdir()) == []  # nothing written to disk


def plaintext_backend() -> object:
    cls = type("PlaintextKeyring", (MemoryKeychain,), {"__module__": "keyrings.alt.file"})
    return cls()


def weak_backend() -> object:
    cls = type("Weak", (MemoryKeychain,), {"priority": 0.5})
    return cls()


@pytest.mark.parametrize(
    "backend",
    [keyring.backends.fail.Keyring, keyring.backends.null.Keyring, plaintext_backend, weak_backend],
    ids=["fail", "null", "keyrings.alt plaintext", "low priority"],
)
def test_open_vault_refuses_insecure_backends(
    backend: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(keyring, "get_keyring", backend)
    with pytest.raises(VaultUnavailableError, match="enzo-server --new-vault-key"):
        open_vault(tmp_path, None)
    assert list(tmp_path.iterdir()) == []


def test_generated_keys_are_32_random_bytes() -> None:
    a, b = generate_key(), generate_key()
    assert a != b
    assert len(base64.urlsafe_b64decode(a)) == v.KEY_BYTES
