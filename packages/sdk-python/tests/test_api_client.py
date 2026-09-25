"""REST client and outcome reporting against a local stub of the API.

The stub answers as the services' contracts document
(``packages/contracts/openapi``: control plane, trace service, simulation
and evaluation services), and every exchange is checked against the contract of the service
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
EVAL = fake.uuid(0xA003)
DATASET = fake.uuid(0xA004)
CHANGE_SET = fake.uuid(0xC101)
RELEASE = fake.uuid(0xD101)
REGRESSION = fake.uuid(0xE001)


class Stub:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.ready_after = 0  # number of 503s before /health/ready answers 200
        self.polls = 0
        self.eval_polls = 0
        self.gate_polls = 0
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
        return self.changes(h, body)

    def changes(self, h: BaseHTTPRequestHandler, body: bytes) -> tuple[int, Any]:
        if h.command == "POST" and h.path == f"/api/v1/projects/{PROJECT}/imports/openapi":
            req = json.loads(body)
            return 201, fake.imported_catalog(name=req["name"], service=req.get("service"))
        if h.command == "POST" and h.path == f"/api/v1/projects/{PROJECT}/imports/mcp":
            req = json.loads(body)
            return 200, fake.imported_catalog(
                created=False, source="MCP", name=req["server"]["name"], spec_version="2026-07-28"
            )
        if h.command == "POST" and h.path == f"/api/v1/projects/{PROJECT}/change-sets":
            req = json.loads(body)
            if req["base_version"] == req["candidate_version"]:
                return 400, fake.error("VALIDATION_FAILED", "The versions must differ.")
            return 201, fake.change_set(
                base_version=req["base_version"], candidate_version=req["candidate_version"]
            )
        if h.command == "GET" and h.path == f"/api/v1/change-sets/{CHANGE_SET}/impact":
            return 200, fake.change_impact([fake.impact_scenario("refund-happy-path")])
        return self.evaluation(h, body)

    def evaluation(self, h: BaseHTTPRequestHandler, body: bytes) -> tuple[int, Any]:
        suite = fake.dataset(id=DATASET)
        if h.command == "GET" and h.path.startswith("/api/v1/datasets?"):
            return 200, {"items": [suite | {"case_count": 2}], "next_cursor": None}
        if h.command == "POST" and h.path == "/api/v1/datasets":
            req = json.loads(body)
            if req["name"] == "taken":
                return 409, fake.error("DATASET_EXISTS", "This project already has a dataset named 'taken'.")
            names = [c["scenario"] for c in req["cases"]]
            return 201, fake.dataset_detail(suite | {"name": req["name"]}, names)
        if h.command == "POST" and h.path == f"/api/v1/datasets/{DATASET}/cases":
            names = [c["scenario"] for c in json.loads(body)["cases"]]
            return 201, fake.dataset_detail(suite | {"latest_version": 2}, names)
        if h.command == "GET" and h.path.startswith(f"/api/v1/datasets/{DATASET}"):
            return 200, fake.dataset_detail(suite, ["refund-happy-path"])
        if h.command == "POST" and h.path == "/api/v1/eval-runs":
            req = json.loads(body)
            dataset = {"id": DATASET, "name": suite["name"], "version": 1} if "dataset_id" in req else None
            selection = {
                "scenarios": req.get("scenarios"),
                "tags": req.get("tags"),
                "dataset": dataset,
                "scenario_versions": None,
            }
            return 202, {"run": fake.eval_run(id=EVAL, seed=req.get("seed"), selection=selection)}
        if h.command == "GET" and h.path == f"/api/v1/eval-runs/{EVAL}":
            self.eval_polls += 1
            status = "COMPLETED" if self.eval_polls >= 2 else "RUNNING"
            return 200, fake.eval_run_detail(fake.eval_run(id=EVAL, status=status))
        return self.releases(h, body)

    def releases(self, h: BaseHTTPRequestHandler, body: bytes) -> tuple[int, Any]:
        decided = fake.release_gate("BLOCK")
        if h.command == "POST" and h.path == "/api/v1/releases":
            req = json.loads(body)
            if req["baseline_version"] == req["candidate_version"]:
                return 400, fake.error("VALIDATION_FAILED", "The versions must differ.")
            gate = fake.release_gate() if req.get("evaluate", True) else None
            release = fake.release(
                gate, baseline_version=req["baseline_version"], candidate_version=req["candidate_version"]
            )
            return 201, {"release": release, "gate": gate}
        if h.command == "GET" and h.path.startswith("/api/v1/releases?"):
            return 200, {"items": [fake.release(decided)], "next_cursor": None}
        if h.command == "GET" and h.path == f"/api/v1/releases/{RELEASE}":
            return 200, {"release": fake.release(decided), "revisions": [fake.gate_summary(decided)]}
        if h.command == "POST" and h.path == f"/api/v1/releases/{RELEASE}/evaluate":
            return 202, fake.release_gate(revision=2)
        if h.command == "GET" and h.path.startswith(f"/api/v1/releases/{RELEASE}/gate"):
            self.gate_polls += 1
            revision = int(h.path.partition("revision=")[2] or 1)
            if self.gate_polls < 2:
                return 200, fake.release_gate(revision=revision)
            return 200, fake.release_gate("BLOCK", revision=revision)
        return self.regressions(h, body)

    def regressions(self, h: BaseHTTPRequestHandler, body: bytes) -> tuple[int, Any]:
        base = f"/api/v1/regressions/{REGRESSION}"
        group = fake.regression(id=REGRESSION)
        if h.command == "GET" and h.path.startswith("/api/v1/regressions/candidates?"):
            return 200, {"items": [group], "next_cursor": None}
        if h.command == "GET" and h.path == base:
            return 200, fake.regression_detail(group)
        if h.command == "GET" and h.path == f"{base}/draft":
            return 200, fake.regression_draft(group)
        if h.command == "PATCH" and h.path == base:
            changes = {k: v for k, v in json.loads(body).items() if k != "reason"}
            return 200, {"regression": group | changes | {"triaged_by": "apikey:test"}}
        if h.command == "POST" and h.path in (f"{base}/confirm", f"{base}/dismiss", f"{base}/reopen"):
            req = json.loads(body)
            if h.path.endswith(("dismiss", "reopen")) and not req.get("reason"):
                return 400, fake.error("INVALID_REQUEST", "A reason is required.", field="reason")
            status = {"confirm": "CONFIRMED", "dismiss": "DISMISSED", "reopen": "REOPENED"}
            return 200, {"regression": group | {"status": status[h.path.rsplit("/", 1)[1]]}}
        if h.command == "POST" and h.path == f"{base}/merge":
            into = json.loads(body)["into"]
            merged = group | {"merged_into": into, "occurrence_count": 0}
            return 200, {"regression": fake.regression(id=into, occurrence_count=2), "merged": merged}
        if h.command == "POST" and h.path == f"{base}/promote":
            return 201, fake.promoted_regression(group)
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

        do_GET = do_POST = do_PATCH = _serve

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


def test_datasets_and_evaluation_runs(stub: tuple[Stub, str]) -> None:
    state, url = stub
    client = Client(url, KEY)
    [found] = client.datasets(PROJECT, name="refund-regression-suite")
    assert found["id"] == DATASET
    assert client.datasets(PROJECT, name="another-suite") == []
    assert "q=another-suite" in state.requests[-1]["path"]

    created = client.create_dataset(
        PROJECT,
        "smoke",
        ["refund-happy-path", {"scenario": "refund-over-limit", "tags": ["limits"]}],
        description="The smoke suite.",
        tags=["smoke"],
    )
    assert created["dataset"]["name"] == "smoke" and created["version"]["case_count"] == 2
    assert json.loads(state.requests[-1]["body"]) == {
        "project_id": PROJECT,
        "name": "smoke",
        "cases": [{"scenario": "refund-happy-path"}, {"scenario": "refund-over-limit", "tags": ["limits"]}],
        "description": "The smoke suite.",
        "tags": ["smoke"],
    }
    with pytest.raises(APIError) as e:
        client.create_dataset(PROJECT, "taken", ["refund-happy-path"])
    assert (e.value.status, e.value.code) == (409, "DATASET_EXISTS")
    added = client.add_dataset_cases(DATASET, ["refund-rate-limited"], note="rate limits")
    assert added["dataset"]["latest_version"] == 2
    assert json.loads(state.requests[-1]["body"]) == {
        "cases": [{"scenario": "refund-rate-limited"}],
        "note": "rate limits",
    }
    assert client.dataset(DATASET, version=1)["version"]["version"] == 1
    assert state.requests[-1]["path"] == f"/api/v1/datasets/{DATASET}?version=1"

    started = client.start_eval_run(
        PROJECT,
        "support-refund-agent",
        "1.2.4",
        "1.3.0",
        dataset_id=DATASET,
        seed=42,
        idempotency_key="ci-7:eval",
    )
    assert started["run"]["status"] == "QUEUED" and started["run"]["selection"]["dataset"]["id"] == DATASET
    assert json.loads(state.requests[-1]["body"]) == {
        "project_id": PROJECT,
        "agent": "support-refund-agent",
        "baseline_version": "1.2.4",
        "candidate_version": "1.3.0",
        "dataset_id": DATASET,
        "seed": 42,
    }
    assert state.requests[-1]["headers"]["idempotency-key"] == "ci-7:eval"
    client.start_eval_run(PROJECT, "support-refund-agent", "1.2.4", "1.3.0", scenarios=["refund-happy-path"])
    assert json.loads(state.requests[-1]["body"])["scenarios"] == ["refund-happy-path"]
    done = client.wait_for_eval_run(EVAL, timeout_s=10, interval_s=0.01)
    assert done["run"]["status"] == "COMPLETED" and state.eval_polls == 2
    used = {"listDatasets", "createDataset", "addDatasetCases", "getDataset", "startEvalRun", "getEvalRun"}
    assert used <= state.checker.succeeded()


def test_tool_catalogs_and_change_impact(stub: tuple[Stub, str]) -> None:
    state, url = stub
    client = Client(url, KEY)
    document = "openapi: 3.1.0\ninfo: {title: Payments, version: '1'}\npaths: {}\n"
    imported = client.import_openapi(PROJECT, "payments-api", document, service="payments-api")
    assert imported["created"] is True and imported["service"] == "payments-api"
    # The document goes as the text it was read as; unset options are left out.
    assert json.loads(state.requests[-1]["body"]) == {
        "name": "payments-api",
        "document": document,
        "service": "payments-api",
    }
    client.import_openapi(
        PROJECT,
        "orders-api",
        {"openapi": "3.1.0"},
        risk_overrides={"getOrder": "READ"},
        names={"getOrder": "lookup_order"},
    )
    assert json.loads(state.requests[-1]["body"]) == {
        "name": "orders-api",
        "document": {"openapi": "3.1.0"},
        "risk_overrides": {"getOrder": "READ"},
        "names": {"getOrder": "lookup_order"},
    }
    tools = [{"name": "sendEmail", "inputSchema": {"type": "object"}}]
    again = client.import_mcp(
        PROJECT,
        {"name": "support-desk", "url": "https://desk.example/mcp"},
        tools,
        protocol_version="2026-07-28",
        risk_overrides={"sendEmail": "WRITE_REVERSIBLE"},
    )
    assert again["created"] is False and again["source"] == "MCP"
    assert json.loads(state.requests[-1]["body"]) == {
        "server": {"name": "support-desk", "url": "https://desk.example/mcp"},
        "tools": tools,
        "protocol_version": "2026-07-28",
        "risk_overrides": {"sendEmail": "WRITE_REVERSIBLE"},
    }
    client.import_mcp(PROJECT, {"name": "support-desk"}, tools, trust_annotations=True)
    assert json.loads(state.requests[-1]["body"])["trust_annotations"] is True

    cs = client.create_change_set(
        PROJECT,
        "support-refund-agent",
        "1.3.1",
        "1.3.2",
        title="Refund tool: idempotency key required",
        git={"changed_files": ["manifests/1.3.2.yaml"]},
    )
    assert cs["id"] == CHANGE_SET and cs["candidate"]["version"] == "1.3.2"
    assert json.loads(state.requests[-1]["body"]) == {
        "agent": "support-refund-agent",
        "base_version": "1.3.1",
        "candidate_version": "1.3.2",
        "title": "Refund tool: idempotency key required",
        "git": {"changed_files": ["manifests/1.3.2.yaml"]},
    }
    with pytest.raises(APIError) as e:
        client.create_change_set(PROJECT, "support-refund-agent", "1.3.1", "1.3.1")
    assert (e.value.status, e.value.code) == (400, "VALIDATION_FAILED")
    impact = client.change_set_impact(CHANGE_SET)
    assert impact["complete"] is True and [s["name"] for s in impact["scenarios"]] == ["refund-happy-path"]
    used = {"importOpenApi", "importMcp", "createChangeSet", "getChangeSetImpact"}
    assert used <= state.checker.succeeded()


def test_releases_and_their_gates(stub: tuple[Stub, str]) -> None:
    state, url = stub
    client = Client(url, KEY)
    created = client.create_release(
        PROJECT,
        "support-refund-agent",
        "1.2.4",
        "1.3.0",
        title="Refund flow rewrite",
        git={"candidate_commit": "4e5f6a7b"},
        ci_url="https://ci.example.com/runs/42",
        idempotency_key="release-1.2.4-1.3.0",
    )
    assert created["release"]["id"] == RELEASE and created["gate"]["effective_outcome"] == "PENDING"
    sent = state.requests[-1]
    assert sent["headers"]["idempotency-key"] == "release-1.2.4-1.3.0"
    # Unset options are left out: the service then evaluates at once.
    assert json.loads(sent["body"]) == {
        "project_id": PROJECT,
        "agent": "support-refund-agent",
        "baseline_version": "1.2.4",
        "candidate_version": "1.3.0",
        "title": "Refund flow rewrite",
        "git": {"candidate_commit": "4e5f6a7b"},
        "ci_url": "https://ci.example.com/runs/42",
    }
    unevaluated = client.create_release(PROJECT, "support-refund-agent", "1.2.4", "1.3.1", evaluate=False)
    assert unevaluated["gate"] is None and json.loads(state.requests[-1]["body"])["evaluate"] is False
    assert "idempotency-key" not in state.requests[-1]["headers"]
    with pytest.raises(APIError) as e:
        client.create_release(PROJECT, "support-refund-agent", "1.3.0", "1.3.0")
    assert (e.value.status, e.value.code) == (400, "VALIDATION_FAILED")

    listed = client.releases(PROJECT, agent="support-refund-agent", limit=10)
    assert [r["gate"]["effective_outcome"] for r in listed] == ["BLOCK"]
    assert state.requests[-1]["path"] == (
        f"/api/v1/releases?project_id={PROJECT}&limit=10&agent=support-refund-agent"
    )
    detail = client.release(RELEASE)
    assert [r["revision"] for r in detail["revisions"]] == [1]

    # The gate is polled until it decides.
    gate = client.wait_for_gate(RELEASE, timeout_s=5, interval_s=0.01)
    assert gate["status"] == "DECIDED" and gate["exit_code"] == 3 and state.gate_polls == 2
    again = client.evaluate_release(RELEASE, idempotency_key="release-evaluate-2")
    assert again["revision"] == 2 and state.requests[-1]["headers"]["idempotency-key"] == "release-evaluate-2"
    assert client.release_gate(RELEASE, revision=2)["revision"] == 2
    assert state.requests[-1]["path"] == f"/api/v1/releases/{RELEASE}/gate?revision=2"
    state.gate_polls = -1000  # never decides
    with pytest.raises(APIError) as e:
        client.wait_for_gate(RELEASE, timeout_s=0.05, interval_s=0.01)
    assert e.value.code == "TIMEOUT" and RELEASE in e.value.message
    used = {"createRelease", "listReleases", "getRelease", "evaluateRelease", "getReleaseGate"}
    assert used <= state.checker.succeeded()


def test_regressions_triage_and_promotion(stub: tuple[Stub, str]) -> None:
    state, url = stub
    client = Client(url, KEY)
    inbox = client.regressions(
        PROJECT, status=["CANDIDATE", "CONFIRMED"], severity=["critical"], agent="support-refund-agent"
    )
    assert [r["id"] for r in inbox] == [REGRESSION]
    assert state.requests[-1]["path"] == (
        f"/api/v1/regressions/candidates?project_id={PROJECT}&limit=200"
        "&status=CANDIDATE%2CCONFIRMED&severity=critical&agent=support-refund-agent"
    )
    client.regressions(PROJECT, taxonomy="TIMEOUT", include_merged=True, limit=5)
    assert state.requests[-1]["path"] == (
        f"/api/v1/regressions/candidates?project_id={PROJECT}&limit=5&taxonomy=TIMEOUT&include_merged=true"
    )
    assert client.regression(REGRESSION)["occurrences"][0]["join_kind"] == "new"
    assert client.regression_draft(REGRESSION)["draft"]["complete"] is True

    triaged = client.triage_regression(REGRESSION, severity="high", assignee=None, reason="seen before")
    assert triaged["regression"]["severity"] == "high"
    sent = state.requests[-1]
    assert (sent["method"], json.loads(sent["body"])) == (
        "PATCH",
        {"severity": "high", "assignee": None, "reason": "seen before"},
    )
    assert client.confirm_regression(REGRESSION)["regression"]["status"] == "CONFIRMED"
    assert json.loads(state.requests[-1]["body"]) == {}
    out = client.dismiss_regression(REGRESSION, "a test tenant", idempotency_key="dismiss-regression-1")
    assert out["regression"]["status"] == "DISMISSED"
    assert state.requests[-1]["headers"]["idempotency-key"] == "dismiss-regression-1"
    assert json.loads(state.requests[-1]["body"]) == {"reason": "a test tenant"}
    assert client.reopen_regression(REGRESSION, "back again")["regression"]["status"] == "REOPENED"
    with pytest.raises(APIError) as e:
        client.dismiss_regression(REGRESSION, "")
    assert (e.value.status, e.value.code) == (400, "INVALID_REQUEST")

    into = fake.uuid(0xE002)
    merged = client.merge_regression(REGRESSION, into, reason="the same failure")
    assert merged["merged"]["merged_into"] == into and merged["regression"]["occurrence_count"] == 2
    assert json.loads(state.requests[-1]["body"]) == {"into": into, "reason": "the same failure"}

    promoted = client.promote_regression(REGRESSION, reason="customers refunded twice")
    assert promoted["regression"]["status"] == "PROMOTED"
    assert promoted["dataset"]["name"] == "production-regressions"
    assert json.loads(state.requests[-1]["body"]) == {"reason": "customers refunded twice"}
    client.promote_regression(REGRESSION, yaml="kind: Scenario\n", idempotency_key="promote-regression-1")
    assert json.loads(state.requests[-1]["body"]) == {"yaml": "kind: Scenario\n"}
    doc = fake.regression_draft(fake.regression())["draft"]["document"]
    client.promote_regression(REGRESSION, document=doc)
    assert json.loads(state.requests[-1]["body"]) == {"document": doc}
    with pytest.raises(ValueError, match="not both"):
        client.promote_regression(REGRESSION, document=doc, yaml="kind: Scenario\n")
    used = {
        "listRegressions",
        "getRegression",
        "draftRegressionScenario",
        "triageRegression",
        "confirmRegression",
        "dismissRegression",
        "reopenRegression",
        "mergeRegression",
        "promoteRegression",
    }
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

        do_GET = do_POST = do_PATCH = _serve

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
