"""Chaos tests (spec §64): RabbitMQ goes away while a Python service publishes
through its outbox and consumes. The server keeps running behind a CutProxy,
so what it holds can be checked during the outage. The twin of gokit's
events/chaos_integration_test.go."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from agenttwin_core import events
from agenttwin_core.db import Migrator, connect, load_migrations, transaction
from agenttwin_core.events import Broker, Envelope, OutboxRelay, outbox_backlog, write_outbox
from agenttwin_core.ids import new_id
from agenttwin_core.testing import amqp_url, cut_proxy, temp_database

from .test_events_integration import OUTBOX_DDL, QUEUE, purge, queue_depth, run_requested

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


async def eventually(what: str, cond: Callable[[], Awaitable[bool]], within: float = 30.0) -> None:
    deadline = time.monotonic() + within
    while not await cond():
        if time.monotonic() > deadline:
            pytest.fail(f"timed out after {within}s waiting for {what}")
        await asyncio.sleep(0.05)


async def test_a_rabbitmq_outage_delays_events_and_loses_none(tmp_path: Path) -> None:
    url = amqp_url()
    await purge(url, QUEUE, QUEUE + ".retry", QUEUE + ".dlq")
    d = tmp_path / "m"
    d.mkdir()
    (d / "0001_init.up.sql").write_text(OUTBOX_DDL)
    async with temp_database() as db_url, cut_proxy(url) as proxy:
        pool = await connect(db_url, schema="svc", max_size=4)
        broker = Broker(proxy.url(url))
        await broker.connect()
        seen: Counter[str] = Counter()

        async def handler(env: Envelope) -> None:
            seen[env.id] += 1

        stop = asyncio.Event()
        consumer = asyncio.create_task(broker.consume(QUEUE, handler, concurrency=4, stop=stop))
        try:
            await Migrator(pool, "svc", load_migrations(d)).up()
            relay = OutboxRelay(pool, "svc", broker)

            async def write(n: int) -> list[str]:
                ids = []
                for _ in range(n):
                    env = run_requested(new_id())
                    async with transaction(pool) as conn:
                        await write_outbox(conn, "svc", env)
                    ids.append(env.id)
                return ids

            async def drained() -> bool:
                with contextlib.suppress(Exception):  # a failing tick is what an outage looks like
                    await relay.tick()
                return await outbox_backlog(pool, "svc") == 0

            before = await write(5)
            await eventually("the outbox to drain", drained)
            await eventually("the events before the outage", lambda: _all(seen, before))

            # RabbitMQ goes away.
            proxy.cut()

            async def not_ready() -> bool:
                try:
                    await broker.ping()
                except ConnectionError:
                    return True
                return False

            await eventually("readiness to report the outage", not_ready, within=10)
            during = await write(5)
            for _ in range(3):
                started = time.monotonic()
                with pytest.raises(Exception):  # noqa: B017 - whatever the client raises, the tick fails
                    await relay.tick()
                assert time.monotonic() - started < 15, "a tick during the outage must fail, not hang"
            assert await outbox_backlog(pool, "svc") == len(during)
            async with pool.connection() as conn:
                row = await (
                    await conn.execute(
                        "SELECT count(*) AS n FROM outbox WHERE published_at IS NULL "
                        "AND last_error IS NOT NULL AND attempts >= 3"
                    )
                ).fetchone()
            assert row is not None and row["n"] == 1, "the head of the queue shows the failed attempts"
            assert not any(seen[i] for i in during)

            # It comes back.
            proxy.restore()

            async def ready() -> bool:
                return not await not_ready()

            await eventually("readiness to recover", ready)
            await eventually("the outbox to drain after the outage", drained)
            await eventually("the events written during the outage", lambda: _all(seen, during))
            assert all(seen[i] >= 1 for i in before + during)
            assert await queue_depth(url, QUEUE + ".dlq") == 0
            async with pool.connection() as conn:
                row = await (
                    await conn.execute(
                        "SELECT count(*) AS n FROM outbox "
                        "WHERE published_at IS NOT NULL AND last_error IS NULL"
                    )
                ).fetchone()
            assert row is not None and row["n"] == len(before) + len(during)
        finally:
            stop.set()
            await asyncio.wait_for(consumer, timeout=30)
            await broker.close()
            await pool.close()


async def test_a_consumer_keeps_trying_through_a_partition(monkeypatch: pytest.MonkeyPatch) -> None:
    """A partition accepts connections and never answers them. The consumer
    gives up on each attempt and tries again, instead of waiting forever on
    the first, and consumes once the partition heals."""
    monkeypatch.setattr(events, "CONNECT_TIMEOUT_S", 0.5)
    url = amqp_url()
    await purge(url, QUEUE, QUEUE + ".retry", QUEUE + ".dlq")
    async with cut_proxy(url) as proxy:
        broker = Broker(proxy.url(url))  # consume only: every dial is the consumer's
        seen: Counter[str] = Counter()

        async def handler(env: Envelope) -> None:
            seen[env.id] += 1

        stop = asyncio.Event()
        consumer = asyncio.create_task(broker.consume(QUEUE, handler, stop=stop))
        try:

            async def consuming() -> bool:
                return proxy.open == 1

            await eventually("the consumer to connect", consuming, within=10)
            proxy.silence()
            dials = proxy.accepted

            async def retried() -> bool:
                return proxy.accepted >= dials + 3

            await eventually("the consumer to retry through the partition", retried, within=10)
            proxy.restore()
            publisher = Broker(url)
            await publisher.connect()
            env = run_requested(new_id())
            await publisher.publish(env)
            await publisher.close()
            await eventually("the event published after the partition", lambda: _all(seen, [env.id]))
        finally:
            stop.set()
            await asyncio.wait_for(consumer, timeout=30)


async def _all(seen: Counter[str], ids: list[str]) -> bool:
    return all(seen[i] for i in ids)
