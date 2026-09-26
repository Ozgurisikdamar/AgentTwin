"""Dead-letter queues: see what was parked and why, and send it back.

    make dlq                                   # every DLQ: depth, and per message its event and reason
    make dlq-replay QUEUE=graph-service.events # move that queue's dead letters back to it
    make dlq-replay QUEUE=... LIMIT=10 DRY_RUN=1
    make dlq-drop QUEUE=...                    # archive to a file, then remove (cannot succeed)

An event is parked in ``<queue>.dlq`` when its handler failed on every
attempt, when it is malformed, or when the database stayed down past the
deferral window (docs/runbooks/rabbitmq-down.md). Nothing consumes a DLQ:
parked events wait for a person. Fix the cause first (the reason is on each
message), then replay; a message that fails again is parked again after its
attempts, so a replay cannot loop.

Replaying moves messages one at a time: publish to the consumer queue with a
publisher confirm, then remove it from the DLQ. A message is never in
neither queue; if the script stops half-way, the rest stay parked. The
event itself is unchanged; its attempt counters are reset and the header
``x-agenttwin-replayed`` counts how often it was sent back, with the reason
it had been parked. Consumers deduplicate by event id, so an event that did
take effect before it was parked is not applied twice.

A message that can never succeed (not an event, or an event for something
that no longer exists) is dropped instead: each one is written to a JSON
Lines archive (body, headers, reason) and synced to disk before it leaves
the DLQ, so nothing disappears without a record.

It reaches RabbitMQ with ``AMQP_URL`` if set, otherwise with the stack's
``.env`` (``RABBITMQ_USER``, ``RABBITMQ_PASSWORD``, ``RABBITMQ_HOST_PORT``).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aio_pika
import aio_pika.exceptions
from aio_pika.abc import AbstractChannel, AbstractIncomingMessage, AbstractRobustConnection

ROOT = Path(__file__).resolve().parents[1]
TOPOLOGY = ROOT / "packages" / "contracts" / "topology.json"

REPLAYED = "x-agenttwin-replayed"
REPLAYED_REASON = "x-agenttwin-replayed-reason"
DEAD_REASON = "x-agenttwin-dead-reason"
# Bookkeeping of the failed deliveries: reset, so the event gets its full
# set of attempts again (packages/gokit/events/broker.go, agenttwin_core.events).
RESET = frozenset(
    {"x-agenttwin-attempt", "x-agenttwin-deferred", "x-agenttwin-last-error", DEAD_REASON, "x-death"}
)


@dataclass(frozen=True)
class DeadLetter:
    event_type: str
    event_id: str
    attempts: int
    replayed: int
    reason: str


def text(value: Any) -> str:
    if isinstance(value, bytes | bytearray):
        return bytes(value).decode("utf-8", "replace")
    return "" if value is None else str(value)


def number(value: Any) -> int:
    try:
        return int(text(value)) if not isinstance(value, int) else value
    except ValueError:
        return 0


def describe(body: bytes, headers: Mapping[str, Any] | None) -> DeadLetter:
    """What a parked message is and why it was parked."""
    headers = headers or {}
    try:
        env = json.loads(body)
    except ValueError:
        env = None
    if not isinstance(env, dict):
        env = {}
    return DeadLetter(
        event_type=text(env.get("type")) or "(not an event envelope)",
        event_id=text(env.get("id")),
        attempts=number(headers.get("x-agenttwin-attempt")) or 1,
        replayed=number(headers.get(REPLAYED)),
        reason=text(headers.get(DEAD_REASON)),
    )


def replay_headers(headers: Mapping[str, Any] | None) -> dict[str, Any]:
    """The headers of a message sent back to its queue."""
    headers = headers or {}
    out = {k: v for k, v in headers.items() if k not in RESET}
    out[REPLAYED] = number(headers.get(REPLAYED)) + 1
    reason = text(headers.get(DEAD_REASON))
    if reason:
        out[REPLAYED_REASON] = reason[:1000]
    return out


def plain(value: Any) -> Any:
    """A header value as JSON (AMQP tables carry bytes and timestamps)."""
    if isinstance(value, bytes | bytearray):
        return text(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [plain(v) for v in value]
    if value is None or isinstance(value, bool | int | float | str):
        return value
    return str(value)


def archive_record(
    queue: str, msg_id: str | None, body: bytes, headers: Mapping[str, Any] | None
) -> dict[str, Any]:
    """What is kept of a dropped message: enough to replay it by hand."""
    record: dict[str, Any] = {
        "archived_at": datetime.now(UTC).isoformat(),
        "queue": queue,
        "message_id": msg_id,
        "headers": plain(dict(headers or {})),
    }
    try:
        record["body"] = body.decode("utf-8")
    except UnicodeDecodeError:
        record["body_base64"] = base64.b64encode(body).decode("ascii")
    return record


def queues() -> list[str]:
    return sorted(json.loads(TOPOLOGY.read_text())["queues"])


def amqp_url() -> str:
    url = os.environ.get("AMQP_URL", "")
    if url:
        return url
    user = quote(os.environ.get("RABBITMQ_USER", "agenttwin"), safe="")
    password = quote(os.environ.get("RABBITMQ_PASSWORD", ""), safe="")
    port = os.environ.get("RABBITMQ_HOST_PORT", "5672")
    return f"amqp://{user}:{password}@127.0.0.1:{port}/"


async def depth(conn: AbstractRobustConnection, name: str) -> int | None:
    """Messages in ``name``; None if the queue does not exist (the services
    declare the topology when they start)."""
    # A failed passive declare closes its channel: one channel per question.
    channel = await conn.channel()
    try:
        q = await channel.declare_queue(name, passive=True)
    except aio_pika.exceptions.ChannelNotFoundEntity:
        return None
    count = int(q.declaration_result.message_count or 0)
    await channel.close()
    return count


async def peek(conn: AbstractRobustConnection, queue: str, limit: int) -> list[DeadLetter]:
    """Up to ``limit`` parked messages, left where they are."""
    channel = await conn.channel()
    try:
        dlq = await channel.declare_queue(queue + ".dlq", passive=True)
        out = []
        for _ in range(limit):
            msg = await dlq.get(no_ack=False, fail=False)
            if msg is None:
                break
            out.append(describe(msg.body, msg.headers))
        return out
    finally:
        # Unacknowledged messages go back to the queue when the channel closes.
        await channel.close()


async def replay(conn: AbstractRobustConnection, queue: str, limit: int = 0) -> int:
    """Moves up to ``limit`` (0: all) dead letters of ``queue`` back to it.

    Only the messages parked when it starts: one that fails again and is
    parked during the replay waits for the next one."""
    if await depth(conn, queue) is None:
        raise LookupError(f"queue {queue} does not exist: start the services first")
    todo = await depth(conn, queue + ".dlq") or 0
    if limit:
        todo = min(todo, limit)
    channel = await conn.channel(publisher_confirms=True, on_return_raises=True)
    try:
        dlq = await channel.declare_queue(queue + ".dlq", passive=True)
        moved = 0
        for _ in range(todo):
            msg = await dlq.get(no_ack=False, fail=False)
            if msg is None:
                break
            await send_back(channel, queue, msg)
            moved += 1
        return moved
    finally:
        await channel.close()


async def send_back(channel: AbstractChannel, queue: str, msg: AbstractIncomingMessage) -> None:
    out = aio_pika.Message(
        msg.body,
        headers=replay_headers(msg.headers),
        content_type=msg.content_type or "application/json",
        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        message_id=msg.message_id,
    )
    try:
        # Confirmed (and routed: on_return_raises) before the original goes.
        await channel.default_exchange.publish(out, routing_key=queue, mandatory=True, timeout=10)
    except BaseException:
        await msg.nack(requeue=True)
        raise
    await msg.ack()


async def drop(conn: AbstractRobustConnection, queue: str, archive: Path, limit: int = 0) -> int:
    """Removes up to ``limit`` (0: all) dead letters of ``queue``, each one
    written to ``archive`` (appended, fsynced) before it is acknowledged."""
    todo = await depth(conn, queue + ".dlq") or 0
    if limit:
        todo = min(todo, limit)
    if not todo:
        return 0
    archive.parent.mkdir(parents=True, exist_ok=True)
    channel = await conn.channel()
    try:
        dlq = await channel.declare_queue(queue + ".dlq", passive=True)
        dropped = 0
        with archive.open("a", encoding="utf-8") as out:
            for _ in range(todo):
                msg = await dlq.get(no_ack=False, fail=False)
                if msg is None:
                    break
                out.write(json.dumps(archive_record(queue, msg.message_id, msg.body, msg.headers)) + "\n")
                out.flush()
                os.fsync(out.fileno())
                await msg.ack()
                dropped += 1
        return dropped
    finally:
        await channel.close()


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def print_letters(queue: str, count: int, letters: list[DeadLetter]) -> None:
    print(f"{queue}.dlq: {plural(count, 'message')}")
    for d in letters:
        again = f", replayed {d.replayed}x" if d.replayed else ""
        print(f"  {d.event_type} {d.event_id} (attempts {d.attempts}{again})")
        print(f"    {d.reason or '(no reason recorded)'}")
    if count > len(letters):
        print(f"  … and {count - len(letters)} more")


async def main_async(args: argparse.Namespace) -> int:
    conn = await aio_pika.connect_robust(amqp_url(), timeout=10)
    try:
        if args.command == "list":
            total = 0
            for q in queues():
                n = await depth(conn, q + ".dlq")
                if n is None:
                    print(f"{q}.dlq: not declared yet (no service has started)")
                    continue
                total += n
                print_letters(q, n, await peek(conn, q, args.show) if n else [])
            print(f"dead letters: {total}")
            return 0
        if args.queue not in queues():
            print(f"unknown queue {args.queue!r}; one of: {', '.join(queues())}", file=sys.stderr)
            return 2
        if args.command == "drop":
            dropped = await drop(conn, args.queue, args.archive, args.limit)
            where = f"{args.queue}.dlq"
            print(f"archived {plural(dropped, 'message')} to {args.archive} and removed them from {where}")
            return 0
        if args.dry_run:
            n = await depth(conn, args.queue + ".dlq") or 0
            shown = min(n, args.limit) if args.limit else n
            print_letters(args.queue, n, await peek(conn, args.queue, shown))
            print(f"dry run: {plural(shown, 'message')} would be sent back to {args.queue}")
            return 0
        try:
            moved = await replay(conn, args.queue, args.limit)
        except LookupError as err:
            print(str(err), file=sys.stderr)
            return 2
        print(f"sent {plural(moved, 'message')} back to {args.queue}")
        return 0
    finally:
        await conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    ls = sub.add_parser("list", help="every DLQ with its messages (moves nothing)")
    ls.add_argument("--show", type=int, default=20, help="messages shown per queue")
    rp = sub.add_parser("replay", help="send one queue's dead letters back to it")
    rp.add_argument("queue", help="the consumer queue, e.g. graph-service.events")
    rp.add_argument("--limit", type=int, default=0, help="at most this many (0: all)")
    rp.add_argument("--dry-run", action="store_true", help="show what would be sent back")
    dp = sub.add_parser("drop", help="archive one queue's dead letters to a file, then remove them")
    dp.add_argument("queue", help="the consumer queue, e.g. graph-service.events")
    dp.add_argument("--limit", type=int, default=0, help="at most this many (0: all)")
    dp.add_argument(
        "--archive", type=Path, default=None, help="JSON Lines file (default: dist/dlq/<queue>-<time>.jsonl)"
    )
    args = parser.parse_args(argv)
    if args.command == "drop" and args.archive is None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        args.archive = ROOT / "dist" / "dlq" / f"{args.queue}-{stamp}.jsonl"
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
