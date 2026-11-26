"""The sites a browser task may use (ADR 0027).

The user approves a task with an exact list of sites. ``amazon.com`` covers its subdomains
(``www.amazon.com``, ``smile.amazon.com``) on the usual web ports. A site with a port
(``localhost:8851``) covers only that port, so approving a local test shop never opens the
user's database or Enzo's own gateway on the same machine.
"""

import ipaddress
import re
from collections.abc import Iterable
from urllib.parse import urlsplit

_LABEL = r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
_HOST = re.compile(rf"^(?=.{{1,253}}$){_LABEL}(\.{_LABEL})*$")
_DEFAULT_PORTS = {80, 443}


class SiteError(ValueError):
    """Not a site Enzo can use: said in plain words, for the brain to fix."""


def site_of(text: str) -> str:
    """``https://www.Shop.test:8851/cart`` → ``shop.test:8851``; ``amazon.com`` → ``amazon.com``."""
    raw = text.strip()
    parts = urlsplit(raw if "://" in raw else f"http://{raw}")
    if parts.scheme not in ("http", "https"):
        msg = f"{raw!r} is not a web address (only http and https)"
        raise SiteError(msg)
    if parts.username or parts.password:
        msg = f"{raw!r} carries a login; give the site without it"
        raise SiteError(msg)
    try:
        port = parts.port
    except ValueError as exc:
        msg = f"{raw!r} has a port that is not a number"
        raise SiteError(msg) from exc
    host = (parts.hostname or "").rstrip(".").removeprefix("www.")
    if not host or not (_HOST.match(host) or _ip(host)):
        msg = f"{raw!r} is not a site name like shop.example or localhost:8080"
        raise SiteError(msg)
    if _ip(host) and ":" in host:
        host = f"[{host}]"
    return f"{host}:{port}" if port and port not in _DEFAULT_PORTS else host


def allowed(url: str, sites: Iterable[str]) -> bool:
    """True when ``url`` is on one of ``sites`` (a subdomain counts; a port must match)."""
    try:
        here = site_of(url)
    except SiteError:
        return False
    host, port = split_site(here)
    for site in sites:
        s_host, s_port = split_site(site)
        if port != s_port:
            continue
        if host == s_host or (not _ip(s_host.strip("[]")) and host.endswith(f".{s_host}")):
            return True
    return False


def host_words(url: str) -> str:
    """The site of an address, as the "using your browser" sign names it."""
    try:
        return site_of(url)
    except SiteError:
        return "a page"


def split_site(site: str) -> tuple[str, int | None]:
    """``shop.test:8851`` → (``shop.test``, 8851); no port → None."""
    if site.startswith("["):
        host, _, rest = site.partition("]")
        return f"{host}]", int(rest[1:]) if rest.startswith(":") else None
    host, _, port = site.partition(":")
    return host, int(port) if port else None


def _ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True
