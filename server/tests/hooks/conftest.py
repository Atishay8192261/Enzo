"""Watch tests never touch real DNS: a page's host resolves to a public address, with no proxy.

Without this, the page guard (``enzo.tools.web``) resolves ``shop.example`` for real when no proxy
is set (CI), or skips the lookup when one is (a cloud session), so a test would pass in one place
and fail in the other.
"""

import ipaddress

import pytest

import enzo.tools.web as w


@pytest.fixture(autouse=True)
def public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    async def resolve(host: str, _port: int) -> set[w.IP] | None:
        table = {"localhost": "127.0.0.1", "router.home": "192.168.1.1", "nowhere.test": None}
        ip = table.get(host, "93.184.215.14")
        return None if ip is None else {ipaddress.ip_address(ip)}

    monkeypatch.setattr(w, "_resolve", resolve)
    monkeypatch.setattr(w, "_proxied", lambda _host: False)
