"""Brain calls over real sockets: a dead or stuck reply fails instead of hanging (#46)."""

import asyncio
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx2
import openai
import pytest

from enzo.brain import Message, ProviderTimeoutError, TextDelta
from enzo.brain.adapters.openai_brain import OpenAIBrain
from enzo.brain.config import load_config
from enzo.brain.factory import build_brain
from enzo.brain.transport import (
    KEEPALIVE_IDLE_S,
    KEEPALIVE_INTERVAL_S,
    KEEPALIVE_PROBES,
    brain_http_client,
)
from tests.brain.test_openai_brain import TEXT, response, sse
from tests.fakes import fake_key


@asynccontextmanager
async def stalling_server() -> AsyncIterator[int]:
    """Answers with the first words of a streamed reply, then sends nothing more, forever."""
    first_words = sse(response([TEXT]))
    first_words = first_words[: first_words.find(b"event: response.output_item.done")]
    held: list[asyncio.StreamWriter] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        length = next(
            (
                int(line.split(b":")[1])
                for line in head.split(b"\r\n")
                if line.lower().startswith(b"content-length")
            ),
            0,
        )
        await reader.readexactly(length)
        writer.write(
            b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\nconnection: close\r\n\r\n"
            + first_words
        )
        await writer.drain()
        held.append(writer)  # never closed: the stream just goes quiet

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield port
    finally:
        for writer in held:
            writer.close()
        server.close()


async def test_a_reply_that_goes_quiet_fails_instead_of_hanging() -> None:
    async with stalling_server() as port:
        client = openai.AsyncOpenAI(
            api_key=fake_key("openai"),
            base_url=f"http://127.0.0.1:{port}/v1",
            timeout=httpx2.Timeout(connect=2.0, read=0.5, write=2.0, pool=2.0),
            http_client=brain_http_client(),
        )
        brain = OpenAIBrain(model="gpt-test", client=client)
        seen: list[str] = []

        async def read() -> None:
            async with asyncio.timeout(10):  # the regression: this used to wait 600 s
                async for event in brain.stream([Message.user("story")], max_output_tokens=50):
                    if isinstance(event, TextDelta):
                        seen.append(event.text)

        with pytest.raises(ProviderTimeoutError):
            await read()
        assert "".join(seen) == "Hello"  # the words that arrived before it went quiet


async def test_every_brain_connection_has_tcp_keepalive() -> None:
    async with stalling_server() as port, brain_http_client() as http:
        request = http.build_request("POST", f"http://127.0.0.1:{port}/", content=b"{}")
        response = await http.send(request, stream=True)
        try:
            sock = response.extensions["network_stream"].get_extra_info("socket")
            assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) != 0
            idle = getattr(socket, "TCP_KEEPIDLE", None) or getattr(socket, "TCP_KEEPALIVE", None)
            if idle is not None:
                assert sock.getsockopt(socket.IPPROTO_TCP, idle) == KEEPALIVE_IDLE_S
            if hasattr(socket, "TCP_KEEPINTVL"):
                assert (
                    sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL)
                    == KEEPALIVE_INTERVAL_S
                )
            if hasattr(socket, "TCP_KEEPCNT"):
                assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT) == KEEPALIVE_PROBES
        finally:
            await response.aclose()


@pytest.mark.parametrize("name", ["gpt-sol", "claude-sonnet"])
def test_brains_wait_long_enough_to_connect_and_never_ten_minutes_for_a_byte(name: str) -> None:
    cfg = load_config().brains[name]
    brain = build_brain(cfg, keys=lambda _p: fake_key("key"))
    timeout = brain._client.timeout  # type: ignore[attr-defined]
    assert isinstance(timeout, httpx2.Timeout)
    assert (timeout.connect, timeout.read) == (15.0, 300.0)
