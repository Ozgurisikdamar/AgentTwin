"""The process skeleton of every Python service (mirrors ``gokit/service``).

``<svc> [serve]``                 the service's primary role
``<svc> <role>``                  another role of the same image (e.g. ``worker``)
``<svc> migrate [up|down N|status]``
``<svc> healthcheck [live|ready]``  container HEALTHCHECK probe

A service declares what is specific to it in a :class:`Spec`; configuration,
logging, telemetry, the database pool, migrations, the broker, the internal
token service, health endpoints and graceful shutdown are handled here.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI

from agenttwin_core.auth import TokenService
from agenttwin_core.config import ConfigError, Environment, Loader
from agenttwin_core.db import Migrator, Pool, connect, load_migrations
from agenttwin_core.events import Broker, OutboxRelay, outbox_backlog
from agenttwin_core.logx import Log, get_logger, setup_logging
from agenttwin_core.probe import healthcheck
from agenttwin_core.telemetry import Metrics, setup_metrics, setup_tracing
from agenttwin_core.web import Health, build_app

__all__ = ["Runtime", "ServiceConfig", "Spec", "main"]


@dataclass(frozen=True)
class ServiceConfig:
    name: str
    env: Environment
    port: int
    log_level: str
    database_url: str
    amqp_url: str
    internal_secret: str
    otlp_endpoint: str
    sample_ratio: float
    migrate_on_start: bool
    db_max_conns: int


def load_service_config(loader: Loader, name: str, default_port: int) -> ServiceConfig:
    return ServiceConfig(
        name=name,
        env=loader.env,
        port=loader.integer("PORT", default_port, 1, 65535),
        log_level=loader.one_of("LOG_LEVEL", "info", "debug", "info", "warn", "error"),
        database_url=loader.required("DATABASE_URL"),
        amqp_url=loader.required("AMQP_URL"),
        internal_secret=loader.secret("AGENTTWIN_INTERNAL_TOKEN_SECRET", 32),
        otlp_endpoint=loader.string("OTEL_EXPORTER_OTLP_ENDPOINT", ""),
        sample_ratio=loader.number("OTEL_TRACES_SAMPLER_ARG", 1.0, 0.0, 1.0),
        migrate_on_start=loader.boolean("MIGRATE_ON_START", loader.env is not Environment.PRODUCTION),
        db_max_conns=loader.integer("DB_MAX_CONNS", 10, 1, 200),
    )


@dataclass
class Runtime:
    """Initialized infrastructure of a running process."""

    config: ServiceConfig
    log: Log
    metrics: Metrics
    tokens: TokenService
    health: Health
    pool: Pool
    broker: Broker
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    _tasks: list[asyncio.Task[None]] = field(default_factory=list)

    def spawn(self, name: str, fn: Callable[[], Awaitable[None]]) -> None:
        """Runs a background worker tied to the process lifecycle; a crash is
        logged loudly (and fails readiness) instead of disappearing."""

        async def wrapped() -> None:
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.log.exception("background worker crashed", worker=name)
                self.health.add(f"worker:{name}", _failed)

        self._tasks.append(asyncio.create_task(wrapped(), name=name))

    def start_relay(self, schema: str) -> None:
        relay = OutboxRelay(self.pool, schema, self.broker)
        self.spawn("outbox-relay", lambda: relay.run(self.stop))

        async def backlog() -> None:
            while not self.stop.is_set():
                try:
                    self.metrics.outbox_backlog.labels(self.metrics.service).set(
                        await outbox_backlog(self.pool, schema)
                    )
                except Exception as err:  # noqa: BLE001 - a metric refresh must never stop the loop
                    self.log.warn("outbox backlog check failed", error=str(err))
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.stop.wait(), timeout=10)

        self.spawn("outbox-backlog", backlog)

    async def close(self, timeout_s: float = 10.0) -> None:
        self.stop.set()
        if self._tasks:
            _, pending = await asyncio.wait(self._tasks, timeout=timeout_s)
            for t in pending:
                t.cancel()
            if pending:
                self.log.warn(
                    "background workers did not stop before the shutdown deadline", count=len(pending)
                )
        await self.broker.close()
        await self.pool.close()


async def _failed() -> None:
    raise RuntimeError("background worker crashed")


BuildFn = Callable[[Runtime, Any], Awaitable[FastAPI | None]]


@dataclass(frozen=True)
class Spec:
    name: str
    version: str
    default_port: int
    schema: str
    migrations_dir: Path
    # Reads service-specific variables (errors accumulate in the loader).
    configure: Callable[[Loader], Any]
    # role -> builder. "serve" is the default role. A builder returns the
    # FastAPI app to serve (routes on top of the standard app) or None to
    # serve only health and metrics (pure workers).
    roles: dict[str, BuildFn]


async def start_runtime(spec: Spec, cfg: ServiceConfig, log: Log, *, role: str) -> Runtime:
    m = setup_metrics(spec.name if role == "serve" else f"{spec.name}-{role}", spec.version)
    setup_tracing(m.service, spec.version, cfg.otlp_endpoint or None, cfg.sample_ratio)
    tokens = TokenService(cfg.internal_secret)
    pool = await connect(cfg.database_url, schema=spec.schema, max_size=cfg.db_max_conns, log=log)
    if cfg.migrate_on_start:
        applied = await Migrator(pool, spec.schema, load_migrations(spec.migrations_dir), log).up()
        log.info("migrations up to date", schema=spec.schema, applied=applied)
    broker = Broker(cfg.amqp_url, connection_name=m.service)
    await broker.connect(log=log)
    health = Health()

    async def pg_check() -> None:
        async with pool.connection() as conn:
            await conn.execute("SELECT 1")

    health.add("postgres", pg_check)
    health.add("rabbitmq", broker.ping)
    return Runtime(config=cfg, log=log, metrics=m, tokens=tokens, health=health, pool=pool, broker=broker)


async def _serve(spec: Spec, cfg: ServiceConfig, svc_cfg: Any, log: Log, role: str) -> None:
    rt = await start_runtime(spec, cfg, log, role=role)
    app: FastAPI | None = None
    try:
        app = await spec.roles[role](rt, svc_cfg)
        if app is None:
            app = build_app(
                service=rt.metrics.service,
                version=spec.version,
                health=rt.health,
                tokens=None,
                audience=spec.name,
            )
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="0.0.0.0",  # noqa: S104 - container entrypoint
                port=cfg.port,
                log_config=None,
                access_log=False,
                lifespan="off",
                server_header=False,
                date_header=False,
                timeout_graceful_shutdown=15,
            )
        )
        log.info("listening", role=role, port=cfg.port)
        await server.serve()
    finally:
        rt.health.draining = True
        await rt.close()
        log.info("stopped", role=role)


async def _migrate(spec: Spec, cfg: ServiceConfig, log: Log, args: Sequence[str]) -> None:
    pool = await connect(cfg.database_url, schema=spec.schema, max_size=2, log=log)
    try:
        m = Migrator(pool, spec.schema, load_migrations(spec.migrations_dir), log)
        action = args[0] if args else "up"
        if action == "up":
            log.info("migrations applied", schema=spec.schema, count=await m.up())
        elif action == "down":
            steps = int(args[1]) if len(args) > 1 else 1
            if steps < 1:
                raise ValueError("down requires a positive step count")
            log.info("migrations reverted", schema=spec.schema, count=await m.down(steps))
        elif action == "status":
            pending = await m.pending()
            log.info(
                "migration status",
                schema=spec.schema,
                pending=len(pending),
                pending_versions=pending,
                known=len(m.migrations),
            )
        else:
            raise ValueError(f"unknown migrate action {action!r} (up | down N | status)")
    finally:
        await pool.close()


def main(spec: Spec, argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args.pop(0) if args else "serve"
    if cmd == "healthcheck":
        return healthcheck(os.environ.get("PORT"), spec.default_port, args)
    if cmd != "migrate" and cmd not in spec.roles:
        roles = " | ".join(spec.roles)
        print(  # noqa: T201
            f"usage: {spec.name} [{roles} | migrate [up | down N | status] | healthcheck [live | ready]]",
            file=sys.stderr,
        )
        return 2
    loader = Loader()
    cfg = load_service_config(loader, spec.name, spec.default_port)
    svc_cfg = spec.configure(loader)
    setup_logging(spec.name if cmd in ("serve", "migrate") else f"{spec.name}-{cmd}", cfg.log_level)
    log = get_logger(spec.name)
    try:
        loader.raise_for_errors()
    except ConfigError as err:
        log.error("invalid configuration", error=str(err))
        return 2
    try:
        if cmd == "migrate":
            asyncio.run(_migrate(spec, cfg, log, args))
        else:
            log.info("starting", version=spec.version, env=str(cfg.env), role=cmd)
            asyncio.run(_serve(spec, cfg, svc_cfg, log, cmd))
    except KeyboardInterrupt:
        return 0
    except Exception as err:
        log.exception("service failed", error=str(err))
        return 1
    return 0
