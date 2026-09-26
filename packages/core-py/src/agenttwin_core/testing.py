"""Real-infrastructure helpers for integration tests (ADR-0012), shared by
the Python services' test suites: an isolated PostgreSQL database per test
and the RabbitMQ URL. Without the environment the tests skip — or fail when
``AGENTTWIN_REQUIRE_INTEGRATION=1`` so CI can never silently skip them.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql

__all__ = ["CutProxy", "amqp_url", "cut_proxy", "require_or_skip", "temp_database"]


def require_or_skip(what: str) -> None:
    import pytest

    if os.environ.get("AGENTTWIN_REQUIRE_INTEGRATION") == "1":
        pytest.fail(f"{what} is required for integration tests but not configured")
    pytest.skip(f"{what} not configured; skipping integration test")


def amqp_url() -> str:
    url = os.environ.get("AGENTTWIN_TEST_AMQP_URL", "")
    if not url:
        require_or_skip("AGENTTWIN_TEST_AMQP_URL")
    return url


@asynccontextmanager
async def temp_database() -> AsyncIterator[str]:
    """Creates a fresh database on AGENTTWIN_TEST_DATABASE_URL's server and
    yields its URL; it is dropped (with FORCE) afterwards."""
    base = os.environ.get("AGENTTWIN_TEST_DATABASE_URL", "")
    if not base:
        require_or_skip("AGENTTWIN_TEST_DATABASE_URL")
    name = "at_test_" + secrets.token_hex(6)
    async with await psycopg.AsyncConnection.connect(base, autocommit=True) as admin:
        await admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    parts = urlsplit(base)
    url = urlunsplit(parts._replace(path="/" + name))
    try:
        yield url
    finally:
        async with await psycopg.AsyncConnection.connect(base, autocommit=True) as admin:
            await admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


class CutProxy:
    """Forwards TCP connections to a real server and can cut them, the way an
    outage does (spec §64). ``cut`` drops every open connection and refuses
    new ones (a stopped server); ``silence`` drops them and accepts new ones
    that never answer (a partition: only a timeout ends the wait). The server
    keeps running, so what it holds can be inspected during the outage. The
    twin of gokit's testutil.CutProxy."""

    def __init__(self, target_host: str, target_port: int) -> None:
        self._target = (target_host, target_port)
        self._state = "forwarding"
        self._pairs: set[tuple[asyncio.StreamWriter, asyncio.StreamWriter]] = set()
        self._held: set[asyncio.StreamWriter] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self._server: asyncio.Server | None = None
        self.accepted = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._accept, "127.0.0.1", 0)

    @property
    def address(self) -> tuple[str, int]:
        if self._server is None:
            raise RuntimeError("the proxy is not started")
        host, port = self._server.sockets[0].getsockname()[:2]
        return host, port

    def url(self, raw: str) -> str:
        """``raw`` with its host and port replaced by the proxy's."""
        parts = urlsplit(raw)
        host, port = self.address
        userinfo = parts.netloc.rpartition("@")[0]
        netloc = (userinfo + "@" if userinfo else "") + f"{host}:{port}"
        return urlunsplit(parts._replace(netloc=netloc))

    @property
    def open(self) -> int:
        """Client connections currently forwarded."""
        return len(self._pairs)

    def cut(self) -> None:
        self._set("refusing")

    def silence(self) -> None:
        self._set("silent")

    def restore(self) -> None:
        self._set("forwarding")

    def _set(self, state: str) -> None:
        self._state = state
        if state != "forwarding":
            for client, upstream in list(self._pairs):
                _abort(client)
                _abort(upstream)
            self._pairs.clear()
        if state != "silent":
            for w in list(self._held):
                _abort(w)
            self._held.clear()

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
        self._set("refusing")
        self._set("forwarding")  # releases held connections
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._server is not None:
            await self._server.wait_closed()

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.accepted += 1
        if self._state == "refusing":
            _abort(writer)
            return
        if self._state == "silent":
            self._held.add(writer)
            return
        try:
            up_reader, up_writer = await asyncio.open_connection(*self._target)
        except OSError:
            _abort(writer)
            return
        if self._state != "forwarding":  # cut while dialing
            _abort(writer)
            _abort(up_writer)
            return
        pair = (writer, up_writer)
        self._pairs.add(pair)
        for src, dst in ((reader, up_writer), (up_reader, writer)):
            task = asyncio.create_task(self._pipe(src, dst, pair))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _pipe(
        self,
        src: asyncio.StreamReader,
        dst: asyncio.StreamWriter,
        pair: tuple[asyncio.StreamWriter, asyncio.StreamWriter],
    ) -> None:
        with contextlib.suppress(OSError, ConnectionError, asyncio.IncompleteReadError):
            while data := await src.read(65536):
                dst.write(data)
                await dst.drain()
        self._pairs.discard(pair)
        for w in pair:
            _abort(w)


def _abort(writer: asyncio.StreamWriter) -> None:
    transport = writer.transport
    if not transport.is_closing():
        transport.abort()


@asynccontextmanager
async def cut_proxy(url: str) -> AsyncIterator[CutProxy]:
    """A started CutProxy in front of the server named by ``url``."""
    parts = urlsplit(url)
    if parts.hostname is None or parts.port is None:
        raise ValueError(f"{url!r} names no host and port")
    proxy = CutProxy(parts.hostname, parts.port)
    await proxy.start()
    try:
        yield proxy
    finally:
        await proxy.close()
