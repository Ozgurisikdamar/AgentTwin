"""PostgreSQL access: an async connection pool per service schema and a
migrator compatible with ``gokit/db`` (same ledger table, checksum rule and
advisory lock key), so Go and Python services follow one migration discipline.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from psycopg import AsyncConnection, AsyncCursor, OperationalError, sql
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb
from psycopg.types.string import TextLoader
from psycopg_pool import AsyncConnectionPool, PoolTimeout
from psycopg_pool.base import AttemptWithBackoff

from agenttwin_core.logx import Log
from agenttwin_core.telemetry import metrics

__all__ = [
    "Conn",
    "Migration",
    "Migrator",
    "Pool",
    "connect",
    "is_unavailable",
    "jsonb",
    "load_migrations",
    "transaction",
]

Conn = AsyncConnection[DictRow]
Pool = AsyncConnectionPool[Conn]

_MIGRATION_NAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.(up|down)\.sql$")
_SCHEMA_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


# SQLSTATEs that mean the server is going away, starting or full, besides the
# connection-exception class 08 (gokit's db.IsUnavailable).
_UNAVAILABLE_STATES = frozenset({"57P01", "57P02", "57P03", "53300"})


def is_unavailable(exc: BaseException) -> bool:
    """Whether ``exc`` means PostgreSQL could not be reached or dropped the
    connection — the request may well succeed when retried — as opposed to an
    error in the request or the data. The HTTP edge answers these 503
    UNAVAILABLE, and consumers wait for the database instead of spending the
    event's attempts (spec §64). The twin of gokit's db.IsUnavailable."""
    if isinstance(exc, PoolTimeout):
        return True  # no connection could be had
    if not isinstance(exc, OperationalError):
        return False
    state = exc.sqlstate
    # No SQLSTATE: the client lost or never had the connection.
    return state is None or state.startswith("08") or state in _UNAVAILABLE_STATES


def jsonb(value: Any) -> Jsonb:
    """Wraps a value for a ``jsonb`` parameter (NaN/Infinity are rejected)."""
    return Jsonb(value, dumps=_dumps)


class _TimedCursor(AsyncCursor[DictRow]):
    """Records query latency into ``agenttwin_db_query_duration_seconds``."""

    async def execute(self, query: Any, params: Any = None, **kwargs: Any) -> _TimedCursor:
        m = metrics()
        start = time.perf_counter()
        outcome = "ok"
        try:
            await super().execute(query, params, **kwargs)
            return self
        except BaseException:
            outcome = "error"
            raise
        finally:
            if m is not None:
                m.observe_db(outcome, time.perf_counter() - start)


# How the pool behaves when PostgreSQL goes away (spec §64). psycopg_pool's
# defaults suit a database that is up: a request waits up to the pool timeout
# (10 s) for a connection, and reconnect attempts back off without bound. So
# during an outage every request hung 10 s before failing, and after a minute
# of outage the pool waited about another minute before trying again.
CONNECT_TIMEOUT_S = 5  # one connection attempt, handshake included (a partition never answers)
UNREACHABLE_WAIT_S = 1.0  # a request's wait while connecting is failing
RECONNECT_DELAY_MAX_S = 2.0  # between reconnect attempts


class _CappedBackoff(AttemptWithBackoff):
    def update_delay(self, now: float) -> None:
        super().update_delay(now)
        self.delay = min(self.delay, RECONNECT_DELAY_MAX_S)


class _Pool(AsyncConnectionPool[Conn]):
    """A pool that says quickly that PostgreSQL is unreachable, and notices
    quickly that it is back. While connection attempts fail, requests waiting
    for a connection are failed at once and new ones wait UNREACHABLE_WAIT_S;
    reconnect attempts are at most RECONNECT_DELAY_MAX_S apart. The failure is
    a PoolTimeout, which is_unavailable reports (503, deferred events)."""

    _unreachable = False

    async def _connect(self, timeout: float | None = None) -> Conn:
        try:
            conn = await super()._connect(timeout)
        except Exception as err:
            self._unreachable = True
            await self._fail_waiting(err)
            raise
        self._unreachable = False
        return conn

    async def _fail_waiting(self, err: Exception) -> None:
        async with self._lock:
            waiting = list(self._waiting)
            self._waiting.clear()
        for client in waiting:
            await client.fail(PoolTimeout(f"the database is unreachable: {type(err).__name__}"))

    async def _add_connection(self, attempt: AttemptWithBackoff | None, growing: bool = False) -> None:
        await super()._add_connection(attempt or _CappedBackoff(timeout=self.reconnect_timeout), growing)

    async def getconn(self, timeout: float | None = None) -> Conn:
        if timeout is None and self._unreachable:
            timeout = UNREACHABLE_WAIT_S
        return await super().getconn(timeout)


async def _configure(conn: AsyncConnection[Any]) -> None:
    # UUID columns come back as canonical strings (the API speaks strings).
    conn.adapters.register_loader("uuid", TextLoader)


async def connect(
    url: str,
    *,
    schema: str,
    min_size: int = 1,
    max_size: int = 10,
    startup_attempts: int = 20,
    log: Log | None = None,
) -> Pool:
    """Opens a pool whose connections use ``search_path=<schema>,public``,
    autocommit (transactions are explicit) and dict rows."""
    if not _SCHEMA_NAME.match(schema):
        raise ValueError(f"invalid schema name {schema!r}")
    pool: Pool = _Pool(
        url,
        min_size=min_size,
        max_size=max(min_size, max_size),
        kwargs={
            "autocommit": True,
            "row_factory": dict_row,
            "cursor_factory": _TimedCursor,
            "application_name": "agenttwin",
            "options": f"-c search_path={schema},public",
            "connect_timeout": CONNECT_TIMEOUT_S,
        },
        configure=_configure,
        open=False,
        timeout=10,
        max_idle=300,
        check=AsyncConnectionPool.check_connection,
    )
    # The pool keeps connecting in the background; wait for it in bounded
    # rounds so a slow database start is logged instead of failing at once.
    await pool.open(wait=False)
    for attempt in range(1, startup_attempts + 1):
        try:
            await pool.wait(timeout=5)
            return pool
        except PoolTimeout as err:
            if attempt == startup_attempts:
                await pool.close()
                raise ConnectionError(f"connect database after {attempt} attempts: {err}") from err
            if log:
                log.warn("database not ready", attempt=attempt)
    raise AssertionError("unreachable")


@asynccontextmanager
async def transaction(pool: Pool) -> AsyncIterator[Conn]:
    """A connection with an open transaction; commits on success."""
    async with pool.connection() as conn, conn.transaction():
        yield conn


# ---------------------------------------------------------------- migrations


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    up: str
    down: str
    checksum: str


def load_migrations(directory: Path) -> list[Migration]:
    """Reads ``NNNN_name.up.sql`` / ``NNNN_name.down.sql``. The checksum is the
    SHA-256 of the up script bytes (identical to gokit/db)."""
    found: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.iterdir()):
        m = _MIGRATION_NAME.match(path.name)
        if m is None:
            if path.suffix == ".sql":
                raise ValueError(f"migration {path.name!r} does not match NNNN_name.(up|down).sql")
            continue
        version, name, kind = m.groups()
        entry = found.setdefault(version, {"name": name})
        if entry["name"] != name:
            raise ValueError(f"migration {version} has conflicting names {entry['name']!r} and {name!r}")
        entry[kind] = path.read_bytes()
    out = []
    for version in sorted(found):
        e = found[version]
        if "up" not in e:
            raise ValueError(f"migration {version}_{e['name']} has no up script")
        up: bytes = e["up"]
        out.append(
            Migration(
                version=version,
                name=e["name"],
                up=up.decode("utf-8"),
                down=cast(bytes, e.get("down", b"")).decode("utf-8"),
                checksum=hashlib.sha256(up).hexdigest(),
            )
        )
    return out


class Migrator:
    """Applies migrations for one schema under a PostgreSQL advisory lock."""

    def __init__(
        self, pool: Pool, schema: str, migrations: Sequence[Migration], log: Log | None = None
    ) -> None:
        if not _SCHEMA_NAME.match(schema):
            raise ValueError(f"invalid schema name {schema!r}")
        self.pool = pool
        self.schema = schema
        self.migrations = list(migrations)
        self.log = log

    @property
    def _ledger(self) -> sql.Composed:
        return sql.SQL("{}.schema_migrations").format(sql.Identifier(self.schema))

    @asynccontextmanager
    async def _locked(self) -> AsyncIterator[Conn]:
        key = "agenttwin-migrate-" + self.schema
        async with self.pool.connection() as conn:
            await conn.execute("SELECT pg_advisory_lock(hashtext(%s))", (key,))
            try:
                await conn.execute(
                    sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(self.schema))
                )
                await conn.execute(
                    sql.SQL(
                        """CREATE TABLE IF NOT EXISTS {} (
                        version text PRIMARY KEY,
                        name text NOT NULL,
                        checksum text NOT NULL,
                        applied_at timestamptz NOT NULL DEFAULT now())"""
                    ).format(self._ledger)
                )
                yield conn
            finally:
                await conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (key,))

    async def _applied(self, conn: Conn) -> dict[str, str]:
        cur = await conn.execute(sql.SQL("SELECT version, checksum FROM {}").format(self._ledger))
        return {row["version"]: row["checksum"] for row in await cur.fetchall()}

    async def up(self) -> int:
        """Applies pending migrations; a modified applied migration is an error."""
        count = 0
        async with self._locked() as conn:
            done = await self._applied(conn)
            for mig in self.migrations:
                if mig.version in done:
                    if done[mig.version] != mig.checksum:
                        raise RuntimeError(
                            f"migration {mig.version}_{mig.name} was modified after being applied "
                            "(checksum mismatch)"
                        )
                    continue
                async with conn.transaction():
                    await conn.execute(
                        sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.schema))
                    )
                    await conn.execute(mig.up)
                    await conn.execute(
                        sql.SQL("INSERT INTO {} (version, name, checksum) VALUES (%s, %s, %s)").format(
                            self._ledger
                        ),
                        (mig.version, mig.name, mig.checksum),
                    )
                count += 1
                if self.log:
                    self.log.info("migration applied", schema=self.schema, version=mig.version, name=mig.name)
        return count

    async def down(self, steps: int = 1) -> int:
        count = 0
        async with self._locked() as conn:
            done = await self._applied(conn)
            for mig in reversed(self.migrations):
                if count >= steps:
                    break
                if mig.version not in done:
                    continue
                if not mig.down:
                    raise RuntimeError(f"migration {mig.version}_{mig.name} has no down script")
                async with conn.transaction():
                    await conn.execute(
                        sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.schema))
                    )
                    await conn.execute(mig.down)
                    await conn.execute(
                        sql.SQL("DELETE FROM {} WHERE version = %s").format(self._ledger), (mig.version,)
                    )
                count += 1
        return count

    async def pending(self) -> list[str]:
        async with self._locked() as conn:
            done = await self._applied(conn)
        return [f"{m.version}_{m.name}" for m in self.migrations if m.version not in done]
