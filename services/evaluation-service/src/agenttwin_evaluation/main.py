"""evaluation-service entry point.

``evaluation-service [serve]``  the API: datasets, evaluation runs, reviews
                                and judge calibrations
``evaluation-service worker``   prepares, waits for and evaluates runs
                                (separable from the API: a slow judge never
                                blocks it)
``evaluation-service migrate``  / ``healthcheck`` (see agenttwin_core.service)
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from agenttwin_core.service import Runtime, Spec
from agenttwin_core.service import main as service_main
from agenttwin_core.web import build_app
from agenttwin_evaluation.clients import SimulationClient, TraceClient
from agenttwin_evaluation.config import EvaluationConfig, load_config
from agenttwin_evaluation.datasets import DatasetsAPI
from agenttwin_evaluation.judges import build_judge
from agenttwin_evaluation.runs import EvalRunsAPI
from agenttwin_evaluation.store import SCHEMA, Store
from agenttwin_evaluation.worker import QUEUE, EvalWorker

__all__ = ["NAME", "SPEC", "build_serve", "build_worker", "main"]

NAME = "evaluation-service"
VERSION = "0.3.0"
MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"


def _migrations_dir() -> Path:
    # Container images copy the migrations next to the package.
    for candidate in (MIGRATIONS, Path("/app/services/evaluation-service/migrations")):
        if candidate.is_dir():
            return candidate
    return MIGRATIONS


async def build_serve(rt: Runtime, cfg: Any) -> FastAPI:
    assert isinstance(cfg, EvaluationConfig)  # noqa: S101 - wired by SPEC
    store = Store(rt.pool)
    simulation = SimulationClient(cfg.simulation_service_url, rt.tokens)
    app = build_app(
        service=rt.metrics.service, version=VERSION, health=rt.health, tokens=rt.tokens, audience=NAME
    )
    DatasetsAPI(store=store, simulation=simulation, log=rt.log).routes(app)
    EvalRunsAPI(store=store, log=rt.log).routes(app)
    rt.start_relay(SCHEMA)

    async def close_clients() -> None:
        await rt.stop.wait()
        await simulation.close()

    rt.spawn("clients", close_clients)
    return app


async def build_worker(rt: Runtime, cfg: Any) -> FastAPI | None:
    assert isinstance(cfg, EvaluationConfig)  # noqa: S101 - wired by SPEC
    simulation = SimulationClient(cfg.simulation_service_url, rt.tokens)
    traces = TraceClient(cfg.trace_service_url, rt.tokens)
    worker = EvalWorker(
        store=Store(rt.pool),
        cfg=cfg,
        simulation=simulation,
        traces=traces,
        judge=build_judge(cfg.judge),
        log=rt.log,
    )
    for i in range(cfg.worker_concurrency):
        rt.spawn(f"evaluation-claim-{i}", lambda: worker.claim_loop(rt.stop))
    rt.spawn("evaluation-wait", lambda: worker.wait_loop(rt.stop))
    rt.spawn("evaluation-janitor", lambda: worker.janitor_loop(rt.stop))
    rt.spawn(
        "evaluation-events", lambda: rt.broker.consume(QUEUE, worker.on_event, concurrency=4, stop=rt.stop)
    )
    rt.start_relay(SCHEMA)

    async def close_clients() -> None:
        await rt.stop.wait()
        await simulation.close()
        await traces.close()

    rt.spawn("clients", close_clients)
    rt.log.info("evaluation worker ready", owner=worker.owner, judge=worker.judge.provider)
    return None


SPEC = Spec(
    name=NAME,
    version=VERSION,
    default_port=8083,
    schema=SCHEMA,
    migrations_dir=_migrations_dir(),
    configure=load_config,
    roles={"serve": build_serve, "worker": build_worker},
)


def main() -> None:
    sys.exit(service_main(SPEC))


if __name__ == "__main__":
    main()
