"""``support-refund-agent seed`` against a recording fake of the AgentTwin API:
what it registers, in which order, and when it reports failure. Every exchange
of the fake is held to the contract of the service that owns the path
(ADR-0021)."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import yaml

from agenttwin_core import api_fakes as fake
from support_refund_agent.cli import default_assurance_dir, main

PROJECT = "0199a0a0-0000-7000-8000-000000000001"
DATASET = fake.uuid(0xB001)
SCENARIOS = sorted(p.stem for p in (default_assurance_dir() / "scenarios").glob("*.yaml"))


class FakeAPI:
    """Just enough of the control plane (and the simulation service behind
    it) for the seed. Every request is recorded as ``METHOD path``."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.bodies: dict[str, list[Any]] = {}
        self.seen: set[str] = set()
        self.twin_error: tuple[int, str] | None = None
        # agent version -> (run status, {scenario: case status}); missing scenarios pass
        self.outcomes: dict[str, tuple[str, dict[str, str]]] = {}
        self.runs: dict[str, str] = {}
        # The dataset the seed keeps (name -> its latest cases) and the
        # evaluation runs (id -> what the fake answers once they are done).
        self.datasets: dict[str, list[str]] = {}
        self.evaluation: tuple[str, dict[str, Any]] = ("COMPLETED", {})
        self.eval_runs: dict[str, dict[str, Any]] = {}
        self.checker = fake.ExchangeChecker()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def _created(self, key: str) -> bool:
        first = key not in self.seen
        self.seen.add(key)
        return first

    def respond(self, method: str, path: str, body: Any) -> tuple[int, Any]:
        self.calls.append(f"{method} {path}")
        self.bodies.setdefault(path, []).append(body)
        if (method, path) == ("GET", "/health/ready"):
            return 200, {"status": "ready"}
        if (method, path) == ("GET", "/api/v1/projects"):
            return 200, {"items": [fake.project(id=PROJECT)]}
        if method == "POST" and path == f"/api/v1/projects/{PROJECT}/agent-manifests":
            version = yaml.safe_load(body)["metadata"]["version"]
            created = self._created(f"manifest:{version}")
            registered = fake.registered_version(created=created, version=version, project_id=PROJECT)
            return 201 if created else 200, registered
        if (method, path) == ("POST", "/api/v1/twins"):
            if self.twin_error:
                status, code = self.twin_error
                return status, fake.error(code, "not available")
            name = yaml.safe_load(body["yaml"])["metadata"]["name"]
            created = self._created(f"twin:{name}")
            return 201 if created else 200, {"twin": fake.twin(name=name), "created": created}
        if (method, path) == ("POST", "/api/v1/scenarios"):
            name = yaml.safe_load(body["yaml"])["metadata"]["name"]
            created = self._created(f"sc:{name}")
            saved = {"scenario": fake.scenario(name=name), "version": 1, "created": created, "warnings": []}
            return 201 if created else 200, saved
        if (method, path) == ("POST", "/api/v1/simulations"):
            run_id = fake.uuid(0xE000 + len(self.runs) + 1)
            self.runs[run_id] = body["agent_version"]
            run = fake.run(id=run_id, agent_version=body["agent_version"], case_count=len(SCENARIOS))
            return 202, {"run": run, "cases": [fake.queued_case(i, s) for i, s in enumerate(SCENARIOS)]}
        if method == "GET" and path.startswith("/api/v1/simulations/"):
            run_id = path.rsplit("/", 1)[1]
            status, overrides = self.outcomes.get(self.runs[run_id], ("COMPLETED", {}))
            cases = [fake.case_summary(i, s, overrides.get(s, "PASSED")) for i, s in enumerate(SCENARIOS)]
            counts = {
                k: sum(1 for c in cases if c["status"] == k.upper()) for k in ("passed", "failed", "errored")
            }
            run = fake.run(
                id=run_id,
                status=status,
                agent_version=self.runs[run_id],
                case_count=len(cases),
                finished_cases=len(cases),
                **counts,
            )
            return 200, fake.run_detail(run, cases)
        return self.respond_evaluation(method, path, body)

    def respond_evaluation(self, method: str, path: str, body: Any) -> tuple[int, Any]:
        suite = fake.dataset(id=DATASET, project_id=PROJECT)
        if (method, path) == ("GET", "/api/v1/datasets"):
            items = [
                suite | {"name": name, "latest_version": 1, "case_count": len(cases)}
                for name, cases in self.datasets.items()
            ]
            return 200, {"items": items, "next_cursor": None}
        if (method, path) == ("POST", "/api/v1/datasets"):
            names = [c["scenario"] for c in body["cases"]]
            self.datasets[body["name"]] = names
            return 201, fake.dataset_detail(suite | {"name": body["name"], "latest_version": 1}, names)
        if (method, path) == ("POST", f"/api/v1/datasets/{DATASET}/cases"):
            [(name, cases)] = self.datasets.items()
            added = [c["scenario"] for c in body["cases"] if c["scenario"] not in cases]
            self.datasets[name] = cases + added
            version = 2 if added else 1
            detail = fake.dataset_detail(
                suite | {"name": name, "latest_version": version}, self.datasets[name]
            )
            return (201 if added else 200), detail
        if (method, path) == ("POST", "/api/v1/eval-runs"):
            run_id = fake.uuid(0xB100 + len(self.eval_runs) + 1)
            run = fake.eval_run(
                id=run_id,
                project_id=PROJECT,
                agent_name=body["agent"],
                baseline_version=body["baseline_version"],
                candidate_version=body["candidate_version"],
                seed=body.get("seed"),
            )
            self.eval_runs[run_id] = run
            return 202, {"run": run}
        if method == "GET" and path.startswith("/api/v1/eval-runs/"):
            status, summary = self.evaluation
            run = self.eval_runs[path.rsplit("/", 1)[1]] | {"status": status, "counts": summary["counts"]}
            return 200, fake.eval_run_detail(run, summary if status == "COMPLETED" else None)
        return 404, fake.error("NOT_FOUND", path)

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        api = self

        class Handler(BaseHTTPRequestHandler):
            def _reply(self, method: str) -> None:
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                body: Any = raw.decode() if raw else None
                if body and self.headers.get("Content-Type") == "application/json":
                    body = json.loads(body)
                assert self.headers.get("X-AgentTwin-Api-Key") == "seed-key" or self.path == "/health/ready"
                status, out = api.respond(method, self.path.split("?")[0], body)
                api.checker.check(method, self.path, dict(self.headers.items()), raw, status, out)
                data = json.dumps(out).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                self._reply("GET")

            def do_POST(self) -> None:
                self._reply("POST")

            def log_message(self, *args: Any) -> None:
                pass

        return Handler


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeAPI]:
    api = FakeAPI()
    thread = threading.Thread(target=api.server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("AGENTTWIN_API_URL", api.url)
    monkeypatch.setenv("AGENTTWIN_API_KEY", "seed-key")
    monkeypatch.delenv("AGENTTWIN_UI_URL", raising=False)
    try:
        yield api
    finally:
        api.server.shutdown()
        api.server.server_close()
    assert not api.checker.violations, "\n".join(api.checker.violations)


def seed(capsys: pytest.CaptureFixture[str], *extra: str) -> tuple[int, dict[str, Any]]:
    code = main(["seed", "--count", "0", "--wait", "5", *extra])
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip() else {}


def test_seed_registers_the_suite_then_runs_it(api: FakeAPI, capsys: pytest.CaptureFixture[str]) -> None:
    regressions = {"refund-happy-path": "FAILED", "refund-tool-success-lie": "FAILED"}
    api.outcomes["1.3.0"] = ("COMPLETED", regressions)
    code, summary = seed(capsys, "--simulate", "1.2.4, 1.3.0,1.2.4")
    assert code == 0
    assert summary["twin"] == {"name": "demo-co-support", "version": 1, "created": True}
    assert summary["scenarios"] == {"saved": SCENARIOS, "unchanged": []}
    assert len(SCENARIOS) == 9
    # The twin first (scenarios name it), then the scenarios, then the runs.
    posts = [c for c in api.calls if c.startswith("POST") and "agent-manifests" not in c]
    assert posts == [
        "POST /api/v1/twins",
        *["POST /api/v1/scenarios"] * 9,
        "POST /api/v1/simulations",
        "POST /api/v1/simulations",
    ]
    # Repeated versions run once, against every scenario of the agent.
    assert api.bodies["/api/v1/simulations"] == [
        {"project_id": PROJECT, "agent": "support-refund-agent", "agent_version": v, "seed": 42}
        for v in ("1.2.4", "1.3.0")
    ]
    base, candidate = summary["simulations"]
    assert base == {
        "version": "1.2.4",
        "run_id": fake.uuid(0xE001),
        "status": "COMPLETED",
        "cases": 9,
        "passed": 9,
        "failed": 0,
        "errored": 0,
        "critical_failures": 0,
        "failed_scenarios": [],
        "errored_scenarios": [],
    }
    # A candidate with regressions is what the demo is for, not a seed failure.
    assert candidate["failed_scenarios"] == ["refund-happy-path", "refund-tool-success-lie"]
    # What the seed sent and read was checked against the services' contracts.
    used = {"listProjects", "registerManifest", "registerTwin", "saveScenario", "startSimulation"}
    assert used | {"getSimulation"} <= api.checker.succeeded()

    # Seeding again changes nothing but starts new runs.
    code, again = seed(capsys, "--simulate", "1.2.4")
    assert code == 0
    assert again["twin"]["created"] is False
    assert again["scenarios"] == {"saved": [], "unchanged": SCENARIOS}
    assert [m["created"] for m in again["manifests"]] == [False] * len(again["manifests"])
    assert again["simulations"][0]["run_id"] == fake.uuid(0xE003)


def test_seed_keeps_the_regression_suite_and_evaluates_the_candidate(
    api: FakeAPI, capsys: pytest.CaptureFixture[str]
) -> None:
    api.evaluation = (
        "COMPLETED",
        fake.eval_run_summary(
            new_critical_failures={"refund-tool-success-lie": ["no-false-confirmation"]},
            regressed=["refund-happy-path"],
            unchanged=7,
        ),
    )
    code, summary = seed(capsys, "--evaluate", "1.2.4:1.3.0")
    assert code == 0
    # The suite is every scenario, created once; the run evaluates it.
    [created] = [b for b in api.bodies["/api/v1/datasets"] if b is not None]  # the POST
    assert created | {"cases": None} == {
        "project_id": PROJECT,
        "name": "refund-regression-suite",
        "description": "Every scenario of the support refund agent (loaded by the demo seed).",
        "tags": ["demo"],
        "cases": None,
    }
    assert api.datasets == {"refund-regression-suite": SCENARIOS}
    assert api.bodies["/api/v1/eval-runs"] == [
        {
            "project_id": PROJECT,
            "agent": "support-refund-agent",
            "baseline_version": "1.2.4",
            "candidate_version": "1.3.0",
            "dataset_id": DATASET,
            "seed": 42,
        }
    ]
    assert summary["dataset"] == {"id": DATASET, "name": "refund-regression-suite", "version": 1}
    assert summary["evaluation"] == {
        "run_id": fake.uuid(0xB101),
        "baseline": "1.2.4",
        "candidate": "1.3.0",
        "status": "COMPLETED",
        "error": None,
        "counts": {"NEW_CRITICAL_FAILURE": 1, "REGRESSED": 1, "IMPROVED": 0, "UNCHANGED": 7, "INCOMPLETE": 0},
        "new_critical_failures": ["refund-tool-success-lie"],
        "regressed": ["refund-happy-path"],
        "incomplete": [],
    }
    used = {"listDatasets", "createDataset", "startEvalRun", "getEvalRun"}
    assert used <= api.checker.succeeded()

    # Seeding again keeps the same dataset (no new version) and evaluates again.
    code, again = seed(capsys, "--evaluate", "1.2.4:1.3.0")
    assert code == 0 and api.calls.count("POST /api/v1/datasets") == 1
    assert again["dataset"]["version"] == 1 and again["evaluation"]["run_id"] == fake.uuid(0xB102)
    assert "addDatasetCases" in api.checker.succeeded()


@pytest.mark.parametrize(
    ("evaluation", "reason"),
    [
        (
            ("COMPLETED", fake.eval_run_summary(incomplete=["refund-happy-path"], unchanged=8)),
            "a side did not run",
        ),
        (("FAILED", fake.eval_run_summary()), "the evaluation failed"),
    ],
)
def test_seed_fails_on_an_unhealthy_evaluation(
    api: FakeAPI, capsys: pytest.CaptureFixture[str], evaluation: tuple[str, dict[str, Any]], reason: str
) -> None:
    api.evaluation = evaluation
    code, summary = seed(capsys, "--evaluate", "1.2.4:1.3.0")
    assert code == 1, reason
    assert summary["evaluation"]["status"] == evaluation[0]


@pytest.mark.parametrize("spec", ["1.2.4", "1.2.4:", "1.2.4:9.9.9"])
def test_seed_rejects_a_bad_evaluation_before_calling_the_api(
    api: FakeAPI, capsys: pytest.CaptureFixture[str], spec: str
) -> None:
    assert seed(capsys, "--evaluate", spec) == (2, {})
    assert api.calls == []


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [
        (("COMPLETED", {"refund-over-limit": "ERRORED"}), "a case could not be evaluated"),
        (("FAILED", {}), "the run itself failed"),
    ],
)
def test_seed_fails_on_an_unhealthy_run(
    api: FakeAPI, capsys: pytest.CaptureFixture[str], outcome: tuple[str, dict[str, str]], reason: str
) -> None:
    api.outcomes["1.2.4"] = outcome
    code, summary = seed(capsys, "--simulate", "1.2.4")
    assert code == 1, reason
    [sim] = summary["simulations"]
    assert sim["status"] == outcome[0]
    assert sim["errored_scenarios"] == sorted(s for s, v in outcome[1].items() if v == "ERRORED")


def test_seed_fails_without_the_simulation_service(api: FakeAPI, capsys: pytest.CaptureFixture[str]) -> None:
    api.twin_error = (503, "SERVICE_NOT_CONFIGURED")
    code, summary = seed(capsys, "--simulate", "1.2.4")
    assert (code, summary) == (1, {})
    assert "POST /api/v1/scenarios" not in api.calls
    assert "POST /api/v1/simulations" not in api.calls


def test_seed_rejects_unknown_versions_before_calling_the_api(
    api: FakeAPI, capsys: pytest.CaptureFixture[str]
) -> None:
    assert seed(capsys, "--simulate", "1.2.4,9.9.9") == (2, {})
    assert api.calls == []


def test_seed_fails_on_missing_assurance_assets(
    api: FakeAPI, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    code, _ = seed(capsys, "--assurance-dir", str(tmp_path))
    assert code == 1
    assert "POST /api/v1/twins" not in api.calls
