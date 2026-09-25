"""Integration harness of the evaluation service (ADR-0012: real
infrastructure). One stack per test:

* a fresh PostgreSQL database with the ``evaluation`` schema migrated;
* the service's HTTP app served by uvicorn on a real socket;
* a fake of the simulation service that verifies the internal JWT (audience
  included) of every call it receives, answers with contract payloads and
  checks every exchange against the simulation service's contract;
* the service's contract (ADR-0021): every response of the API, and every
  request it accepts, is checked against ``evaluation-service.openapi.yaml``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx

from agenttwin_core import api_fakes as fake
from agenttwin_core.auth import Principal, Role, TokenService
from agenttwin_core.db import Migrator, Pool, connect, load_migrations
from agenttwin_core.logx import get_logger
from agenttwin_core.openapi_contract import Contract, ContractViolation, contract_path
from agenttwin_core.testing import temp_database
from agenttwin_core.web import Health, build_app
from agenttwin_evaluation.clients import SimulationClient
from agenttwin_evaluation.datasets import DatasetsAPI
from agenttwin_evaluation.store import SCHEMA, Store
from sim_testutil import serve_app

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


@dataclass
class Stack:
    url: str
    pool: Pool
    store: Store
    tokens: TokenService
    simulation: FakeSimulation
    client: httpx.AsyncClient
    contract: Contract
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
            params={k: v for k, v in params.items() if v is not None},
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
async def evaluation_stack() -> AsyncIterator[Stack]:
    async with temp_database() as url:
        pool = await connect(url, schema=SCHEMA, max_size=8)
        await Migrator(pool, SCHEMA, load_migrations(MIGRATIONS)).up()
        tokens = TokenService(SECRET)
        store = Store(pool)
        sim = FakeSimulation(tokens)
        simulation = SimulationClient(
            "http://simulation-service.test", tokens, transport=httpx.MockTransport(sim)
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
                    )
                finally:
                    # A violation on the service's own calls to the simulation
                    # service is the root cause of whatever the test saw.
                    if sim.checker.violations:
                        raise ContractViolation("\n".join(sim.checker.violations))
        finally:
            await simulation.close()
            await pool.close()
