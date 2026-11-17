"""Connections: the secret lives only in the vault; the index file holds no secrets."""

import json
import stat
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from enzo.accounts.store import ApiKeyCredential
from tests.accounts.vaults import file_store
from tests.fakes import fake_key

KEY = fake_key("anthropic")


def credential(key: str = KEY) -> ApiKeyCredential:
    return ApiKeyCredential(api_key=SecretStr(key))


def test_connect_lists_and_returns_the_key(tmp_path: Path) -> None:
    store = file_store(tmp_path)
    connection = store.connect("anthropic", credential())
    assert store.api_key("anthropic") == KEY
    assert store.connections() == {"anthropic": connection}
    assert connection.method == "api_key"


def test_no_file_holds_the_key_in_plaintext(tmp_path: Path) -> None:
    store = file_store(tmp_path)
    store.connect("anthropic", credential())
    files = list(tmp_path.iterdir())
    assert {f.name for f in files} == {"vault.json", "connections.json"}
    for f in files:
        assert KEY.encode() not in f.read_bytes()
        assert stat.S_IMODE(f.stat().st_mode) == 0o600
    index = json.loads((tmp_path / "connections.json").read_text())
    assert set(index["anthropic"]) == {"method", "connected_at"}


def test_reconnecting_replaces_the_key(tmp_path: Path) -> None:
    store = file_store(tmp_path)
    store.connect("xai", credential())
    store.connect("xai", credential(fake_key("second")))
    assert store.api_key("xai") == fake_key("second")
    assert list(store.connections()) == ["xai"]


def test_disconnect_removes_the_secret_and_the_listing(tmp_path: Path) -> None:
    store = file_store(tmp_path)
    store.connect("anthropic", credential())
    store.connect("xai", credential(fake_key("xai")))
    store.disconnect("anthropic")
    store.disconnect("anthropic")  # twice is fine
    assert store.api_key("anthropic") is None
    assert list(store.connections()) == ["xai"]
    assert store.api_key("xai") == fake_key("xai")


def test_a_tampered_index_fails_loudly(tmp_path: Path) -> None:
    store = file_store(tmp_path)
    store.connect("anthropic", credential())
    path = tmp_path / "connections.json"
    index = json.loads(path.read_text())
    index["anthropic"]["api_key"] = "smuggled"
    path.write_text(json.dumps(index))
    with pytest.raises(ValidationError):
        store.connections()


def test_the_key_never_shows_in_reprs_or_dumps() -> None:
    c = credential()
    assert KEY not in repr(c)
    assert KEY not in str(c)
    assert KEY not in c.model_dump_json()


def test_short_keys_are_rejected() -> None:
    with pytest.raises(ValidationError):
        credential("short")
