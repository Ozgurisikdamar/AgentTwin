"""Events (ADR-0002, spec §36): the common envelope, schema validation,
RabbitMQ publishing with confirms, consumers with bounded retry and a parking
DLQ, the transactional outbox and consumer de-duplication.

Wire-compatible with ``gokit/events``: same exchange, queue arguments,
attempt header and DLQ/retry routing, so Go and Python services share one
topology (``packages/contracts/topology.json``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

import aio_pika
from aio_pika.abc import AbstractChannel, AbstractIncomingMessage, AbstractRobustConnection
from psycopg import sql

from agenttwin_core.db import Conn, Pool, jsonb
from agenttwin_core.ids import new_id
from agenttwin_core.logx import Log, get_logger
from agenttwin_core.schemas import event_payload_validator, load_topology
from agenttwin_core.telemetry import metrics

__all__ = [
    "ATTEMPT_HEADER",
    "Broker",
    "Envelope",
    "OutboxRelay",
    "Permanent",
    "Publisher",
    "claim_event",
    "outbox_backlog",
    "validate_envelope",
    "write_outbox",
]

ATTEMPT_HEADER = "x-agenttwin-attempt"
_log = get_logger("agenttwin.events")


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class Envelope:
    """The event wrapper of ``packages/contracts/events/envelope.v1``."""

    type: str
    organization_id: str
    payload: dict[str, Any]
    correlation_id: str
    project_id: str | None = None
    causation_id: str | None = None
    producer: str = ""
    id: str = field(default_factory=new_id)
    occurred_at: str = field(default_factory=lambda: _iso(datetime.now(UTC)))

    @classmethod
    def new(
        cls,
        event_type: str,
        producer: str,
        organization_id: str,
        project_id: str | None,
        correlation_id: str | None,
        payload: Mapping[str, Any],
        causation_id: str | None = None,
    ) -> Envelope:
        return cls(
            type=event_type,
            producer=producer,
            organization_id=organization_id,
            project_id=project_id or None,
            correlation_id=correlation_id or new_id(),
            causation_id=causation_id or None,
            payload=dict(payload),
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "type": self.type,
            "occurred_at": self.occurred_at,
            "organization_id": self.organization_id,
            "project_id": self.project_id,
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "payload": self.payload,
        }
        if self.producer:
            out["producer"] = self.producer
        return out

    def to_json(self) -> bytes:
        return json.dumps(self.to_dict(), ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Envelope:
        return cls(
            id=str(raw["id"]),
            type=str(raw["type"]),
            occurred_at=str(raw["occurred_at"]),
            organization_id=str(raw["organization_id"]),
            project_id=raw.get("project_id"),
            correlation_id=str(raw["correlation_id"]),
            causation_id=raw.get("causation_id"),
            producer=str(raw.get("producer") or ""),
            payload=dict(raw["payload"]),
        )


class InvalidEvent(ValueError):
    pass


def validate_envelope(raw: Mapping[str, Any]) -> Envelope:
    """Validates an envelope and its payload against the published contracts."""
    errors = sorted(event_payload_validator("envelope.v1").iter_errors(raw), key=lambda e: list(e.path))
    if errors:
        raise InvalidEvent(f"invalid envelope: {errors[0].message}")
    event_type = str(raw["type"])
    try:
        validator = event_payload_validator(event_type)
    except KeyError:
        raise InvalidEvent(f"unknown event type {event_type!r}") from None
    errors = sorted(validator.iter_errors(raw["payload"]), key=lambda e: list(e.path))
    if errors:
        raise InvalidEvent(f"invalid {event_type} payload: {errors[0].message}")
    return Envelope.from_dict(raw)


class Permanent(Exception):
    """Wraps a handler failure that must not be retried (parks the message)."""


class Publisher(Protocol):
    async def publish(self, env: Envelope) -> None: ...


# ---------------------------------------------------------------- outbox


async def write_outbox(conn: Conn, schema: str, env: Envelope) -> None:
    """Stores ``env`` in ``<schema>.outbox`` inside the caller's transaction, so
    the event is published if and only if the state change commits."""
    validate_envelope(env.to_dict())
    await conn.execute(
        sql.SQL("INSERT INTO {}.outbox (id, event_type, envelope) VALUES (%s, %s, %s)").format(
            sql.Identifier(schema)
        ),
        (env.id, env.type, jsonb(env.to_dict())),
    )


async def claim_event(conn: Conn, consumer: str, event_id: str) -> bool:
    """Records ``(consumer, event_id)``; False when it was already processed.
    Call inside the transaction that applies the event's effects."""
    cur = await conn.execute(
        "INSERT INTO processed_event (consumer, event_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (consumer, event_id),
    )
    return cur.rowcount == 1


async def outbox_backlog(pool: Pool, schema: str) -> int:
    async with pool.connection() as conn:
        cur = await conn.execute(
            sql.SQL("SELECT count(*) AS n FROM {}.outbox WHERE published_at IS NULL").format(
                sql.Identifier(schema)
            )
        )
        row = await cur.fetchone()
    return int(row["n"]) if row else 0


class OutboxRelay:
    """Publishes committed outbox rows with publisher confirms. Rows are locked
    with SKIP LOCKED so replicas never publish the same row concurrently; a
    crash between publish and commit republishes (consumers de-duplicate)."""

    def __init__(self, pool: Pool, schema: str, publisher: Publisher, batch_size: int = 50) -> None:
        self.pool = pool
        self.schema = schema
        self.publisher = publisher
        self.batch_size = batch_size

    async def tick(self) -> int:
        table = sql.SQL("{}.outbox").format(sql.Identifier(self.schema))
        published = 0
        failure: Exception | None = None
        async with self.pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                sql.SQL(
                    "SELECT id, envelope FROM {} WHERE published_at IS NULL ORDER BY created_at "
                    "LIMIT %s FOR UPDATE SKIP LOCKED"
                ).format(table),
                (self.batch_size,),
            )
            for row in await cur.fetchall():
                try:
                    await self.publisher.publish(Envelope.from_dict(row["envelope"]))
                except Exception as err:  # noqa: BLE001 - any failure stops the batch and is recorded
                    failure = err
                    await conn.execute(
                        sql.SQL(
                            "UPDATE {} SET attempts = attempts + 1, last_error = %s WHERE id = %s"
                        ).format(table),
                        (str(err)[:1000], row["id"]),
                    )
                    break
                await conn.execute(
                    sql.SQL(
                        "UPDATE {} SET published_at = now(), attempts = attempts + 1, last_error = NULL "
                        "WHERE id = %s"
                    ).format(table),
                    (row["id"],),
                )
                published += 1
        if failure is not None:
            raise failure
        return published

    async def run(self, stop: asyncio.Event, interval_s: float = 0.3) -> None:
        m = metrics()
        while not stop.is_set():
            try:
                n = await self.tick()
            except Exception as err:  # noqa: BLE001 - the relay must survive any tick failure
                n = 0
                if m is not None:
                    m.outbox_failures.labels(m.service).inc()
                _log.warn("outbox relay tick failed", schema=self.schema, error=str(err))
            if n:
                continue  # drain the backlog quickly
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval_s)


# ---------------------------------------------------------------- broker

Handler = Callable[[Envelope], Awaitable[None]]


class Broker:
    """One robust AMQP connection with a confirm-mode publishing channel.
    Consumers use their own connections so a slow handler never blocks
    publishing (outbox relay, retries)."""

    def __init__(self, url: str, *, connection_name: str = "agenttwin") -> None:
        self.url = url
        self.connection_name = connection_name
        self.topology = load_topology()
        self._conn: AbstractRobustConnection | None = None
        self._channel: AbstractChannel | None = None
        self._lock = asyncio.Lock()

    async def connect(self, attempts: int = 15, log: Log | None = None) -> None:
        backoff = 0.25
        for attempt in range(1, attempts + 1):
            try:
                self._conn = await aio_pika.connect_robust(
                    self.url, client_properties={"connection_name": self.connection_name}, timeout=10
                )
                self._channel = await self._conn.channel(publisher_confirms=True)
                await declare_topology(self._channel, self.topology)
                return
            except (aio_pika.exceptions.AMQPError, OSError, TimeoutError) as err:
                if attempt == attempts:
                    raise ConnectionError(f"connect rabbitmq: {err}") from err
                if log:
                    log.warn("rabbitmq not ready", attempt=attempt, error=str(err))
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 5.0)

    async def ping(self) -> None:
        if self._conn is None or self._conn.is_closed or self._channel is None or self._channel.is_closed:
            raise ConnectionError("rabbitmq connection is not open")

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()

    async def publish(self, env: Envelope) -> None:
        """Validates and publishes persistently; returns once the broker confirms."""
        validate_envelope(env.to_dict())
        await self._publish_raw(self.topology["exchange"], env.type, env.id, env.to_json(), None)

    async def _publish_raw(
        self,
        exchange_name: str,
        routing_key: str,
        message_id: str,
        body: bytes,
        headers: dict[str, Any] | None,
    ) -> None:
        if self._channel is None:
            raise ConnectionError("broker is not connected")
        msg = aio_pika.Message(
            body=body,
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            message_id=message_id,
            timestamp=datetime.now(UTC),
            headers=headers or {},
        )
        if exchange_name == "":
            exchange = self._channel.default_exchange
        else:
            exchange = await self._channel.get_exchange(exchange_name, ensure=False)
        await exchange.publish(msg, routing_key=routing_key, timeout=10)

    async def consume(
        self,
        queue: str,
        handler: Handler,
        *,
        concurrency: int = 4,
        timeout_s: float = 60.0,
        stop: asyncio.Event | None = None,
    ) -> None:
        """Consumes ``queue`` until ``stop`` is set, reconnecting on failures."""
        if queue not in self.topology["queues"]:
            raise ValueError(f"queue {queue!r} is not part of the topology contract")
        stop = stop or asyncio.Event()
        backoff = 0.5
        while not stop.is_set():
            try:
                await self._consume_once(queue, handler, concurrency, timeout_s, stop)
                backoff = 0.5
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - reconnect on any consumer failure
                if stop.is_set():
                    return
                _log.warn("consumer stopped; reconnecting", queue=queue, error=str(err))
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=backoff)
                backoff = min(backoff * 2, 10.0)

    async def _consume_once(
        self, queue: str, handler: Handler, concurrency: int, timeout_s: float, stop: asyncio.Event
    ) -> None:
        conn = await aio_pika.connect(
            self.url, client_properties={"connection_name": self.connection_name + "-consumer"}
        )
        try:
            channel = await conn.channel()
            await declare_topology(channel, self.topology)
            await channel.set_qos(prefetch_count=concurrency)
            q = await channel.get_queue(queue, ensure=False)
            sem = asyncio.Semaphore(concurrency)
            tasks: set[asyncio.Task[None]] = set()
            closed = asyncio.Event()
            conn.close_callbacks.add(lambda *_: closed.set())

            async def on_message(m: AbstractIncomingMessage) -> None:
                await self._dispatch(m, sem, tasks, queue, handler, timeout_s)

            consumer_tag = await q.consume(on_message)
            stopper = asyncio.create_task(stop.wait())
            closer = asyncio.create_task(closed.wait())
            try:
                await asyncio.wait({stopper, closer}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                stopper.cancel()
                closer.cancel()
            if not closed.is_set():
                await q.cancel(consumer_tag)
                if tasks:
                    await asyncio.wait(tasks, timeout=timeout_s)
            else:
                raise ConnectionError("consumer connection closed")
        finally:
            if not conn.is_closed:
                await conn.close()

    async def _dispatch(
        self,
        msg: AbstractIncomingMessage,
        sem: asyncio.Semaphore,
        tasks: set[asyncio.Task[None]],
        queue: str,
        handler: Handler,
        timeout_s: float,
    ) -> None:
        await sem.acquire()
        task = asyncio.create_task(self._handle(msg, queue, handler, timeout_s))
        tasks.add(task)

        def done(t: asyncio.Task[None]) -> None:
            tasks.discard(t)
            sem.release()

        task.add_done_callback(done)

    async def _handle(
        self, msg: AbstractIncomingMessage, queue: str, handler: Handler, timeout_s: float
    ) -> None:
        start = time.perf_counter()
        m = metrics()
        event_type = ""

        def observe(outcome: str) -> None:
            if m is not None:
                m.observe_event(event_type, outcome, time.perf_counter() - start)

        try:
            raw = json.loads(msg.body)
            if not isinstance(raw, dict):
                raise InvalidEvent("envelope must be a JSON object")
            env = validate_envelope(raw)
            event_type = env.type
        except (ValueError, KeyError, TypeError) as err:
            await self._park(msg, queue, f"invalid: {err}")
            observe("poison")
            return
        try:
            await asyncio.wait_for(handler(env), timeout=timeout_s)
        except Exception as err:  # noqa: BLE001 - classified below: retry or park
            attempt = _attempt(msg)
            if isinstance(err, Permanent) or attempt >= int(self.topology["max_attempts"]):
                await self._park(msg, queue, f"{type(err).__name__}: {err}")
                observe("dead_lettered")
                return
            headers = dict(msg.headers or {})
            headers[ATTEMPT_HEADER] = attempt + 1
            headers["x-agenttwin-last-error"] = f"{type(err).__name__}: {err}"[:500]
            try:
                await self._publish_raw("", queue + ".retry", msg.message_id or "", msg.body, headers)
            except Exception:  # noqa: BLE001 - could not schedule a retry: requeue, never lose it
                await msg.nack(requeue=True)
                observe("requeued")
                return
            await msg.ack()
            _log.warn(
                "event handling failed; retry scheduled",
                type=env.type,
                event_id=env.id,
                attempt=attempt,
                error=str(err)[:300],
            )
            observe("retry")
            return
        await msg.ack()
        observe("ok")

    async def _park(self, msg: AbstractIncomingMessage, queue: str, reason: str) -> None:
        headers = dict(msg.headers or {})
        headers["x-agenttwin-dead-reason"] = reason[:1000]
        try:
            await self._publish_raw("", queue + ".dlq", msg.message_id or "", msg.body, headers)
        except Exception:  # noqa: BLE001 - could not park: requeue, never lose it
            await msg.nack(requeue=True)
            return
        await msg.ack()
        _log.error("event parked in DLQ", queue=queue, message_id=msg.message_id, reason=reason[:300])


def _attempt(msg: AbstractIncomingMessage) -> int:
    value = (msg.headers or {}).get(ATTEMPT_HEADER)
    return value if isinstance(value, int) and value > 0 else 1


async def declare_topology(channel: AbstractChannel, topology: Mapping[str, Any]) -> None:
    """Declares the exchange, every consumer queue, its retry queue (TTL, then
    dead-lettered back into the consumer's own queue) and its parking DLQ —
    with arguments identical to gokit's DeclareTopology."""
    exchange = await channel.declare_exchange(topology["exchange"], aio_pika.ExchangeType.TOPIC, durable=True)
    for name in sorted(topology["queues"]):
        q = await channel.declare_queue(name, durable=True)
        await channel.declare_queue(
            name + ".retry",
            durable=True,
            arguments={
                "x-message-ttl": int(topology["retry_ttl_ms"]),
                "x-dead-letter-exchange": "",
                "x-dead-letter-routing-key": name,
            },
        )
        await channel.declare_queue(name + ".dlq", durable=True)
        for key in topology["queues"][name]:
            await q.bind(exchange, routing_key=key)
