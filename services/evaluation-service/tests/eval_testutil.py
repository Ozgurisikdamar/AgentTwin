"""Integration harness of the evaluation service (ADR-0012: real
infrastructure). One stack per test:

* a fresh PostgreSQL database with the ``evaluation`` schema migrated;
* the service's HTTP app served by uvicorn on a real socket;
* the simulation service: either a fake that verifies the internal JWT
  (audience included) of every call it receives and answers with contract
  payloads, or the real service with the real demo agent
  (:func:`evaluation_with_simulation`) — every exchange with it checked
  against the simulation service's contract;
* a fake of the trace service answering each simulation run's traces with
  their usage (one trace per run not finalized yet, so unknown usage is
  exercised), checked against the trace service's contract;
* an evaluation worker driven step by step by the test, with the
  deterministic fake judge;
* the service's contract (ADR-0021): every response of the API, and every
  request it accepts, is checked against ``evaluation-service.openapi.yaml``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx

from agenttwin_core import api_fakes as fake
from agenttwin_core.auth import Principal, Role, TokenService
from agenttwin_core.db import Migrator, Pool, connect, load_migrations
from agenttwin_core.embeddings import HashingEmbedder
from agenttwin_core.logx import get_logger
from agenttwin_core.openapi_contract import Contract, ContractViolation, contract_path
from agenttwin_core.testing import temp_database
from agenttwin_core.web import Health, build_app
from agenttwin_evaluation.calibrations import JudgesAPI
from agenttwin_evaluation.clients import SimulationClient, TraceClient
from agenttwin_evaluation.config import EvaluationConfig
from agenttwin_evaluation.datasets import DatasetsAPI
from agenttwin_evaluation.judges import FakeJudge, JudgeProvider
from agenttwin_evaluation.miner import Miner
from agenttwin_evaluation.regression_store import RegressionStore
from agenttwin_evaluation.reviews import ReviewsAPI
from agenttwin_evaluation.runs import EvalRunsAPI
from agenttwin_evaluation.store import SCHEMA, Store
from agenttwin_evaluation.worker import EvalWorker
from sim_testutil import Stack as SimStack
from sim_testutil import serve_app, simulation_stack

REPO = Path(__file__).resolve().parents[3]
MIGRATIONS = REPO / "services" / "evaluation-service" / "migrations"
ASSURANCE = REPO / "demo" / "support-refund-agent" / "assurance"
SECRET = "test-internal-token-secret-0123456789abcdef"
ORG = "0190f3b4-0000-7000-8000-00000000000a"
PROJECT = "0190f3b4-0000-7000-8000-0000000000b1"
OTHER_PROJECT = "0190f3b4-0000-7000-8000-0000000000b2"
SCENARIOS = tuple(sorted(p.stem for p in (ASSURANCE / "scenarios").glob("*.yaml")))

_CONTRACT: Contract | None = None


def contract() -> Contract:
    """A fresh contract checker (its coverage is per stack) over the parsed
    document (parsed once)."""
    global _CONTRACT
    if _CONTRACT is None:
        _CONTRACT = Contract.load(contract_path("evaluation-service"))
    return Contract(_CONTRACT.document, _CONTRACT.name)


class FakeSimulation:
    """``GET /api/v1/scenarios`` of the simulation service, over a scenario
    list per project (default: the demo suite in :data:`PROJECT`). Answers
    are contract payloads and every exchange is checked against the
    simulation service's contract (:attr:`checker`, asserted by the harness)."""

    def __init__(self, tokens: TokenService) -> None:
        self.tokens = tokens
        self.scenarios: dict[str, list[str]] = {PROJECT: list(SCENARIOS)}
        self.page_size = 4
        self.calls: list[dict[str, str]] = []
        self.fail: int | Literal["down"] | None = None
        self.checker = fake.ExchangeChecker()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        response = self._answer(request)
        self.checker.check_exchange(request, response)
        return response

    def _answer(self, request: httpx.Request) -> httpx.Response:
        p, _ = self.tokens.verify(request.headers["authorization"][7:], "simulation-service")
        q = dict(request.url.params)
        self.calls.append({"path": request.url.path, "actor": p.actor} | q)
        if self.fail == "down":
            raise httpx.ConnectError("connection refused", request=request)
        if self.fail is not None:
            return httpx.Response(self.fail, json=fake.error("INTERNAL", "An internal error occurred."))
        if request.url.path != "/api/v1/scenarios" or not p.can_access_project(q["project_id"]):
            return httpx.Response(404, json=fake.error("NOT_FOUND", "No such resource."))
        names = self.scenarios.get(q["project_id"], [])
        start = int(q.get("cursor") or 0)
        # The client pages with the largest page; the fake answers smaller
        # pages, so paging is exercised.
        assert int(q["limit"]) == 200
        page = names[start : start + self.page_size]
        more = start + self.page_size < len(names)
        items = [
            fake.scenario(
                id=fake.uuid(0xD000 + i),
                organization_id=p.org_id,
                project_id=q["project_id"],
                name=name,
                version_id=fake.uuid(0xD100 + i),
            )
            for i, name in enumerate(page, start)
        ]
        return httpx.Response(
            200, json={"items": items, "next_cursor": str(start + self.page_size) if more else None}
        )


class CheckedTransport(httpx.AsyncBaseTransport):
    """A real HTTP transport whose exchanges are checked against the contract
    of the service that owns the path (violations kept, see the harness)."""

    def __init__(self) -> None:
        self.inner = httpx.AsyncHTTPTransport()
        self.checker = fake.ExchangeChecker()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self.inner.handle_async_request(request)
        content = await response.aread()
        checked = httpx.Response(
            response.status_code, headers=response.headers, content=content, request=request
        )
        self.checker.check_exchange(request, checked)
        return checked

    async def aclose(self) -> None:
        await self.inner.aclose()


class FakeTraces:
    """``GET /api/v1/traces?simulation_run_id=…`` of the trace service: the
    traces of the cases of that run, as ``traces_of`` returns their ids, with
    usage by position (100 tokens and $0.001 per position, from 1), times the
    order in which the run was first asked about (so two runs never report
    the same usage); the trace at :attr:`unsettled` positions is not
    finalized (unknown usage)."""

    def __init__(self, tokens: TokenService) -> None:
        self.tokens = tokens
        self.traces_of: Callable[[str], Awaitable[list[str | None]]] | None = None
        self.unsettled: set[int] = {2}
        self.scale: dict[str, int] = {}
        self.calls: list[dict[str, str]] = []
        self.fail: int | None = None
        # ``GET /api/v1/traces/{id}``: the detail of each known trace (by id).
        self.details: dict[str, dict[str, Any]] = {}
        self.checker = fake.ExchangeChecker()

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        response = await self._answer(request)
        self.checker.check_exchange(request, response)
        return response

    async def _answer(self, request: httpx.Request) -> httpx.Response:
        p, _ = self.tokens.verify(request.headers["authorization"][7:], "trace-service")
        q = dict(request.url.params)
        self.calls.append({"actor": p.actor} | q)
        assert p.can_access_project(q["project_id"])
        if self.fail is not None:
            return httpx.Response(self.fail, json=fake.error("INTERNAL", "An internal error occurred."))
        prefix = "/api/v1/traces/"
        if request.url.path.startswith(prefix):
            detail = self.details.get(request.url.path[len(prefix) :])
            # A trace of a detail without a project belongs to the one asked for.
            owner = {"organization_id": p.org_id, "project_id": q["project_id"]}
            if detail is not None:
                detail = detail | {"trace": owner | detail["trace"]}
            if detail is None or detail["trace"]["project_id"] != q["project_id"]:
                missing = fake.error("NOT_FOUND", "The requested resource does not exist.")
                return httpx.Response(404, json=missing)
            return httpx.Response(200, json=detail)
        ids = await self.traces_of(q["simulation_run_id"]) if self.traces_of else []
        scale = self.scale.setdefault(q["simulation_run_id"], len(self.scale) + 1)
        items = [
            fake.trace(
                trace_id=trace_id,
                organization_id=p.org_id,
                project_id=q["project_id"],
                simulation_run_id=q["simulation_run_id"],
                finalized=i not in self.unsettled,
                input_tokens=80 * i * scale,
                output_tokens=20 * i * scale,
                cost_usd=round(0.001 * i * scale, 6),
            )
            for i, trace_id in enumerate(ids, 1)
            if trace_id
        ]
        return httpx.Response(200, json={"items": items, "next_cursor": None})


@dataclass
class Stack:
    url: str
    pool: Pool
    store: Store
    tokens: TokenService
    simulation: FakeSimulation
    client: httpx.AsyncClient
    contract: Contract
    worker: EvalWorker
    traces: FakeTraces
    cfg: EvaluationConfig
    regressions: RegressionStore
    principal: Principal = field(
        default_factory=lambda: Principal(
            org_id=ORG, actor="user:engineer", role=Role.ENGINEER, project_ids=(PROJECT,)
        )
    )

    def token(self, p: Principal | None = None) -> str:
        return self.tokens.mint(p or self.principal, "evaluation-service")

    async def call(
        self, method: str, path: str, body: Any = None, *, as_: Principal | None = None, **params: Any
    ) -> httpx.Response:
        return await self.client.request(
            method,
            path,
            json=body,
            params={k: v for k, v in params.items() if v is not None} or None,
            headers={"Authorization": "Bearer " + self.token(as_)},
        )

    async def ok(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        status: int = 200,
        as_: Principal | None = None,
        **params: Any,
    ) -> Any:
        resp = await self.call(method, path, body, as_=as_, **params)
        assert resp.status_code == status, (resp.status_code, resp.text)
        return resp.json()

    async def fails(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        status: int,
        code: str,
        as_: Principal | None = None,
        **params: Any,
    ) -> dict[str, Any]:
        resp = await self.call(method, path, body, as_=as_, **params)
        assert resp.status_code == status, (resp.status_code, resp.text)
        error: dict[str, Any] = resp.json()["error"]
        assert error["code"] == code, error
        return error

    async def outbox(self, event_type: str) -> list[dict[str, Any]]:
        rows = await self.store.all(
            "SELECT envelope FROM outbox WHERE event_type = %s ORDER BY created_at, id", (event_type,)
        )
        return [r["envelope"] for r in rows]


@asynccontextmanager
async def evaluation_stack(
    simulation_url: str | None = None, judge: JudgeProvider | None = None, **overrides: Any
) -> AsyncIterator[Stack]:
    """The evaluation service over a fake simulation service, or over the
    real one at ``simulation_url``."""
    async with temp_database() as url:
        pool = await connect(url, schema=SCHEMA, max_size=12)
        await Migrator(pool, SCHEMA, load_migrations(MIGRATIONS)).up()
        tokens = TokenService(SECRET)
        store = Store(pool)
        sim = FakeSimulation(tokens)
        checked = CheckedTransport()
        transport: httpx.AsyncBaseTransport = checked if simulation_url else httpx.MockTransport(sim)
        simulation = SimulationClient(
            simulation_url or "http://simulation-service.test", tokens, transport=transport
        )
        fake_traces = FakeTraces(tokens)
        traces = TraceClient("http://trace-service.test", tokens, transport=httpx.MockTransport(fake_traces))
        settings: dict[str, Any] = {
            "simulation_service_url": simulation_url or "http://simulation-service.test",
            "trace_service_url": "http://trace-service.test",
            "lease_seconds": 5.0,
            "poll_interval_s": 0.05,
            "fetch_concurrency": 4,
        }
        settings.update(overrides)
        cfg = EvaluationConfig(**settings)
        regressions = RegressionStore(pool)
        worker = EvalWorker(
            store=store,
            cfg=cfg,
            simulation=simulation,
            traces=traces,
            judge=judge or FakeJudge(),
            log=get_logger("worker"),
            miner=Miner(
                store=regressions,
                traces=traces,
                embedder=HashingEmbedder(),
                threshold=cfg.regression_similarity_threshold,
                log=get_logger("miner"),
            ),
        )
        checker = contract()
        app = build_app(
            service="evaluation-service",
            version="test",
            health=Health(),
            tokens=tokens,
            audience="evaluation-service",
        )
        DatasetsAPI(store=store, simulation=simulation, log=get_logger("api")).routes(app)
        EvalRunsAPI(store=store, log=get_logger("api")).routes(app)
        ReviewsAPI(store=store, log=get_logger("api")).routes(app)
        JudgesAPI(store=store, judge=worker.judge, log=get_logger("api")).routes(app)
        try:
            async with (
                serve_app(app) as base,
                httpx.AsyncClient(
                    base_url=base,
                    trust_env=False,
                    timeout=60,
                    event_hooks={"response": [checker.response_hook()]},
                ) as client,
            ):
                try:
                    yield Stack(
                        url=base,
                        pool=pool,
                        store=store,
                        tokens=tokens,
                        simulation=sim,
                        client=client,
                        contract=checker,
                        worker=worker,
                        traces=fake_traces,
                        cfg=cfg,
                        regressions=regressions,
                    )
                finally:
                    # A violation on the service's own calls to the simulation
                    # or trace service is the root cause of whatever the test saw.
                    violations = (
                        sim.checker.violations + checked.checker.violations + fake_traces.checker.violations
                    )
                    if violations:
                        raise ContractViolation("\n".join(violations))
        finally:
            await simulation.close()
            await traces.close()
            await pool.close()


@asynccontextmanager
async def evaluation_with_simulation(**overrides: Any) -> AsyncIterator[tuple[Stack, SimStack]]:
    """The evaluation service over the real simulation service (its API, its
    worker and the real demo agent), sharing the internal token secret; the
    demo twin and every scenario of the demo suite are registered."""
    sim_overrides = overrides.pop("simulation", {})
    async with simulation_stack(**sim_overrides) as sim:
        await sim.register_suite()
        async with evaluation_stack(simulation_url=sim.url, **overrides) as ev:

            async def traces_of(run_id: str) -> list[str | None]:
                rows = await sim.store.all(
                    "SELECT trace_id FROM simulation_case WHERE run_id = %s ORDER BY position", (run_id,)
                )
                return [r["trace_id"] for r in rows]

            ev.traces.traces_of = traces_of
            yield ev, sim


async def run_simulations(sim: SimStack) -> list[str]:
    """Lets the simulation worker execute every queued run."""
    done: list[str] = []
    while (run_id := await sim.worker.process_next()) is not None:
        done.append(run_id)
    return done
