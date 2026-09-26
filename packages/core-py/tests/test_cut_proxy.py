"""The cut proxy chaos tests put between a client and a real server."""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from agenttwin_core.testing import CutProxy, cut_proxy

pytestmark = pytest.mark.anyio


async def _echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    with contextlib.suppress(OSError, ConnectionError):
        while line := await reader.readline():
            writer.write(line)
            await writer.drain()
    writer.close()


async def _round_trip(proxy: CutProxy, msg: bytes, timeout: float = 2.0) -> bytes:
    """The echoed line, or b"" when the connection ended (a reset or a close)."""
    reader, writer = await asyncio.open_connection(*proxy.address)
    try:
        writer.write(msg + b"\n")
        await writer.drain()
        return await asyncio.wait_for(reader.readline(), timeout)
    except ConnectionError:
        return b""
    finally:
        writer.close()


async def test_cut_silence_and_restore() -> None:
    server = await asyncio.start_server(_echo, "127.0.0.1", 0)
    host, port = server.sockets[0].getsockname()[:2]
    try:
        async with cut_proxy(f"amqp://u:p@{host}:{port}/vh") as proxy:
            assert await _round_trip(proxy, b"before") == b"before\n"

            # An open connection is dropped by a cut.
            reader, writer = await asyncio.open_connection(*proxy.address)
            writer.write(b"x\n")
            await writer.drain()
            assert await reader.readline() == b"x\n"
            assert proxy.open == 1
            proxy.cut()
            with contextlib.suppress(ConnectionError):
                assert await asyncio.wait_for(reader.read(), 2) == b""
            writer.close()
            # A new one ends before carrying anything.
            assert await _round_trip(proxy, b"refused") == b""

            # Silence: a new connection hangs until its caller gives up.
            proxy.silence()
            with pytest.raises(TimeoutError):
                await _round_trip(proxy, b"silent", timeout=0.3)

            proxy.restore()
            assert await _round_trip(proxy, b"after") == b"after\n"
            assert proxy.accepted == 5
            assert proxy.url(f"amqp://u:p@{host}:{port}/vh") == "amqp://u:p@{}:{}/vh".format(*proxy.address)
    finally:
        server.close()
        await server.wait_closed()
