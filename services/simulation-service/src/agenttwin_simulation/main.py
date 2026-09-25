"""simulation-service entry point.

``simulation-service [serve]``  the API and the twin endpoint agents call
``simulation-service worker``   executes simulation runs (separable from the
                                API: a stuck simulation never blocks it)
``simulation-service migrate``  / ``healthcheck`` (see agenttwin_core.service)
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from agenttwin_core.embeddings import build_embedder
from agenttwin_core.evaluators import default_registry
from agenttwin_core.service import Runtime, Spec
from agenttwin_core.service import main as service_main
from agenttwin_core.web import build_app
from agenttwin_simulation.api import SimulationAPI
from agenttwin_simulation.clients import AgentClient, ControlPlaneClient, TraceServiceClient
from agenttwin_simulation.config import SimulationConfig, load_config
from agenttwin_simulation.metrics import SimulationMetrics
from agenttwin_simulation.runner import QUEUE, Worker
from agenttwin_simulation.store import SCHEMA, Store
from agenttwin_simulation.twin.adapters import AdapterRegistry
from agenttwin_simulation.twin_http import TwinEndpoint

__all__ = ["NAME", "SPEC", "build_serve", "build_worker", "main"]

NAME = "simulation-service"
VERSION = "0.2.0"
MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"


def _migrations_dir() -> Path:
    # Container images copy the migrations next to the package.
    for candidate in (MIGRATIONS, Path("/app/services/simulation-service/migrations")):
        if candidate.is_dir():
            return candidate
    return MIGRATIONS


def adapters() -> AdapterRegistry:
    """Custom twin adapters are registered here, in code (no dynamic loading)."""
    return AdapterRegistry()


async def build_serve(rt: Runtime, cfg: Any) -> FastAPI:
    assert isinstance(cfg, SimulationConfig)  # noqa: S101 - wired by SPEC
    store = Store(rt.pool)
    registry = default_registry()
    reg = adapters()
    metrics = SimulationMetrics.on(rt.metrics.registry, rt.metrics.service)
    control_plane = ControlPlaneClient(cfg.control_plane_url, rt.tokens)
    embedder = build_embedder(cfg.embedding)
    rt.log.info(
        "scenario embeddings", provider=cfg.embedding.provider, model=embedder.model, dims=embedder.dims
    )
    app = build_app(
        service=rt.metrics.service, version=VERSION, health=rt.health, tokens=rt.tokens, audience=NAME
    )
    SimulationAPI(
        store=store,
        cfg=cfg,
        registry=registry,
        control_plane=control_plane,
        log=rt.log,
        adapters=reg,
        embedder=embedder,
    ).routes(app)
    TwinEndpoint(store, cfg, adapters=reg, metrics=metrics).routes(app)
    rt.start_relay(SCHEMA)

    async def close_clients() -> None:
        await rt.stop.wait()
        await control_plane.close()
        close = getattr(embedder, "close", None)
        if close is not None:
            await close()

    rt.spawn("clients", close_clients)
    return app


async def build_worker(rt: Runtime, cfg: Any) -> FastAPI | None:
    assert isinstance(cfg, SimulationConfig)  # noqa: S101 - wired by SPEC
    agents = AgentClient()
    traces = TraceServiceClient(cfg.trace_service_url, rt.tokens)
    worker = Worker(
        store=Store(rt.pool),
        cfg=cfg,
        agents=agents,
        traces=traces,
        registry=default_registry(),
        log=rt.log,
        adapters=adapters(),
        metrics=SimulationMetrics.on(rt.metrics.registry, rt.metrics.service),
    )
    for i in range(cfg.worker_concurrency):
        rt.spawn(f"simulation-worker-{i}", lambda: worker.claim_loop(rt.stop))
    rt.spawn("simulation-janitor", lambda: worker.janitor_loop(rt.stop))
    rt.spawn("simulation-outcomes", lambda: worker.outcome_loop(rt.stop))
    rt.spawn(
        "simulation-events", lambda: rt.broker.consume(QUEUE, worker.on_event, concurrency=4, stop=rt.stop)
    )
    rt.start_relay(SCHEMA)

    async def close_clients() -> None:
        await rt.stop.wait()
        await agents.close()
        await traces.close()

    rt.spawn("clients", close_clients)
    rt.log.info("simulation worker ready", owner=worker.owner, concurrency=cfg.worker_concurrency)
    return None


SPEC = Spec(
    name=NAME,
    version=VERSION,
    default_port=8084,
    schema=SCHEMA,
    migrations_dir=_migrations_dir(),
    configure=load_config,
    roles={"serve": build_serve, "worker": build_worker},
)


def main() -> None:
    sys.exit(service_main(SPEC))


if __name__ == "__main__":
    main()
