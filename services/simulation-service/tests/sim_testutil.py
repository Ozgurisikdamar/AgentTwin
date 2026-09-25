"""Integration harness of the simulation service (ADR-0012: real
infrastructure). One stack per test:

* a fresh PostgreSQL database with the ``simulation`` schema migrated;
* the service's HTTP app (API + twin endpoint) served by uvicorn on a real
  socket, so tool calls, dropped connections and partial responses travel
  over real TCP;
* the real demo agent (``support_refund_agent``) behind its HTTP adapter, in
  a background thread, calling the twin endpoint with ``urllib``;
* a worker driven step by step by the test (``process_next``);
* fakes of the control plane and the trace service that verify the
  internal JWT (audience included) of every call they receive, answer with
  contract payloads and check every exchange against those services'
  contracts;
* the service's contract (ADR-0021): every response of the API and the twin
  endpoint, every request the service accepts and every call the worker makes
  to the agent is checked against ``simulation-service.openapi.yaml``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import socket
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx
import uvicorn
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agenttwin import AgentTwin, Config
from agenttwin_core import api_fakes as fake
from agenttwin_core.auth import Principal, Role, TokenService
from agenttwin_core.db import Migrator, Pool, connect, load_migrations, transaction
from agenttwin_core.evaluators import default_registry
from agenttwin_core.jobs import JobStatus
from agenttwin_core.logx import get_logger
from agenttwin_core.openapi_contract import Contract, ContractTransport, ContractViolation, contract_path
from agenttwin_core.testing import temp_database
from agenttwin_core.web import Health, build_app
from agenttwin_simulation.api import SimulationAPI
from agenttwin_simulation.cases import initial_state
from agenttwin_simulation.clients import AgentClient, ControlPlaneClient, TraceServiceClient
from agenttwin_simulation.config import AgentEndpoint, SimulationConfig
from agenttwin_simulation.runner import Worker
from agenttwin_simulation.store import SCHEMA, Store
from agenttwin_simulation.twin.definition import load_twin
from agenttwin_simulation.twin.engine import CaseState
from agenttwin_simulation.twin_http import TwinEndpoint, token_hash
from support_refund_agent.agent import Agent, ManifestStore, scripted_model_factory
from support_refund_agent.server import AgentServer
from support_refund_agent.world import INTERNAL_API_KEY

REPO = Path(__file__).resolve().parents[3]
MIGRATIONS = REPO / "services" / "simulation-service" / "migrations"
ASSURANCE = REPO / "demo" / "support-refund-agent" / "assurance"
SECRET = "test-internal-token-secret-0123456789abcdef"
ORG = "0190f3b4-0000-7000-8000-00000000000a"
PROJECT = "0190f3b4-0000-7000-8000-0000000000b1"
OTHER_PROJECT = "0190f3b4-0000-7000-8000-0000000000b2"
AGENT = "support-refund-agent"
AGENT_TOKEN = "agent-token-for-tests-0123456789"
VERSIONS = ("1.2.3", "1.2.4", "1.3.0", "1.3.1")


_CONTRACT: Contract | None = None


def contract() -> Contract:
    """A fresh contract checker (its coverage is per stack) over the parsed
    document (parsed once)."""
    global _CONTRACT
    if _CONTRACT is None:
        _CONTRACT = Contract.load(contract_path("simulation-service"))
    return Contract(_CONTRACT.document, _CONTRACT.name)


def twin_yaml() -> str:
    return (ASSURANCE / "twin.yaml").read_text()


def scenario_yaml(name: str) -> str:
    return (ASSURANCE / "scenarios" / f"{name}.yaml").read_text()


def manifest_sha256(version: str) -> str:
    """The manifest digest the fake control plane pins for ``version``."""
    return hashlib.sha256(f"manifest:{version}".encode()).hexdigest()


class FakeControlPlane:
    """``GET /internal/v1/agent-versions`` of the control plane. Its answers
    are contract payloads and every exchange is checked against the control
    plane's contract (:attr:`checker`, asserted by the harness)."""

    def __init__(self, tokens: TokenService, versions: tuple[str, ...] = VERSIONS) -> None:
        self.tokens = tokens
        self.versions = versions
        self.calls: list[dict[str, str]] = []
        # A documented failure status to answer every call with, or "down"
        # for a control plane that does not answer at all.
        self.fail: int | Literal["down"] | None = None
        self.checker = fake.ExchangeChecker()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        response = self._answer(request)
        self.checker.check_exchange(request, response)
        return response

    def _answer(self, request: httpx.Request) -> httpx.Response:
        p, _ = self.tokens.verify(request.headers["authorization"][7:], "control-plane")
        q = dict(request.url.params)
        self.calls.append(q)
        if self.fail == "down":
            raise httpx.ConnectError("connection refused", request=request)
        if self.fail is not None:
            return httpx.Response(self.fail, json=fake.error("INTERNAL", "An internal error occurred."))
        not_found = httpx.Response(404, json=fake.error("NOT_FOUND", "No such resource."))
        if request.url.path != "/internal/v1/agent-versions" or not p.can_access_project(q["project_id"]):
            return not_found
        if q["project_id"] != PROJECT or q["agent"] != AGENT or q["version"] not in self.versions:
            return not_found
        version = q["version"]
        return httpx.Response(
            200,
            json=fake.agent_version_detail(
                id=f"0190f3b4-0000-7000-8000-0000000{self.versions.index(version):05d}",
                agent_name=AGENT,
                project_id=PROJECT,
                version=version,
                manifest_sha256=manifest_sha256(version),
                prompt_sha256=hashlib.sha256(f"prompt:{version}".encode()).hexdigest(),
                tools=[],
            ),
        )


class FakeTraceService:
    """``POST /api/v1/traces/{id}/outcome``: the first post of every trace
    answers 404 (not ingested yet), later ones store the outcome. Answers are
    contract payloads and every exchange is checked against the trace
    service's contract (:attr:`checker`)."""

    def __init__(self, tokens: TokenService) -> None:
        self.tokens = tokens
        self.seen: set[str] = set()
        self.posts: list[dict[str, Any]] = []
        self.checker = fake.ExchangeChecker()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        response = self._answer(request)
        self.checker.check_exchange(request, response)
        return response

    def _answer(self, request: httpx.Request) -> httpx.Response:
        p, _ = self.tokens.verify(request.headers["authorization"][7:], "trace-service")
        project = request.url.params["project_id"]
        assert p.can_access_project(project)
        trace_id = request.url.path.split("/")[4]
        if trace_id not in self.seen:
            self.seen.add(trace_id)
            return httpx.Response(404, json=fake.error("NOT_FOUND", "No such resource."))
        body = json.loads(request.content)
        self.posts.append({"trace_id": trace_id, "project_id": project, "actor": p.actor, "body": body})
        return httpx.Response(200, json=fake.outcome(**body, recorded_by=p.actor))


def free_socket() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    return sock


class ASGIFake:
    """Serves one of the httpx fakes above as an ASGI app, so it can listen
    on a real socket for code that builds its own HTTP client."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.handler = handler

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return
        body = b""
        while True:
            msg = await receive()
            body += msg.get("body", b"")
            if not msg.get("more_body"):
                break
        query = scope["query_string"].decode()
        request = httpx.Request(
            scope["method"],
            "http://fake" + scope["path"] + ("?" + query if query else ""),
            headers=[(k.decode(), v.decode()) for k, v in scope["headers"]],
            content=body,
        )
        try:
            resp = self.handler(request)
        except Exception:  # noqa: BLE001 - a rejected credential is a 401, as in the real services
            resp = httpx.Response(401, json=fake.error("UNAUTHENTICATED", "Authentication is required."))
        await send(
            {
                "type": "http.response.start",
                "status": resp.status_code,
                "headers": [(k.encode(), v.encode()) for k, v in resp.headers.items()],
            }
        )
        await send({"type": "http.response.body", "body": resp.content})


@asynccontextmanager
async def serve_app(app: Any, sock: socket.socket | None = None) -> AsyncIterator[str]:
    """Serves an ASGI app with uvicorn on a real socket; yields its base URL."""
    sock = sock or free_socket()
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, log_config=None, access_log=False, lifespan="off", timeout_graceful_shutdown=2)
    )
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(500):
            if server.started:
                break
            await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await serving


@contextmanager
def demo_agent(
    tool_timeout_s: float = 2.0, manifest_dir: Path | None = None
) -> Iterator[tuple[AgentServer, InMemorySpanExporter]]:
    """The real demo agent behind its HTTP adapter (ADR-0011), in a thread,
    with its telemetry exported in memory. ``manifest_dir`` replaces the
    versions it can run (default: the demo's manifests)."""
    exporter = InMemorySpanExporter()
    telemetry = AgentTwin(
        Config(service_name="support-refund-agent", schedule_delay_ms=20, content_mode="redacted"),
        exporter=exporter,
    )
    agent = Agent(
        telemetry,
        ManifestStore(manifest_dir),
        tools_base_url="http://127.0.0.1:9/unused",
        model_factory=scripted_model_factory(INTERNAL_API_KEY),
        tool_timeout_s=tool_timeout_s,
        backoff_scale=0.01,
    )
    server = AgentServer(agent, token=AGENT_TOKEN).start()
    try:
        yield server, exporter
    finally:
        server.stop()
        telemetry.shutdown()


@dataclass
class Stack:
    url: str
    pool: Pool
    store: Store
    cfg: SimulationConfig
    tokens: TokenService
    worker: Worker
    control_plane: FakeControlPlane
    traces: FakeTraceService
    exporter: InMemorySpanExporter
    client: httpx.AsyncClient
    contract: Contract
    principal: Principal = field(
        default_factory=lambda: Principal(
            org_id=ORG, actor="user:engineer", role=Role.ENGINEER, project_ids=(PROJECT,)
        )
    )

    def token(self, p: Principal | None = None) -> str:
        return self.tokens.mint(p or self.principal, "simulation-service")

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

    async def ok(self, method: str, path: str, body: Any = None, *, status: int = 200, **params: Any) -> Any:
        resp = await self.call(method, path, body, **params)
        assert resp.status_code == status, (resp.status_code, resp.text)
        return resp.json()

    async def register_suite(self) -> list[str]:
        """The twin and every scenario of the demo's assurance suite."""
        names = sorted(p.stem for p in (ASSURANCE / "scenarios").glob("*.yaml"))
        await self.register_demo(*names)
        return names

    async def register_demo(self, *scenarios: str) -> None:
        await self.ok("POST", "/api/v1/twins", {"project_id": PROJECT, "yaml": twin_yaml()}, status=201)
        for name in scenarios:
            await self.ok(
                "POST", "/api/v1/scenarios", {"project_id": PROJECT, "yaml": scenario_yaml(name)}, status=201
            )

    async def start_run(self, version: str, *scenarios: str, seed: int | None = 7, **extra: Any) -> str:
        body = {"project_id": PROJECT, "agent": AGENT, "agent_version": version, "scenarios": list(scenarios)}
        if seed is not None:
            body["seed"] = seed
        body.update(extra)
        out = await self.ok("POST", "/api/v1/simulations", body, status=202)
        return str(out["run"]["id"])

    async def outbox(self, event_type: str) -> list[dict[str, Any]]:
        rows = await self.store.all(
            "SELECT envelope FROM outbox WHERE event_type = %s ORDER BY created_at", (event_type,)
        )
        return [r["envelope"] for r in rows]


async def running_cases(s: Stack, run_id: str, tokens: dict[str, str]) -> dict[str, str]:
    """Claims the run like a worker would and opens its cases with known
    tokens (scenario name -> token); returns scenario name -> case id."""
    claimed = await s.store.claim_next_run("test-worker", 60)
    assert claimed is not None and claimed["id"] == run_id
    async with transaction(s.pool) as conn:
        assert await s.store.transition(conn, run_id, JobStatus.RUNNING, "test", owner="test-worker")
    ids = {}
    for case in await s.store.pending_cases(run_id):
        definition = load_twin(case["twin_document"])
        state = CaseState.fresh(initial_state(definition, case["scenario_document"]))
        await s.store.start_case(case["id"], token_hash(tokens[case["scenario_name"]]), state.to_json())
        ids[case["scenario_name"]] = str(case["id"])
    return ids


@asynccontextmanager
async def simulation_stack(**overrides: Any) -> AsyncIterator[Stack]:
    async with temp_database() as url:
        pool = await connect(url, schema=SCHEMA, max_size=12)
        await Migrator(pool, SCHEMA, load_migrations(MIGRATIONS)).up()
        tokens = TokenService(SECRET)
        versions = tuple(overrides.pop("agent_versions", VERSIONS))
        embedder = overrides.pop("embedder", None)
        with demo_agent(
            float(overrides.pop("agent_tool_timeout_s", 2.0)), overrides.pop("manifest_dir", None)
        ) as (agent_server, exporter):
            sock = free_socket()
            port = sock.getsockname()[1]
            settings: dict[str, Any] = {
                "control_plane_url": "http://control-plane.test",
                "trace_service_url": "http://trace-service.test",
                "twin_public_url": f"http://127.0.0.1:{port}/twin/v1",
                "agent_endpoints": {AGENT: AgentEndpoint(url=agent_server.url, token_env="DEMO_AGENT_TOKEN")},
                "agent_tokens": {AGENT: AGENT_TOKEN},
                "lease_seconds": 5.0,
                "poll_interval_s": 0.2,
                "case_timeout_s": 30.0,
                "outcome_retry_s": 0.01,
            }
            settings.update(overrides)
            cfg = SimulationConfig(**settings)
            store = Store(pool)
            registry = default_registry()
            cp, ts = FakeControlPlane(tokens, versions), FakeTraceService(tokens)
            control = ControlPlaneClient(cfg.control_plane_url, tokens, transport=httpx.MockTransport(cp))
            traces = TraceServiceClient(cfg.trace_service_url, tokens, transport=httpx.MockTransport(ts))
            checker = contract()
            agents = AgentClient(transport=ContractTransport(checker, "agentRun"))
            app = build_app(
                service="simulation-service",
                version="test",
                health=Health(),
                tokens=tokens,
                audience="simulation-service",
            )
            SimulationAPI(
                store=store,
                cfg=cfg,
                registry=registry,
                control_plane=control,
                log=get_logger("api"),
                embedder=embedder,
            ).routes(app)
            TwinEndpoint(store, cfg).routes(app)
            try:
                async with (
                    serve_app(app, sock) as base,
                    httpx.AsyncClient(
                        base_url=base,
                        trust_env=False,
                        timeout=60,
                        event_hooks={"response": [checker.response_hook()]},
                    ) as client,
                ):
                    worker = Worker(
                        store=store,
                        cfg=cfg,
                        agents=agents,
                        traces=traces,
                        registry=registry,
                        log=get_logger("worker"),
                    )
                    try:
                        yield Stack(
                            url=base,
                            pool=pool,
                            store=store,
                            cfg=cfg,
                            tokens=tokens,
                            worker=worker,
                            control_plane=cp,
                            traces=ts,
                            exporter=exporter,
                            client=client,
                            contract=checker,
                        )
                    finally:
                        # The worker turns what its agent call raises into a
                        # failed run; a contract violation there is the root
                        # cause of whatever the test saw, so it is reported.
                        # So is one on the service's own calls to the control
                        # plane and the trace service.
                        violations = checker.violations + cp.checker.violations + ts.checker.violations
                        if violations:
                            raise ContractViolation("\n".join(violations))
            finally:
                await agents.close()
                await control.close()
                await traces.close()
                await pool.close()
