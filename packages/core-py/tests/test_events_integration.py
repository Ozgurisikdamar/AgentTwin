"""Broker, retry/DLQ, poison messages and the outbox against real RabbitMQ and
PostgreSQL. Wire compatibility with gokit is proven by declaring the topology
from Go and from Python in both orders (RabbitMQ rejects a redeclaration with
different arguments)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import aio_pika
import pytest

from agenttwin_core.db import Migrator, connect, load_migrations, transaction
from agenttwin_core.events import (
    ATTEMPT_HEADER,
    Broker,
    Envelope,
    InvalidEvent,
    OutboxRelay,
    Permanent,
    claim_event,
    outbox_backlog,
    write_outbox,
)
from agenttwin_core.ids import new_id
from agenttwin_core.schemas import load_topology
from agenttwin_core.testing import amqp_url, temp_database

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

QUEUE = "simulation-service.events"
OUTBOX_DDL = """
CREATE TABLE outbox (
    id uuid PRIMARY KEY,
    event_type text NOT NULL,
    envelope jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz,
    attempts int NOT NULL DEFAULT 0,
    last_error text
);
"""
PROCESSED_DDL = """
CREATE TABLE processed_event (
    consumer text NOT NULL,
    event_id uuid NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (consumer, event_id)
);
"""
ORG = "0190f3b4-0000-7000-8000-000000000001"
PROJ = "0190f3b4-0000-7000-8000-000000000002"


async def queue_depth(url: str, name: str) -> int:
    conn = await aio_pika.connect(url)
    try:
        ch = await conn.channel()
        q = await ch.declare_queue(name, passive=True)
        return int(q.declaration_result.message_count or 0)
    finally:
        await conn.close()


async def purge(url: str, *names: str) -> None:
    conn = await aio_pika.connect(url)
    try:
        ch = await conn.channel()
        for n in names:
            q = await ch.get_queue(n, ensure=False)
            await q.purge()
    finally:
        await conn.close()


async def get_one(url: str, name: str) -> aio_pika.abc.AbstractIncomingMessage | None:
    conn = await aio_pika.connect(url)
    try:
        ch = await conn.channel()
        q = await ch.get_queue(name, ensure=False)
        msg = await q.get(no_ack=True, fail=False)
        return msg
    finally:
        await conn.close()


def run_requested(run_id: str) -> Envelope:
    return Envelope.new(
        "simulation.run_requested.v1", "test", ORG, PROJ, None, {"run_id": run_id, "side": "SINGLE"}
    )


async def test_topology_is_wire_compatible_with_go(
    go_parity: Callable[[list[dict[str, Any]]], list[dict[str, Any]]],
) -> None:
    url = amqp_url()
    topo = load_topology()
    # Start from nothing, let Python declare, then Go redeclares (must match).
    conn = await aio_pika.connect(url)
    ch = await conn.channel()
    for q in topo["queues"]:
        for name in (q, q + ".retry", q + ".dlq"):
            await ch.queue_delete(name)
    await conn.close()
    b = Broker(url)
    await b.connect()
    await b.close()
    [res] = go_parity([{"op": "declare_topology", "input": url}])
    assert res.get("error") is None, res
    # And the other order: Go first, Python redeclares.
    conn = await aio_pika.connect(url)
    ch = await conn.channel()
    for q in topo["queues"]:
        for name in (q, q + ".retry", q + ".dlq"):
            await ch.queue_delete(name)
    await conn.close()
    [res] = go_parity([{"op": "declare_topology", "input": url}])
    assert res.get("error") is None, res
    b = Broker(url)
    await b.connect()
    await b.close()


async def test_publish_consume_retry_dlq_and_poison() -> None:
    url = amqp_url()
    broker = Broker(url)
    await broker.connect()
    await purge(url, QUEUE, QUEUE + ".retry", QUEUE + ".dlq")
    ok_id, flaky_id, bad_id = new_id(), new_id(), new_id()
    calls: dict[str, int] = {}
    done = asyncio.Event()
    ok_ids: set[str] = set()

    async def handler(env: Envelope) -> None:
        rid = env.payload["run_id"]
        calls[rid] = calls.get(rid, 0) + 1
        if rid == flaky_id and calls[rid] == 1:
            raise ConnectionError("transient")
        if rid == bad_id:
            raise Permanent("deterministic rejection")
        ok_ids.add(rid)
        if {ok_id, flaky_id} <= ok_ids:
            done.set()

    stop = asyncio.Event()
    task = asyncio.create_task(broker.consume(QUEUE, handler, concurrency=4, stop=stop))
    try:
        for rid in (ok_id, flaky_id, bad_id):
            await broker.publish(run_requested(rid))
        # A poison message goes straight to the DLQ, the handler never sees it.
        await broker._publish_raw("", QUEUE, "poison-1", b'{"not": "an envelope"}', None)
        await asyncio.wait_for(done.wait(), timeout=30)
        await asyncio.sleep(0.5)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=15)
    assert calls[ok_id] == 1
    assert calls[flaky_id] == 2  # retried once through the retry queue (TTL)
    assert calls[bad_id] == 1  # permanent errors are never retried
    dead = []
    while (msg := await get_one(url, QUEUE + ".dlq")) is not None:
        dead.append(msg)
    reasons = {m.message_id: str((m.headers or {}).get("x-agenttwin-dead-reason", "")) for m in dead}
    assert len(dead) == 2
    bad_env_id = next(mid for mid, r in reasons.items() if "deterministic rejection" in r)
    assert json.loads(next(m.body for m in dead if m.message_id == bad_env_id))["payload"]["run_id"] == bad_id
    assert reasons["poison-1"].startswith("invalid:")
    await broker.close()


async def test_exhausted_retries_are_parked() -> None:
    url = amqp_url()
    broker = Broker(url)
    await broker.connect()
    await purge(url, QUEUE, QUEUE + ".retry", QUEUE + ".dlq")
    # A message already at the maximum attempt is parked on its next failure.
    env = run_requested(new_id())
    max_attempts = int(load_topology()["max_attempts"])
    await broker._publish_raw("", QUEUE, env.id, env.to_json(), {ATTEMPT_HEADER: max_attempts})
    handled = asyncio.Event()

    async def failing(_: Envelope) -> None:
        handled.set()
        raise ConnectionError("still failing")

    stop = asyncio.Event()
    task = asyncio.create_task(broker.consume(QUEUE, failing, stop=stop))
    try:
        await asyncio.wait_for(handled.wait(), timeout=15)
        for _ in range(50):
            if await queue_depth(url, QUEUE + ".dlq") == 1:
                break
            await asyncio.sleep(0.1)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=15)
    assert await queue_depth(url, QUEUE + ".dlq") == 1
    assert await queue_depth(url, QUEUE + ".retry") == 0
    await broker.close()


async def test_invalid_events_are_never_published() -> None:
    broker = Broker(amqp_url())
    await broker.connect()
    bad = Envelope.new("simulation.run_requested.v1", "test", ORG, PROJ, None, {"run_id": "not-a-uuid"})
    with pytest.raises(InvalidEvent):
        await broker.publish(bad)
    unknown = Envelope.new("simulation.nonexistent.v1", "test", ORG, PROJ, None, {})
    with pytest.raises(InvalidEvent, match="unknown event type"):
        await broker.publish(unknown)
    await broker.close()


async def test_outbox_publishes_only_committed_events(tmp_path: Path) -> None:
    url = amqp_url()
    d = tmp_path / "m"
    d.mkdir()
    (d / "0001_init.up.sql").write_text(OUTBOX_DDL + PROCESSED_DDL)
    broker = Broker(url)
    await broker.connect()
    await purge(url, QUEUE, QUEUE + ".retry", QUEUE + ".dlq")
    async with temp_database() as db_url:
        pool = await connect(db_url, schema="svc_outbox", max_size=4)
        try:
            await Migrator(pool, "svc_outbox", load_migrations(d)).up()
            committed, rolled_back = run_requested(new_id()), run_requested(new_id())
            async with transaction(pool) as conn:
                await write_outbox(conn, "svc_outbox", committed)
            with pytest.raises(RuntimeError):
                async with transaction(pool) as conn:
                    await write_outbox(conn, "svc_outbox", rolled_back)
                    raise RuntimeError("business rule failed after writing the event")
            assert await outbox_backlog(pool, "svc_outbox") == 1
            relay = OutboxRelay(pool, "svc_outbox", broker)
            assert await relay.tick() == 1
            assert await relay.tick() == 0
            assert await outbox_backlog(pool, "svc_outbox") == 0
            msg = await get_one(url, QUEUE)
            assert msg is not None
            body = json.loads(msg.body)
            assert body["id"] == committed.id and body["payload"]["run_id"] == committed.payload["run_id"]
            assert await get_one(url, QUEUE) is None
            # Consumers de-duplicate redeliveries in their own transaction.
            async with transaction(pool) as conn:
                assert await claim_event(conn, "simulation-worker", committed.id) is True
                assert await claim_event(conn, "simulation-worker", committed.id) is False
                assert await claim_event(conn, "other-consumer", committed.id) is True
        finally:
            await pool.close()
    await broker.close()


async def test_relay_records_publish_failures(tmp_path: Path) -> None:
    class Failing:
        async def publish(self, env: Envelope) -> None:
            raise ConnectionError("broker down")

    d = tmp_path / "m"
    d.mkdir()
    (d / "0001_init.up.sql").write_text(OUTBOX_DDL)
    async with temp_database() as db_url:
        pool = await connect(db_url, schema="svc_fail", max_size=2)
        try:
            await Migrator(pool, "svc_fail", load_migrations(d)).up()
            async with transaction(pool) as conn:
                await write_outbox(conn, "svc_fail", run_requested(new_id()))
            with pytest.raises(ConnectionError):
                await OutboxRelay(pool, "svc_fail", Failing()).tick()
            async with pool.connection() as conn:
                row = await (
                    await conn.execute("SELECT attempts, last_error, published_at FROM outbox")
                ).fetchone()
            assert row == {"attempts": 1, "last_error": "broker down", "published_at": None}
        finally:
            await pool.close()
