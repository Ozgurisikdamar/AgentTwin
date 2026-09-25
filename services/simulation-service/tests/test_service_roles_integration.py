"""The two roles of the service as they are deployed (agenttwin_core.service),
each with its own runtime, on a real PostgreSQL and a real RabbitMQ:

* ``serve``: the API, the twin endpoint and an outbox relay;
* ``worker``: claim loops woken by ``simulation.run_requested.v1`` from the
  broker, the lease janitor and the verified-outcome poster.

The control plane and the trace service are HTTP fakes on real sockets (they
verify the internal JWT of every call); the agent is the real demo agent.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import aio_pika
import httpx
import pytest
from prometheus_client.parser import text_string_to_metric_families

from agenttwin_core.auth import Principal, Role, TokenService
from agenttwin_core.config import Environment, Loader
from agenttwin_core.embeddings import HashingEmbedder
from agenttwin_core.logx import get_logger
from agenttwin_core.service import Runtime, ServiceConfig, start_runtime
from agenttwin_core.testing import amqp_url, temp_database
from agenttwin_core.web import build_app
from agenttwin_simulation.config import load_config
from agenttwin_simulation.main import NAME, SPEC, VERSION, build_serve, build_worker
from agenttwin_simulation.runner import CONSUMER, QUEUE
from sim_testutil import (
    AGENT,
    AGENT_TOKEN,
    ORG,
    PROJECT,
    SECRET,
    ASGIFake,
    FakeControlPlane,
    FakeTraceService,
    demo_agent,
    free_socket,
    scenario_yaml,
    serve_app,
    twin_yaml,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

HAPPY = "refund-happy-path"
TIMEOUT = "refund-timeout-after-mutation"
# simulation.run_completed.v1 is routed to the evaluation service's queue.
ROUTED = "evaluation-service.events"


async def purge(url: str, *queues: str) -> None:
    conn = await aio_pika.connect(url)
    try:
        ch = await conn.channel()
        for name in queues:
            for q in (name, name + ".retry", name + ".dlq"):
                queue = await ch.get_queue(q, ensure=False)
                await queue.purge()
    finally:
        await conn.close()


async def drain(url: str, queue: str) -> list[dict[str, Any]]:
    conn = await aio_pika.connect(url)
    out: list[dict[str, Any]] = []
    try:
        ch = await conn.channel()
        q = await ch.get_queue(queue, ensure=False)
        while (msg := await q.get(fail=False, no_ack=True)) is not None:
            out.append(json.loads(msg.body))
    finally:
        await conn.close()
    return out


def service_config(database_url: str, broker_url: str) -> ServiceConfig:
    return ServiceConfig(
        name=NAME,
        env=Environment.TEST,
        port=8084,
        log_level="info",
        database_url=database_url,
        amqp_url=broker_url,
        internal_secret=SECRET,
        otlp_endpoint="",
        sample_ratio=1.0,
        migrate_on_start=True,
        db_max_conns=8,
    )


async def wait_for(check: Any, timeout: float, what: str) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = await check()
        if value:
            return value
        await asyncio.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


class FakeEmbeddings:
    """An OpenAI-compatible ``/embeddings`` endpoint that embeds like
    hashing-v1 (a hosted model, as far as the service can tell)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, list[str]]] = []
        self.local = HashingEmbedder()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.calls.append((request.url.path, request.headers.get("authorization"), body["input"]))
        data = [{"index": i, "embedding": self.local.embed_one(t)} for i, t in enumerate(body["input"])]
        return httpx.Response(200, json={"object": "list", "data": data, "model": body["model"]})


async def test_serve_and_worker_roles_over_rabbitmq() -> None:
    broker_url = amqp_url()
    await purge(broker_url, QUEUE, ROUTED)
    tokens = TokenService(SECRET)
    control_plane, traces = FakeControlPlane(tokens), FakeTraceService(tokens)
    embeddings = FakeEmbeddings()
    engineer = Principal(org_id=ORG, actor="user:engineer", role=Role.ENGINEER, project_ids=(PROJECT,))
    runtimes: list[Runtime] = []
    async with (
        temp_database() as database_url,
        serve_app(ASGIFake(control_plane)) as control_plane_url,
        serve_app(ASGIFake(traces)) as trace_service_url,
        serve_app(ASGIFake(embeddings)) as embeddings_url,
    ):
        with demo_agent() as (agent_server, exporter):
            sock = free_socket()
            port = sock.getsockname()[1]
            # The service's configuration, read from its environment variables.
            loader = Loader(
                {
                    "APP_ENV": "test",
                    "CONTROL_PLANE_URL": control_plane_url,
                    "TRACE_SERVICE_URL": trace_service_url,
                    "SIMULATION_TWIN_PUBLIC_URL": f"http://127.0.0.1:{port}/twin/v1/",
                    "SIMULATION_AGENT_ENDPOINTS": json.dumps(
                        {AGENT: {"url": agent_server.url, "token_env": "DEMO_AGENT_TOKEN"}}
                    ),
                    "DEMO_AGENT_TOKEN": AGENT_TOKEN,
                    # Only an event can start a run quickly: the pollers sleep 60 s.
                    "SIMULATION_POLL_INTERVAL": "60s",
                    "SIMULATION_OUTCOME_RETRY": "100ms",
                    "SIMULATION_LEASE_DURATION": "10s",
                    # Scenario matching with a hosted embedding model.
                    "EMBEDDING_PROVIDER": "openai_compatible",
                    "EMBEDDING_MODEL": "fake-semantic-1",
                    "EMBEDDING_DIMENSIONS": "256",
                    "EMBEDDING_BASE_URL": embeddings_url + "/v1",
                    "EMBEDDING_API_KEY": "sk-fake-embeddings",
                }
            )
            cfg = load_config(loader)
            loader.raise_for_errors()
            assert cfg.twin_public_url == f"http://127.0.0.1:{port}/twin/v1"
            assert cfg.agent_tokens == {AGENT: AGENT_TOKEN}
            svc = service_config(database_url, broker_url)
            log = get_logger(NAME)
            try:
                serve_rt = await start_runtime(SPEC, svc, log, role="serve")
                runtimes.append(serve_rt)
                app = await build_serve(serve_rt, cfg)
                worker_rt = await start_runtime(SPEC, svc, log, role="worker")
                runtimes.append(worker_rt)
                assert await build_worker(worker_rt, cfg) is None
                # A pure worker serves health and metrics only.
                worker_app = build_app(
                    service=worker_rt.metrics.service,
                    version=VERSION,
                    health=worker_rt.health,
                    tokens=None,
                    audience=NAME,
                )
                async with (
                    serve_app(app, sock) as base,
                    httpx.AsyncClient(
                        base_url=base,
                        trust_env=False,
                        timeout=30,
                        headers={"Authorization": "Bearer " + tokens.mint(engineer, NAME)},
                    ) as api,
                    httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=worker_app), base_url="http://worker"
                    ) as worker_http,
                ):
                    await exercise(api, worker_http, serve_rt, broker_url, control_plane, traces)
                    # The configured embedding model selects scenarios.
                    r = await api.post(
                        "/api/v1/scenarios/match",
                        json={
                            "project_id": PROJECT,
                            "queries": [{"id": "p", "text": "refund timeout retry"}],
                        },
                    )
                    assert r.status_code == 200, r.text
                    assert r.json()["embedding_model"] == "fake-semantic-1"
                    assert [m["name"] for m in r.json()["scenarios"]] == [TIMEOUT]
                    [(path, auth, _), *_] = embeddings.calls
                    assert (path, auth) == ("/v1/embeddings", "Bearer sk-fake-embeddings")
                    assert sum(len(texts) for *_, texts in embeddings.calls) == 3  # two scenarios, one query
                    roots = [sp for sp in exporter.get_finished_spans() if sp.parent is None]
                    assert len(roots) == 2
            finally:
                for rt in reversed(runtimes):
                    await rt.close(timeout_s=5)
                await purge(broker_url, QUEUE, ROUTED)


async def exercise(
    api: httpx.AsyncClient,
    worker_http: httpx.AsyncClient,
    serve_rt: Runtime,
    broker_url: str,
    control_plane: FakeControlPlane,
    traces: FakeTraceService,
) -> None:
    for path in ("/health/ready",):
        ready = await api.get(path)
        assert ready.status_code == 200, ready.text
        assert set(ready.json()["checks"]) >= {"postgres", "rabbitmq"}
        assert (await worker_http.get(path)).status_code == 200

    r = await api.post("/api/v1/twins", json={"project_id": PROJECT, "yaml": twin_yaml()})
    assert r.status_code == 201, r.text
    for name in (HAPPY, TIMEOUT):
        r = await api.post("/api/v1/scenarios", json={"project_id": PROJECT, "yaml": scenario_yaml(name)})
        assert r.status_code == 201, r.text

    started = time.monotonic()
    r = await api.post(
        "/api/v1/simulations",
        json={"project_id": PROJECT, "agent": AGENT, "agent_version": "1.3.0", "scenarios": [HAPPY, TIMEOUT]},
    )
    assert r.status_code == 202, r.text
    run_id = r.json()["run"]["id"]
    # The version was resolved over HTTP with a token for the control plane.
    assert control_plane.calls[-1] == {"project_id": PROJECT, "agent": AGENT, "version": "1.3.0"}

    async def finished() -> dict[str, Any] | None:
        detail = (await api.get(f"/api/v1/simulations/{run_id}")).json()
        return detail if detail["run"]["status"] in ("COMPLETED", "FAILED", "CANCELLED") else None

    detail = await wait_for(finished, 45, "the run to finish")
    elapsed = time.monotonic() - started
    run = detail["run"]
    assert run["status"] == "COMPLETED", run
    assert (run["passed"], run["failed"], run["critical_failures"]) == (0, 2, 1)
    # Woken by the event (recorded in processed_event below), long before
    # the claim loops' 60 s poll would have picked the run up.
    assert elapsed < 20, elapsed

    # The request event went through the broker and was deduplicated by id.
    async with serve_rt.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id, event_type, published_at FROM outbox WHERE event_type = %s",
            ("simulation.run_requested.v1",),
        )
        [requested] = await cur.fetchall()
        assert requested["published_at"] is not None
        cur = await conn.execute(
            "SELECT count(*) AS n FROM processed_event WHERE consumer = %s AND event_id = %s",
            (CONSUMER, requested["id"]),
        )
        assert (await cur.fetchone()) == {"n": 1}

    # The completion event is published (relay) and routed to its consumer.
    async def routed() -> list[dict[str, Any]]:
        return await drain(broker_url, "evaluation-service.events")

    [completed] = await wait_for(routed, 15, "the completion event on the broker")
    assert completed["type"] == "simulation.run_completed.v1"
    assert completed["payload"]["run_id"] == run_id and completed["payload"]["failed"] == 2

    # The worker reports both verified outcomes (after the first 404s).
    async def reported() -> bool:
        return len(traces.posts) == 2

    await wait_for(reported, 20, "the verified outcomes")
    statuses = sorted(p["body"]["status"] for p in traces.posts)
    assert statuses == ["FAILURE", "FAILURE"]
    assert {p["actor"] for p in traces.posts} == {"service:simulation-service"}

    # Metrics of the run, the twin and the outcomes (process-wide registry).
    samples: dict[tuple[str, frozenset[tuple[str, str]]], float] = {}

    def value(name: str, **labels: str) -> float:
        return sum(v for (n, ls), v in samples.items() if n == name and set(labels.items()) <= ls)

    async def scraped() -> bool:
        samples.clear()
        for family in text_string_to_metric_families((await worker_http.get("/metrics")).text):
            for sample in family.samples:
                samples[(sample.name, frozenset(sample.labels.items()))] = sample.value
        return value("agenttwin_simulation_outcomes_total", result="posted") >= 2

    # The fake trace service records a post when it arrives; the worker counts it
    # only after marking the case reported, so the counter can trail by a moment.
    await wait_for(scraped, 10, "both posted outcomes in the metrics")

    assert value("agenttwin_simulation_runs_total", status="COMPLETED") >= 1
    assert value("agenttwin_simulation_cases_total", status="FAILED") >= 2
    assert value("agenttwin_twin_calls_total", fault="timeout_after_mutation") >= 1
    assert value("agenttwin_twin_calls_total", fault="none", status="ok") >= 1
    assert value("agenttwin_simulation_outcomes_total", result="posted") >= 2
    assert value("agenttwin_simulation_outcomes_total", result="retry") >= 2
    assert value("agenttwin_simulation_case_duration_seconds_count") >= 2
    # Tool names never become labels (they come from user-authored twins).
    assert not any("tool" in dict(ls) for _, ls in samples)
    # The service's calls to the control plane and the trace service, and the
    # fakes' answers, are what those services' contracts document.
    violations = control_plane.checker.violations + traces.checker.violations
    assert not violations, "\n".join(violations)
    assert {"findAgentVersionInternal", "recordOutcome"} <= (
        control_plane.checker.succeeded() | traces.checker.succeeded()
    )
