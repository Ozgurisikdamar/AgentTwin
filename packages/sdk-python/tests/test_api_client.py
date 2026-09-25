"""REST client and outcome reporting against a local stub of the API.

The stub answers as the services' contracts document
(``packages/contracts/openapi``: control plane, trace service, simulation
service), and every exchange is checked against the contract of the service
that owns the path (ADR-0021): a request the service would reject, or a stub
answer the service could not give, fails the test that sees it.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from agenttwin import APIError, Client, Config, OutcomeReportError, report_outcome
from agenttwin_core import api_fakes as fake

KEY = "atk_test0000_secret-value-that-must-not-leak"
PROJECT = fake.PROJECT
RUN = fake.uuid(0xA001)
STUCK = fake.uuid(0xA002)


class Stub:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.ready_after = 0  # number of 503s before /health/ready answers 200
        self.polls = 0
        self.checker = fake.ExchangeChecker()

    def check(self, h: BaseHTTPRequestHandler, body: bytes, status: int, payload: Any) -> None:
        self.checker.check(h.command, h.path, dict(h.headers.items()), body, status, payload)

    def handle(self, h: BaseHTTPRequestHandler) -> tuple[int, Any]:
        length = int(h.headers.get("Content-Length") or 0)
        body = h.rfile.read(length) if length else b""
        self.requests.append(
            {
                "method": h.command,
                "path": h.path,
                "headers": {k.lower(): v for k, v in h.headers.items()},
                "body": body,
            }
        )
        status, payload = self.answer(h, body)
        self.check(h, body, status, payload)
        return status, payload

    def answer(self, h: BaseHTTPRequestHandler, body: bytes) -> tuple[int, Any]:
        if h.path == "/health/ready":
            if self.ready_after > 0:
                self.ready_after -= 1
                return 503, {"status": "unready"}
            return 200, {"status": "ready"}
        if h.headers.get("X-AgentTwin-Api-Key") != KEY:
            return 401, fake.error("UNAUTHENTICATED", "Authentication is required.")
        if h.command == "GET" and h.path == "/api/v1/projects":
            return 200, {"items": [fake.project()]}
        if h.command == "POST" and h.path.startswith(f"/api/v1/projects/{PROJECT}/agent-manifests"):
            if b"version: 9.9.9" in body:
                return 409, fake.error("VERSION_EXISTS", "Versions are immutable.", v=1)
            return 201, fake.registered_version(version="1.0.0")
        if h.command == "POST" and h.path.endswith("/outcome"):
            sent = json.loads(body)
            return 200, fake.outcome(**sent, recorded_by="apikey:test")
        if h.command == "POST" and h.path == "/api/v1/twins":
            return 201, {"twin": fake.twin(), "created": True}
        if h.command == "POST" and h.path == "/api/v1/scenarios":
            if "kind: Broken" in json.loads(body)["yaml"]:
                return 400, fake.error("SCENARIO_INVALID", "invalid", problems=["x"])
            return 201, {"scenario": fake.scenario(), "version": 1, "created": True, "warnings": []}
        if h.command == "POST" and h.path == "/api/v1/scenarios/validate":
            return 200, {
                "valid": True,
                "problems": [],
                "warnings": [],
                "spec_hash": "b" * 64,
                "twin": fake.twin_summary(),
            }
        if h.command == "POST" and h.path == "/api/v1/simulations":
            req = json.loads(body)
            queued = fake.run(
                id=RUN, agent_name=req["agent"], agent_version=req["agent_version"], case_count=1
            )
            queued["pinning"]["seed"] = req.get("seed", 42)
            return 202, {"run": queued, "cases": [fake.queued_case(0, "refund-happy-path")]}
        if h.command == "GET" and h.path in (f"/api/v1/simulations/{RUN}", f"/api/v1/simulations/{STUCK}"):
            status = "RUNNING"
            if h.path.endswith(RUN):
                self.polls += 1
                status = "COMPLETED" if self.polls >= 3 else "RUNNING"
            return 200, fake.run_detail(fake.run(id=h.path.rsplit("/", 1)[1], status=status), [])
        return 404, fake.error("NOT_FOUND", "Not found.")


@pytest.fixture
def stub() -> Iterator[tuple[Stub, str]]:
    state = Stub()

    class Handler(BaseHTTPRequestHandler):
        def _serve(self) -> None:
            status, payload = state.handle(self)
            raw = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_GET = do_POST = _serve

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield state, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
    assert not state.checker.violations, "\n".join(state.checker.violations)


def test_client_authenticates_and_resolves_projects(stub: tuple[Stub, str]) -> None:
    state, url = stub
    client = Client(url + "/", KEY)
    assert client.project_id("support") == PROJECT
    assert client.project_id(PROJECT) == PROJECT
    assert state.requests[0]["headers"]["x-agenttwin-api-key"] == KEY
    with pytest.raises(APIError) as e:
        client.project_id("billing")
    assert e.value.status == 404 and e.value.code == "PROJECT_NOT_FOUND"
    assert "listProjects" in state.checker.succeeded()


def test_register_manifest_sends_yaml_and_commit_metadata(stub: tuple[Stub, str]) -> None:
    state, url = stub
    client = Client(url, KEY)
    res = client.register_manifest(PROJECT, "name: a\nversion: 1.0.0\n", commit_sha="abc1234", branch="main")
    assert res["created"] is True and res["version"]["version"] == "1.0.0"
    req = state.requests[-1]
    assert req["headers"]["content-type"] == "application/yaml"
    assert req["path"].endswith("agent-manifests?commit_sha=abc1234&branch=main")
    with pytest.raises(APIError) as e:
        client.register_manifest(PROJECT, "name: a\nversion: 9.9.9\n")
    assert (e.value.status, e.value.code, e.value.details) == (409, "VERSION_EXISTS", {"v": 1})
    assert "registerManifest" in state.checker.succeeded()


def test_errors_never_leak_the_key(stub: tuple[Stub, str]) -> None:
    _, url = stub
    bad = Client(url, "atk_wrong000_other-secret")
    with pytest.raises(APIError) as e:
        bad.projects()
    assert e.value.status == 401 and e.value.code == "UNAUTHENTICATED"
    assert "other-secret" not in str(e.value) and "other-secret" not in repr(bad)
    down = Client("http://127.0.0.1:1", KEY, timeout_s=1)
    with pytest.raises(APIError) as e:
        down.projects()
    assert e.value.status == 0 and e.value.code == "UNAVAILABLE"
    assert KEY not in str(e.value)


def test_client_validates_configuration() -> None:
    with pytest.raises(ValueError):
        Client("ftp://example.com", KEY)
    with pytest.raises(ValueError):
        Client("http://example.com", "")
    with pytest.raises(ValueError):
        Client.from_config(Config())
    with pytest.raises(ValueError):
        Client("http://example.com", KEY).request("GET", "api/v1/projects")


def test_wait_ready_polls_until_ready(stub: tuple[Stub, str]) -> None:
    state, url = stub
    state.ready_after = 2
    Client(url, KEY).wait_ready(timeout_s=10, interval_s=0.01)
    assert sum(r["path"] == "/health/ready" for r in state.requests) == 3
    state.ready_after = 10_000
    with pytest.raises(APIError) as e:
        Client(url, KEY).wait_ready(timeout_s=0.2, interval_s=0.01)
    assert e.value.code == "NOT_READY"


def test_report_outcome_round_trip_and_validation(stub: tuple[Stub, str]) -> None:
    state, url = stub
    cfg = Config(api_url=url, api_key=KEY)
    trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
    out = report_outcome(
        trace_id.upper(),
        "FAILURE",
        config=cfg,
        verified=True,
        verification_source="state_assertion",
        claimed_status="SUCCESS",
        actual_state={"refund_count": 2},
    )
    # The stored outcome: the verification disproved what the agent claimed.
    assert (out["status"], out["claimed_status"], out["contradiction"]) == ("FAILURE", "SUCCESS", True)
    req = state.requests[-1]
    assert req["path"] == f"/api/v1/traces/{trace_id}/outcome"
    assert json.loads(req["body"]) == {
        "status": "FAILURE",
        "verified": True,
        "verification_source": "state_assertion",
        "claimed_status": "SUCCESS",
        "actual_state": {"refund_count": 2},
    }
    assert "recordOutcome" in state.checker.succeeded()
    with pytest.raises(ValueError):
        report_outcome("not-a-trace", "SUCCESS", config=cfg)
    with pytest.raises(ValueError):
        report_outcome(trace_id, "ESCALATED", config=cfg)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        report_outcome(trace_id, "SUCCESS", config=cfg, verification_source="self_report")  # type: ignore[arg-type]
    bad = Config(api_url=url, api_key="atk_wrong000_x")
    with pytest.raises(OutcomeReportError) as e:
        report_outcome(trace_id, "SUCCESS", config=bad)
    assert isinstance(e.value, APIError) and e.value.status == 401


def test_twins_scenarios_and_simulations(stub: tuple[Stub, str]) -> None:
    state, url = stub
    client = Client(url, KEY)
    registered = client.register_twin(PROJECT, "kind: TwinDefinition")
    assert registered["created"] is True and registered["twin"]["project_id"] == PROJECT
    assert json.loads(state.requests[-1]["body"]) == {"project_id": PROJECT, "yaml": "kind: TwinDefinition"}
    assert client.validate_scenario(PROJECT, "kind: Scenario")["valid"] is True
    assert client.save_scenario(PROJECT, "kind: Scenario")["created"] is True
    with pytest.raises(APIError) as e:
        client.save_scenario(PROJECT, "kind: Broken")
    assert (e.value.status, e.value.code, e.value.details) == (400, "SCENARIO_INVALID", {"problems": ["x"]})

    started = client.start_simulation(
        PROJECT, "agent", "1.2.4", scenarios=["refund-happy-path"], tags=["smoke"], seed=7, release_id="rel-1"
    )
    assert started["run"]["status"] == "QUEUED" and started["run"]["pinning"]["seed"] == 7
    assert json.loads(state.requests[-1]["body"]) == {
        "project_id": PROJECT,
        "agent": "agent",
        "agent_version": "1.2.4",
        "scenarios": ["refund-happy-path"],
        "tags": ["smoke"],
        "seed": 7,
        "release_id": "rel-1",
    }
    assert "idempotency-key" not in state.requests[-1]["headers"]
    client.start_simulation(PROJECT, "agent", "1.2.4", idempotency_key="ci-build-42:1.2.4")
    assert state.requests[-1]["headers"]["idempotency-key"] == "ci-build-42:1.2.4"
    done = client.wait_for_simulation(RUN, timeout_s=10, interval_s=0.01)
    assert done["run"]["status"] == "COMPLETED" and state.polls == 3
    with pytest.raises(APIError) as e:
        client.wait_for_simulation(STUCK, timeout_s=0.05, interval_s=0.01)
    assert e.value.code == "TIMEOUT"
    # Every simulation API call the client makes was checked against the contract.
    used = {"registerTwin", "validateScenario", "saveScenario", "startSimulation", "getSimulation"}
    assert used <= state.checker.succeeded()


@pytest.mark.parametrize(
    ("call", "violation"),
    [
        # A seed the service does not accept (the contract bounds it to uint32).
        (lambda c: c.start_simulation(PROJECT, "agent", "1.2.4", seed=2**32), "seed"),
        # An idempotency key the edge rejects (8-128 of [A-Za-z0-9._:-]).
        (
            lambda c: c.start_simulation(PROJECT, "agent", "1.2.4", idempotency_key="bad key!"),
            "idempotency-key",
        ),
        # A run id that is not one.
        (lambda c: c.simulation("run-1"), "run_id"),
    ],
)
def test_requests_the_service_would_reject_are_caught(call: Any, violation: str) -> None:
    """The contract check has teeth: the stub accepts these requests, the
    service would not, so the check reports them."""
    state = Stub()

    class Handler(BaseHTTPRequestHandler):
        def _serve(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            # A valid answer, so what is reported is the request.
            status, payload = (202, {"run": fake.run(), "cases": []})
            if self.command == "GET":
                status, payload = (200, fake.run_detail(fake.run(status="RUNNING"), []))
            state.check(self, body, status, payload)
            raw = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_GET = do_POST = _serve

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        call(Client(f"http://127.0.0.1:{server.server_address[1]}", KEY))
    finally:
        server.shutdown()
        server.server_close()
    assert any(violation in v for v in state.checker.violations), state.checker.violations
