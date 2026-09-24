"""REST client and outcome reporting against a local stub of the API."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from agenttwin import APIError, Client, Config, OutcomeReportError, report_outcome

KEY = "atk_test0000_secret-value-that-must-not-leak"
PROJECT = "0197a0c4-7b8e-7000-8000-000000000001"


class Stub:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.ready_after = 0  # number of 503s before /health/ready answers 200
        self.polls = 0

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
        if h.path == "/health/ready":
            if self.ready_after > 0:
                self.ready_after -= 1
                return 503, {"status": "unready"}
            return 200, {"status": "ready"}
        if h.headers.get("X-AgentTwin-Api-Key") != KEY:
            return 401, {"error": {"code": "UNAUTHORIZED", "message": "Authentication required."}}
        if h.command == "GET" and h.path == "/api/v1/projects":
            return 200, {"items": [{"id": PROJECT, "slug": "support", "name": "Customer Support"}]}
        if h.command == "POST" and h.path.startswith(f"/api/v1/projects/{PROJECT}/agent-manifests"):
            if b"version: 9.9.9" in body:
                return 409, {"error": {"code": "VERSION_EXISTS", "message": "immutable", "details": {"v": 1}}}
            return 201, {"created": True, "version": {"version": "1.0.0"}}
        if h.command == "POST" and h.path.endswith("/outcome"):
            return 200, {"status": json.loads(body)["status"], "recorded": True}
        if h.command == "POST" and h.path in ("/api/v1/twins", "/api/v1/scenarios"):
            doc = json.loads(body)
            if "kind: Broken" in doc["yaml"]:
                return 400, {
                    "error": {
                        "code": "SCENARIO_INVALID",
                        "message": "invalid",
                        "details": {"problems": ["x"]},
                    }
                }
            return 201, {"created": True, "project": doc["project_id"]}
        if h.command == "POST" and h.path == "/api/v1/scenarios/validate":
            return 200, {"valid": True, "problems": [], "warnings": []}
        if h.command == "POST" and h.path == "/api/v1/simulations":
            return 202, {"run": {"id": "run-1", "status": "QUEUED"}, "request": json.loads(body)}
        if h.command == "GET" and h.path == "/api/v1/simulations/run-1":
            self.polls += 1
            return 200, {"run": {"id": "run-1", "status": "COMPLETED" if self.polls >= 3 else "RUNNING"}}
        if h.command == "GET" and h.path == "/api/v1/simulations/stuck":
            return 200, {"run": {"id": "stuck", "status": "RUNNING"}}
        return 404, {"error": {"code": "NOT_FOUND", "message": "Not found."}}


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
    yield state, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_client_authenticates_and_resolves_projects(stub: tuple[Stub, str]) -> None:
    state, url = stub
    client = Client(url + "/", KEY)
    assert client.project_id("support") == PROJECT
    assert client.project_id(PROJECT) == PROJECT
    assert state.requests[0]["headers"]["x-agenttwin-api-key"] == KEY
    with pytest.raises(APIError) as e:
        client.project_id("billing")
    assert e.value.status == 404 and e.value.code == "PROJECT_NOT_FOUND"


def test_register_manifest_sends_yaml_and_commit_metadata(stub: tuple[Stub, str]) -> None:
    state, url = stub
    client = Client(url, KEY)
    res = client.register_manifest(PROJECT, "name: a\nversion: 1.0.0\n", commit_sha="abc1234", branch="main")
    assert res["created"] is True
    req = state.requests[-1]
    assert req["headers"]["content-type"] == "application/yaml"
    assert req["path"].endswith("agent-manifests?commit_sha=abc1234&branch=main")
    with pytest.raises(APIError) as e:
        client.register_manifest(PROJECT, "name: a\nversion: 9.9.9\n")
    assert (e.value.status, e.value.code, e.value.details) == (409, "VERSION_EXISTS", {"v": 1})


def test_errors_never_leak_the_key(stub: tuple[Stub, str]) -> None:
    _, url = stub
    bad = Client(url, "atk_wrong000_other-secret")
    with pytest.raises(APIError) as e:
        bad.projects()
    assert e.value.status == 401 and e.value.code == "UNAUTHORIZED"
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
    assert out == {"status": "FAILURE", "recorded": True}
    req = state.requests[-1]
    assert req["path"] == f"/api/v1/traces/{trace_id}/outcome"
    assert json.loads(req["body"]) == {
        "status": "FAILURE",
        "verified": True,
        "verification_source": "state_assertion",
        "claimed_status": "SUCCESS",
        "actual_state": {"refund_count": 2},
    }
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
    assert client.register_twin(PROJECT, "kind: TwinDefinition") == {"created": True, "project": PROJECT}
    assert json.loads(state.requests[-1]["body"]) == {"project_id": PROJECT, "yaml": "kind: TwinDefinition"}
    assert client.validate_scenario(PROJECT, "kind: Scenario")["valid"] is True
    assert client.save_scenario(PROJECT, "kind: Scenario")["created"] is True
    with pytest.raises(APIError) as e:
        client.save_scenario(PROJECT, "kind: Broken")
    assert (e.value.status, e.value.code, e.value.details) == (400, "SCENARIO_INVALID", {"problems": ["x"]})

    started = client.start_simulation(PROJECT, "agent", "1.2.4", tags=["smoke"], seed=7, release_id="rel-1")
    assert started["request"] == {
        "project_id": PROJECT,
        "agent": "agent",
        "agent_version": "1.2.4",
        "tags": ["smoke"],
        "seed": 7,
        "release_id": "rel-1",
    }
    done = client.wait_for_simulation("run-1", timeout_s=10, interval_s=0.01)
    assert done["run"]["status"] == "COMPLETED" and state.polls == 3
    with pytest.raises(APIError) as e:
        client.wait_for_simulation("stuck", timeout_s=0.05, interval_s=0.01)
    assert e.value.code == "TIMEOUT"
