"""Chaos tests (spec §64): PostgreSQL goes away under a running Python
service. The server keeps running behind a CutProxy; every Python service
uses this pool, this error rendering and this consumer. The twins of gokit's
service and events chaos tests."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import Request, Response
from psycopg import OperationalError
from psycopg.errors import AdminShutdown, QueryCanceled, UniqueViolation
from psycopg_pool import PoolTimeout

from agenttwin_core import events
from agenttwin_core.db import Pool, connect, is_unavailable, transaction
from agenttwin_core.events import ATTEMPT_HEADER, DEFERRED_HEADER, Broker, Envelope
from agenttwin_core.ids import new_id
from agenttwin_core.telemetry import setup_metrics
from agenttwin_core.testing import CutProxy, amqp_url, cut_proxy, temp_database
from agenttwin_core.web import Health, build_app

from .test_events_chaos_integration import eventually
from .test_events_integration import QUEUE, get_one, purge, queue_depth, run_requested

pytestmark = pytest.mark.anyio


def test_is_unavailable() -> None:
    lost = OperationalError("consuming input failed: server closed the connection unexpectedly")
    assert is_unavailable(lost)  # no SQLSTATE: the client lost the connection
    assert is_unavailable(PoolTimeout("couldn't get a connection after 10.00 sec"))
    assert is_unavailable(AdminShutdown("terminating connection due to administrator command"))
    assert not is_unavailable(QueryCanceled("canceling statement due to statement timeout"))
    assert not is_unavailable(UniqueViolation("duplicate key"))
    assert not is_unavailable(RuntimeError("a bug"))
    assert not is_unavailable(TimeoutError())


async def _pool_through(proxy: CutProxy, url: str) -> Pool:
    pool = await connect(proxy.url(url), schema="chaos", max_size=4)
    async with pool.connection() as conn:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS public.note (id serial PRIMARY KEY, body text NOT NULL)"
        )
    return pool


@pytest.mark.integration
async def test_a_database_outage_answers_503_and_recovers() -> None:
    async with temp_database() as url, cut_proxy(url) as proxy:
        pool = await _pool_through(proxy, url)
        health = Health()

        async def pg_check() -> None:
            async with pool.connection() as conn:
                await conn.execute("SELECT 1")

        health.add("postgres", pg_check)
        setup_metrics("chaos", "dev")
        app = build_app(service="chaos", version="dev", health=health, tokens=None, audience="chaos")

        @app.get("/public/notes")
        async def count() -> dict[str, Any]:
            async with pool.connection() as conn:
                row = await (await conn.execute("SELECT count(*) AS n FROM public.note")).fetchone()
            return {"notes": row["n"] if row else 0}

        @app.post("/public/notes")
        async def add(_: Request) -> Response:
            async with transaction(pool) as conn:
                await conn.execute("INSERT INTO public.note (body) VALUES ('n')")
            return Response(status_code=201)

        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://svc"
            ) as client:
                for method in ("POST", "GET"):
                    assert (await client.request(method, "/public/notes")).status_code < 300

                # PostgreSQL goes away: 503 with a retry hint, not 500, not a hang.
                proxy.cut()
                for method in ("GET", "POST", "GET", "POST"):
                    started = time.monotonic()
                    r = await client.request(method, "/public/notes")
                    assert r.status_code == 503, r.text
                    assert r.json()["error"]["code"] == "UNAVAILABLE"
                    assert r.headers["retry-after"] and r.headers["x-request-id"]
                    assert r.json()["error"]["request_id"] == r.headers["x-request-id"]
                    assert "closed" not in r.text and "connection" not in r.text.lower()
                    # Not the pool's 10 s wait, nor until the next reconnect
                    # attempt: the pool knows connecting fails.
                    assert time.monotonic() - started < 1.6, "a request during the outage must not hang"
                ready = await client.get("/health/ready")
                assert ready.status_code == 503 and ready.json()["checks"]["postgres"] == "unavailable"
                assert (await client.get("/health/live")).status_code == 200

                # It comes back: the pool replaces its dead connections.
                # A longer outage: reconnect attempts backing off without bound
                # would by now be 16 s apart.
                await asyncio.sleep(20)
                proxy.restore()

                async def recovered() -> bool:
                    return (await client.get("/public/notes")).status_code == 200

                # Reconnect attempts stay at most 2 s apart, however long the outage.
                restored = time.monotonic()
                await eventually("the service to recover", recovered, within=20)
                assert time.monotonic() - restored < 4, "the pool must notice the database is back"
                for i in range(20):
                    r = await client.request(("GET", "POST")[i % 2], "/public/notes")
                    assert r.status_code < 300, (i, r.text)
                assert (await client.get("/health/ready")).status_code == 200
                # Once the database is back, a busy pool waits for a connection
                # as usual instead of failing fast.
                held = [await pool.getconn() for _ in range(4)]

                async def release() -> None:
                    await asyncio.sleep(1.5)
                    for conn in held:
                        await pool.putconn(conn)

                releaser = asyncio.create_task(release())
                assert (await client.get("/public/notes")).status_code == 200
                await releaser
                # Only the writes answered 201 exist.
                assert (await client.get("/public/notes")).json() == {"notes": 1 + 10}
        finally:
            await pool.close()


class _Message:
    """Just enough of an incoming message for Broker._handle."""

    def __init__(self, env: Envelope, headers: dict[str, Any]) -> None:
        self.body = env.to_json()
        self.message_id = env.id
        self.headers = headers
        self.acked = self.nacked = 0

    async def ack(self) -> None:
        self.acked += 1

    async def nack(self, requeue: bool = True) -> None:
        self.nacked += 1


@pytest.mark.integration
async def test_retries_count_attempts_and_waiting_for_the_database_apart() -> None:
    url = amqp_url()
    await purge(url, QUEUE, QUEUE + ".retry", QUEUE + ".dlq")
    broker = Broker(url)
    await broker.connect()
    try:
        cases: list[tuple[str, Exception, int, int]] = [
            ("a failing handler", RuntimeError("a bug"), 4, 2),
            ("the database is down", OperationalError("server closed the connection unexpectedly"), 3, 3),
        ]
        for name, err, want_attempt, want_deferred in cases:
            env = run_requested(new_id())
            msg = _Message(env, {ATTEMPT_HEADER: 3, DEFERRED_HEADER: 2})

            async def fail(_: Envelope, err: Exception = err) -> None:
                raise err

            await broker._handle(msg, QUEUE, fail, 5.0)  # type: ignore[arg-type]
            assert (msg.acked, msg.nacked) == (1, 0), name
            retried = None
            for _ in range(50):
                if (retried := await get_one(url, QUEUE + ".retry")) is not None:
                    break
                await asyncio.sleep(0.1)
            assert retried is not None and retried.message_id == env.id, name
            headers = retried.headers or {}
            assert (headers[ATTEMPT_HEADER], headers.get(DEFERRED_HEADER)) == (want_attempt, want_deferred), (
                name
            )
    finally:
        await broker.close()


@pytest.mark.integration
async def test_a_database_outage_defers_events_instead_of_parking_them(tmp_path: Path) -> None:
    url = amqp_url()
    await purge(url, QUEUE, QUEUE + ".retry", QUEUE + ".dlq")
    async with temp_database() as db_url, cut_proxy(db_url) as proxy:
        pool = await _pool_through(proxy, db_url)
        async with pool.connection() as conn:
            await conn.execute("CREATE TABLE public.handled (event_id uuid PRIMARY KEY)")
        broker = Broker(url)
        await broker.connect()
        calls = 0

        async def handler(env: Envelope) -> None:
            nonlocal calls
            calls += 1
            async with transaction(pool) as conn:
                await conn.execute("INSERT INTO public.handled VALUES (%s) ON CONFLICT DO NOTHING", (env.id,))

        stop = asyncio.Event()
        consumer = asyncio.create_task(broker.consume(QUEUE, handler, concurrency=2, timeout_s=15, stop=stop))
        try:
            proxy.cut()
            # An event on its last attempt: one counted failure would park it.
            env = run_requested(new_id())
            max_attempts = int(broker.topology["max_attempts"])
            await broker._publish_raw("", QUEUE, env.id, env.to_json(), {ATTEMPT_HEADER: max_attempts})

            async def retried_twice() -> bool:
                return calls >= 2

            await eventually("the event to wait for the database twice", retried_twice, within=40)
            assert await queue_depth(url, QUEUE + ".dlq") == 0, (
                "an event was parked because the database was down"
            )

            # Waiting is bounded: an event that has waited the whole window is parked, saying why.
            late = run_requested(new_id())
            await broker._publish_raw(
                "", QUEUE, late.id, late.to_json(), {DEFERRED_HEADER: broker.max_deferrals}
            )

            async def parked() -> bool:
                return await queue_depth(url, QUEUE + ".dlq") == 1

            await eventually("the event that waited too long to be parked", parked, within=40)
            dead = await get_one(url, QUEUE + ".dlq")
            assert dead is not None and dead.message_id == late.id
            assert "database stayed unavailable" in str((dead.headers or {})["x-agenttwin-dead-reason"])
            assert broker.max_deferrals == events.DEFER_WINDOW_S * 1000 // int(
                broker.topology["retry_ttl_ms"]
            )

            proxy.restore()

            async def handled() -> bool:
                try:
                    async with pool.connection() as conn:
                        row = await (
                            await conn.execute(
                                "SELECT count(*) AS n FROM public.handled WHERE event_id = %s", (env.id,)
                            )
                        ).fetchone()
                except OperationalError:
                    return False
                return row is not None and row["n"] == 1

            await eventually("the event to be handled once the database is back", handled, within=40)
            assert await queue_depth(url, QUEUE + ".dlq") == 0
        finally:
            stop.set()
            await asyncio.wait_for(consumer, timeout=30)
            await broker.close()
            await pool.close()
