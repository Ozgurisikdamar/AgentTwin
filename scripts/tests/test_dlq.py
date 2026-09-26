"""scripts/dlq.py: what a dead letter says, and that sending one back loses
nothing — against a real RabbitMQ for the moving parts."""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
import sys
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aio_pika
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dlq import (
    DEAD_REASON,
    REPLAYED,
    REPLAYED_REASON,
    archive_record,
    depth,
    describe,
    drop,
    peek,
    queues,
    replay,
    replay_headers,
)

from agenttwin_core.testing import amqp_url

EVENT = {"id": "0190f3b4-0000-7000-8000-00000000abcd", "type": "trace.ingested.v1"}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_describe_reads_the_envelope_and_the_reason() -> None:
    d = describe(
        json.dumps(EVENT).encode(),
        {"x-agenttwin-attempt": 5, DEAD_REASON: b"RuntimeError: boom", REPLAYED: 2},
    )
    assert (d.event_type, d.event_id, d.attempts, d.replayed, d.reason) == (
        "trace.ingested.v1",
        EVENT["id"],
        5,
        2,
        "RuntimeError: boom",
    )


def test_describe_survives_what_is_not_an_event() -> None:
    for body in (b"\xff\xfe not json", b"[1, 2]", b'"text"'):
        d = describe(body, None)
        assert d.event_type == "(not an event envelope)"
        assert (d.event_id, d.attempts, d.replayed, d.reason) == ("", 1, 0, "")


def test_replay_headers_reset_the_attempts_and_count_the_replay() -> None:
    parked = {
        "x-agenttwin-attempt": 5,
        "x-agenttwin-deferred": 3,
        "x-agenttwin-last-error": "RuntimeError: boom",
        DEAD_REASON: "RuntimeError: boom",
        "x-death": [{"count": 1}],
        "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
    }
    out = replay_headers(parked)
    assert out == {
        "traceparent": parked["traceparent"],
        REPLAYED: 1,
        REPLAYED_REASON: "RuntimeError: boom",
    }
    assert parked[DEAD_REASON] == "RuntimeError: boom"  # the input is left alone
    assert replay_headers({REPLAYED: 1})[REPLAYED] == 2
    assert REPLAYED_REASON not in replay_headers({})
    assert len(replay_headers({DEAD_REASON: "x" * 5000})[REPLAYED_REASON]) == 1000


def test_the_archive_keeps_what_a_replay_by_hand_needs() -> None:
    when = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    headers = {
        DEAD_REASON: b"invalid: not json",
        "x-death": [{"count": 2, "time": when, "queue": b"graph-service.events"}],
    }
    record = archive_record("graph-service.events", "m1", b"not json", headers)
    json.dumps(record)  # plain JSON all the way down
    assert record["queue"] == "graph-service.events" and record["message_id"] == "m1"
    assert record["body"] == "not json" and "body_base64" not in record
    assert record["headers"] == {
        DEAD_REASON: "invalid: not json",
        "x-death": [{"count": 2, "time": when.isoformat(), "queue": "graph-service.events"}],
    }
    binary = archive_record("q", None, b"\xff\x00", None)
    assert "body" not in binary and base64.b64decode(binary["body_base64"]) == b"\xff\x00"


def test_queues_are_the_topology_contract() -> None:
    topology = Path(__file__).resolve().parents[2] / "packages" / "contracts" / "topology.json"
    assert queues() == sorted(json.loads(topology.read_text())["queues"])


# --- against RabbitMQ: queues of their own, so nothing else is touched ---


@pytest.fixture
async def conn() -> AsyncIterator[aio_pika.abc.AbstractRobustConnection]:
    c = await aio_pika.connect_robust(amqp_url())
    try:
        yield c
    finally:
        await c.close()


@pytest.fixture
async def queue(conn: aio_pika.abc.AbstractRobustConnection) -> AsyncIterator[str]:
    name = "test-dlq-" + secrets.token_hex(4)
    ch = await conn.channel()
    await ch.declare_queue(name, durable=True)
    await ch.declare_queue(name + ".dlq", durable=True)
    await ch.close()
    try:
        yield name
    finally:
        ch = await conn.channel()
        for n in (name, name + ".dlq"):
            await ch.queue_delete(n)
        await ch.close()


async def park(conn: aio_pika.abc.AbstractRobustConnection, queue: str, n: int) -> None:
    ch = await conn.channel(publisher_confirms=True)
    for i in range(n):
        body = json.dumps({**EVENT, "seq": i}).encode()
        headers: dict[str, Any] = {"x-agenttwin-attempt": 5, DEAD_REASON: f"RuntimeError: boom {i}"}
        await ch.default_exchange.publish(
            aio_pika.Message(body, headers=headers, message_id=f"m{i}"), routing_key=queue + ".dlq"
        )
    await ch.close()


async def drain(
    conn: aio_pika.abc.AbstractRobustConnection, name: str
) -> list[aio_pika.abc.AbstractIncomingMessage]:
    ch = await conn.channel()
    q = await ch.declare_queue(name, passive=True)
    out = []
    while (msg := await q.get(no_ack=True, fail=False)) is not None:
        out.append(msg)
    await ch.close()
    return out


@pytest.mark.integration
@pytest.mark.anyio
async def test_peek_leaves_the_messages_parked(
    conn: aio_pika.abc.AbstractRobustConnection, queue: str
) -> None:
    await park(conn, queue, 3)
    letters = await peek(conn, queue, 2)
    assert [d.reason for d in letters] == ["RuntimeError: boom 0", "RuntimeError: boom 1"]
    assert await depth(conn, queue + ".dlq") == 3
    assert await depth(conn, queue) == 0


@pytest.mark.integration
@pytest.mark.anyio
async def test_replay_sends_every_dead_letter_back_once(
    conn: aio_pika.abc.AbstractRobustConnection, queue: str
) -> None:
    await park(conn, queue, 3)
    assert await replay(conn, queue) == 3
    assert await depth(conn, queue + ".dlq") == 0
    back = await drain(conn, queue)
    assert [json.loads(m.body)["seq"] for m in back] == [0, 1, 2]  # in order
    assert [m.message_id for m in back] == ["m0", "m1", "m2"]
    for i, m in enumerate(back):
        headers = dict(m.headers)
        assert "x-agenttwin-attempt" not in headers and DEAD_REASON not in headers
        assert headers[REPLAYED] == 1
        assert headers[REPLAYED_REASON] == f"RuntimeError: boom {i}"
        assert m.delivery_mode == aio_pika.DeliveryMode.PERSISTENT


@pytest.mark.integration
@pytest.mark.anyio
async def test_replay_respects_the_limit(conn: aio_pika.abc.AbstractRobustConnection, queue: str) -> None:
    await park(conn, queue, 5)
    assert await replay(conn, queue, limit=2) == 2
    assert await depth(conn, queue + ".dlq") == 3
    assert await depth(conn, queue) == 2


@pytest.mark.integration
@pytest.mark.anyio
async def test_a_message_the_queue_refuses_stays_parked(
    conn: aio_pika.abc.AbstractRobustConnection, queue: str
) -> None:
    # A consumer queue that rejects every publish: the broker nacks the
    # confirm, and the dead letter must still be in the DLQ afterwards.
    ch = await conn.channel()
    await ch.queue_delete(queue)
    await ch.declare_queue(queue, durable=True, arguments={"x-max-length": 0, "x-overflow": "reject-publish"})
    await ch.close()
    await park(conn, queue, 2)
    with pytest.raises(aio_pika.exceptions.DeliveryError):
        await replay(conn, queue)
    assert await depth(conn, queue + ".dlq") == 2
    left = await drain(conn, queue + ".dlq")
    assert [m.message_id for m in left] == ["m0", "m1"]  # untouched, in order
    assert dict(left[0].headers)[DEAD_REASON] == "RuntimeError: boom 0"


@pytest.mark.integration
@pytest.mark.anyio
async def test_replay_needs_the_consumer_queue(
    conn: aio_pika.abc.AbstractRobustConnection, queue: str
) -> None:
    await park(conn, queue, 1)
    ch = await conn.channel()
    await ch.queue_delete(queue)
    await ch.close()
    with pytest.raises(LookupError):
        await replay(conn, queue)
    assert await depth(conn, queue + ".dlq") == 1
    assert await depth(conn, "test-dlq-missing-" + secrets.token_hex(4)) is None
    ch = await conn.channel()
    await ch.declare_queue(queue, durable=True)  # for the fixture's cleanup
    await ch.close()


@pytest.mark.integration
@pytest.mark.anyio
async def test_drop_archives_before_it_removes(
    conn: aio_pika.abc.AbstractRobustConnection, queue: str, tmp_path: Path
) -> None:
    await park(conn, queue, 3)
    archive = tmp_path / "dlq" / "dropped.jsonl"
    assert await drop(conn, queue, archive, limit=2) == 2
    assert await depth(conn, queue + ".dlq") == 1
    assert await depth(conn, queue) == 0  # dropped, not sent back
    records = [json.loads(line) for line in archive.read_text().splitlines()]
    assert [r["message_id"] for r in records] == ["m0", "m1"]
    assert [json.loads(r["body"])["seq"] for r in records] == [0, 1]
    assert records[0]["headers"][DEAD_REASON] == "RuntimeError: boom 0"
    # Appends: a second drop keeps the first one's records.
    assert await drop(conn, queue, archive) == 1
    assert len(archive.read_text().splitlines()) == 3
    assert await drop(conn, queue, archive) == 0


@pytest.mark.integration
@pytest.mark.anyio
async def test_a_drop_that_cannot_write_its_archive_removes_nothing(
    conn: aio_pika.abc.AbstractRobustConnection, queue: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import dlq

    await park(conn, queue, 2)

    def full_disk(fd: int) -> None:
        raise OSError(28, "No space left on device")

    # The message is taken from the DLQ, the write fails: it must go back.
    monkeypatch.setattr(dlq.os, "fsync", full_disk)
    with pytest.raises(OSError):
        await drop(conn, queue, tmp_path / "dropped.jsonl")
    assert await depth(conn, queue + ".dlq") == 2


# --- the command line, end to end (the argument wiring included) ---


@pytest.fixture
def cli_queue(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A queue pair of its own, made the only queue of the topology."""
    import dlq

    url = amqp_url()
    name = "test-dlq-cli-" + secrets.token_hex(4)

    async def setup() -> None:
        c = await aio_pika.connect(url)
        ch = await c.channel()
        await ch.declare_queue(name, durable=True)
        await ch.declare_queue(name + ".dlq", durable=True)
        await c.close()
        c2 = await aio_pika.connect_robust(url)
        await park(c2, name, 2)
        await c2.close()

    async def teardown() -> None:
        c = await aio_pika.connect(url)
        ch = await c.channel()
        for n in (name, name + ".dlq"):
            await ch.queue_delete(n)
        await c.close()

    monkeypatch.setenv("AMQP_URL", url)
    monkeypatch.setattr(dlq, "queues", lambda: [name])
    asyncio.run(setup())
    try:
        yield name
    finally:
        asyncio.run(teardown())


def depths(name: str) -> tuple[int | None, int | None]:
    async def go() -> tuple[int | None, int | None]:
        c = await aio_pika.connect_robust(amqp_url())
        try:
            return await depth(c, name), await depth(c, name + ".dlq")
        finally:
            await c.close()

    return asyncio.run(go())


@pytest.mark.integration
def test_cli_list_dry_run_replay(cli_queue: str, capsys: pytest.CaptureFixture[str]) -> None:
    import dlq

    assert dlq.main(["list", "--show", "1"]) == 0
    out = capsys.readouterr().out
    assert f"{cli_queue}.dlq: 2 messages" in out and "RuntimeError: boom 0" in out
    assert "… and 1 more" in out and "dead letters: 2" in out

    assert dlq.main(["replay", cli_queue, "--dry-run"]) == 0
    assert "dry run: 2 messages would be sent back" in capsys.readouterr().out
    assert depths(cli_queue) == (0, 2)

    assert dlq.main(["replay", cli_queue, "--limit", "1"]) == 0
    assert "sent 1 message back" in capsys.readouterr().out
    assert depths(cli_queue) == (1, 1)

    assert dlq.main(["replay", "no-such.events"]) == 2
    assert "unknown queue" in capsys.readouterr().err


@pytest.mark.integration
def test_cli_drop(cli_queue: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import dlq

    archive = tmp_path / "dropped.jsonl"
    assert dlq.main(["drop", cli_queue, "--archive", str(archive)]) == 0
    assert f"archived 2 messages to {archive}" in capsys.readouterr().out
    assert depths(cli_queue) == (0, 0)
    assert len(archive.read_text().splitlines()) == 2
