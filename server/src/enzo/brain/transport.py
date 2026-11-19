"""HTTP settings for brain calls: notice a dead network in seconds, not after ten minutes (#46).

The provider SDKs wait 600 s for the next byte of a reply. When Wi-Fi drops or the laptop changes
network, the old connection never gets closed by the other side, so a turn hung for ten minutes.

- **TCP keepalive** finds a dead link: after 10 s without traffic the OS probes the connection
  every 5 s, and after 3 unanswered probes (about 25 s) the read fails. A model that is only
  thinking keeps answering probes, so a long silent reasoning step is never cut off.
- **Read timeout** is the backstop for a server that is alive but stuck: 300 s without a byte, the
  same idle limit OpenAI's Codex uses (`DEFAULT_STREAM_IDLE_TIMEOUT_MS`, read 2026-11-19).
- **Connect timeout** 15 s (the SDKs use 5 s, too short for a slow or captive network, #45).
"""

import socket

import httpx2

KEEPALIVE_IDLE_S = 10
KEEPALIVE_INTERVAL_S = 5
KEEPALIVE_PROBES = 3
TIMEOUT = httpx2.Timeout(connect=15.0, read=300.0, write=60.0, pool=15.0)
LIMITS = httpx2.Limits(max_connections=100, max_keepalive_connections=20)


def keepalive_options() -> list[tuple[int, int, int]]:
    """Socket options for TCP keepalive, with the names each OS uses (macOS: TCP_KEEPALIVE)."""
    options = [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)]
    idle = getattr(socket, "TCP_KEEPIDLE", None) or getattr(socket, "TCP_KEEPALIVE", None)
    for name, value in (
        (idle, KEEPALIVE_IDLE_S),
        (getattr(socket, "TCP_KEEPINTVL", None), KEEPALIVE_INTERVAL_S),
        (getattr(socket, "TCP_KEEPCNT", None), KEEPALIVE_PROBES),
    ):
        if name is not None:
            options.append((socket.IPPROTO_TCP, name, value))
    return options


def brain_http_client() -> httpx2.AsyncClient:
    """A client for one brain's SDK: keepalive on every connection, the timeouts above."""
    transport = httpx2.AsyncHTTPTransport(socket_options=keepalive_options(), limits=LIMITS)
    return httpx2.AsyncClient(transport=transport, timeout=TIMEOUT, follow_redirects=True)
