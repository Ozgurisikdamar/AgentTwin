"""``support-refund-agent seed`` against a recording fake of the AgentTwin API:
what it registers, in which order, and when it reports failure. Every exchange
of the fake is held to the contract of the service that owns the path
(ADR-0021)."""

from __future__ import annotations

import json
import re
import shutil
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
        # Change sets (id -> the pair) and how their impact answers: the
        # first ``impact_lag`` answers still miss the candidate in the graph.
        self.change_sets: dict[str, tuple[str, str]] = {}
        self.impact_lag = 0
        self.impact_calls = 0
        self.impact_problems: list[dict[str, str]] = []
        # Releases (id -> pair, change set, evaluations) and how their gates
        # answer: undecided for the first ``gate_lag`` reads of a revision.
        self.release_store: dict[str, dict[str, Any]] = {}
        self.gate_outcomes = {"1.3.0": "BLOCK", "1.3.1": "PASS"}
        self.gate_lag = 1
        self.gate_incomplete = False
        self.gate_unverified = False
        # The regression inbox: groups (newest first) and their failures'
        # traces. ``regression_lag`` inbox reads pass before the miner adds
        # ``pending`` (group id, trace id) to its group.
        self.regressions: list[dict[str, Any]] = []
        self.occurrences: dict[str, list[str]] = {}
        self.pending: tuple[str, str] | None = None
        self.regression_lag = 0
        self.inbox_reads = 0
        self.keys: list[tuple[str, str, str | None]] = []
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
        return self.respond_changes(method, path, body)

    def respond_changes(self, method: str, path: str, body: Any) -> tuple[int, Any]:
        if method == "POST" and path.startswith(f"/api/v1/projects/{PROJECT}/imports/"):
            source = path.rsplit("/", 1)[1].upper()
            name = body["name"] if source == "OPENAPI" else body["server"]["name"]
            created = self._created(f"catalog:{source}:{name}")
            tools = [f"{name}-tool-{i}" for i in range(len(body.get("tools") or [1, 2]))]
            spec = "3.1.0" if source == "OPENAPI" else body.get("protocol_version", "")
            catalog = fake.imported_catalog(
                created=created,
                entries=[t.replace("-", "_") for t in tools],
                source=source,
                name=name,
                service=body.get("service"),
                spec_version=spec,
            )
            return (201 if created else 200), catalog
        if method == "POST" and path == f"/api/v1/projects/{PROJECT}/change-sets":
            pair = (body["base_version"], body["candidate_version"])
            known = [cs for cs, p in self.change_sets.items() if p == pair]
            cs_id = known[0] if known else fake.uuid(0xC100 + len(self.change_sets) + 1)
            self.change_sets[cs_id] = pair
            item = (
                fake.change_item(
                    "tool",
                    "refund_payment",
                    breaking=True,
                    summary="description changed; 2 schema changes (2 breaking)",
                )
                if pair == ("1.3.1", "1.3.2")
                else fake.change_item(
                    "prompt",
                    fake.sha256("c"),
                    summary="prompt modified; changed lines mention refund_payment",
                )
            )
            detail = fake.change_set(
                created=not known, id=cs_id, base_version=pair[0], candidate_version=pair[1], items=[item]
            )
            return (200 if known else 201), detail
        if method == "GET" and path.startswith("/api/v1/change-sets/") and path.endswith("/impact"):
            cs_id = path.split("/")[4]
            base, candidate = self.change_sets[cs_id]
            self.impact_calls += 1
            lagging = self.impact_calls <= self.impact_lag
            seed = {
                "component": {"kind": "AGENT_VERSION", "key": f"support-refund-agent@{candidate}"},
                "change": "modified",
            }
            graph = {
                "seeds": [seed],
                "unresolved": [seed] if lagging else [],
                "affected": [],
                "affected_count": 0,
                "policies": [],
                "evaluators": [],
                "max_depth": 4,
                "truncated": False,
            }
            scenarios = (
                []
                if lagging
                else [
                    fake.impact_scenario(
                        "refund-timeout-after-mutation", why=["tests refund_payment, which changed"]
                    ),
                    fake.impact_scenario("cross-tenant-order", why=["always runs (tagged security)"]),
                ]
            )
            impact = fake.change_impact(
                scenarios,
                change_set_id=cs_id,
                base_version=base,
                candidate_version=candidate,
                graph=graph,
                problems=self.impact_problems,
                new_privileges=[
                    {"tool": "refund_payment", "change": "escalated", "risk": "ADMIN", "from": "READ"}
                ],
            )
            return 200, impact
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
        return self.respond_releases(method, path, body)

    def _gate(self, release_id: str) -> dict[str, Any]:
        rel = self.release_store[release_id]
        polls = rel["evaluations"][-1]
        if polls <= self.gate_lag:
            return fake.release_gate(release_id=release_id, revision=len(rel["evaluations"]))
        outcome = self.gate_outcomes[rel["pair"][1]]
        gate = fake.release_gate(outcome, release_id=release_id, revision=len(rel["evaluations"]))
        if self.gate_incomplete:
            gate["decision"] = fake.gate_decision("BLOCK", incomplete=True)
            gate["summary"] = fake.gate_decision_summary(gate["decision"])
            gate |= {"effective_outcome": "BLOCK", "exit_code": 3, "ci_fails": True}
        if self.gate_unverified:
            gate["evidence_verified"] = False
        return gate

    def _release(self, release_id: str) -> dict[str, Any]:
        rel = self.release_store[release_id]
        base, candidate = rel["pair"]
        return fake.release(
            self._gate(release_id) if rel["evaluations"] else None,
            id=release_id,
            project_id=PROJECT,
            change_set_id=rel["change_set_id"],
            baseline_version=base,
            candidate_version=candidate,
            title=f"{base} -> {candidate} (demo)",
        )

    def respond_releases(self, method: str, path: str, body: Any) -> tuple[int, Any]:
        if (method, path) == ("POST", "/api/v1/releases"):
            n = len(self.release_store) + 1
            release_id, cs_id = fake.uuid(0xD100 + n), fake.uuid(0xC180 + n)
            pair = (body["baseline_version"], body["candidate_version"])
            self.change_sets[cs_id] = pair
            self.release_store[release_id] = {"pair": pair, "change_set_id": cs_id, "evaluations": []}
            return 201, {"release": self._release(release_id), "gate": None}
        if (method, path) == ("GET", "/api/v1/releases"):
            items = [self._release(r) for r in reversed(self.release_store)]
            return 200, {"items": items, "next_cursor": None}
        if m := re.fullmatch(r"/api/v1/releases/([^/]+)/(evaluate|gate)", path):
            release_id, what = m.groups()
            evaluations = self.release_store[release_id]["evaluations"]
            if (method, what) == ("POST", "evaluate"):
                evaluations.append(0)
                return 202, self._gate(release_id)
            evaluations[-1] += 1
            return 200, self._gate(release_id)
        return self.respond_regressions(method, path, body)

    def respond_regressions(self, method: str, path: str, body: Any) -> tuple[int, Any]:
        if (method, path) == ("GET", "/api/v1/regressions/candidates"):
            self.inbox_reads += 1
            if self.pending is not None and self.inbox_reads > self.regression_lag:
                group_id, trace_id = self.pending
                self.occurrences[group_id].insert(0, trace_id)
                self.pending = None
            return 200, {"items": self.regressions, "next_cursor": None}
        if method == "GET" and (m := re.fullmatch(r"/api/v1/regressions/([^/]+)", path)):
            [group] = [g for g in self.regressions if g["id"] == m.group(1)]
            detail = fake.regression_detail(group)
            [occurrence] = detail["occurrences"]
            detail["occurrences"] = [occurrence | {"trace_id": t} for t in self.occurrences[group["id"]]]
            return 200, detail
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
                api.keys.append((method, self.path.split("?")[0], self.headers.get("Idempotency-Key")))
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
    # The manifests first (their tools keep the manifests' definitions), the
    # tool catalogs, the twin (scenarios name it), the scenarios, the runs.
    assert api.calls.index(f"POST /api/v1/projects/{PROJECT}/imports/openapi") > max(
        i for i, c in enumerate(api.calls) if "agent-manifests" in c
    )
    posts = [c for c in api.calls if c.startswith("POST") and "agent-manifests" not in c]
    assert posts == [
        f"POST /api/v1/projects/{PROJECT}/imports/openapi",
        f"POST /api/v1/projects/{PROJECT}/imports/openapi",
        f"POST /api/v1/projects/{PROJECT}/imports/mcp",
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
    # The catalogs of imports.yaml, sent as the files hold them.
    assert [(c["source"], c["name"], c["created"]) for c in summary["catalogs"]] == [
        ("OPENAPI", "payments-api", True),
        ("OPENAPI", "orders-api", True),
        ("MCP", "support-desk", True),
    ]
    assurance = default_assurance_dir()
    payments, orders = api.bodies[f"/api/v1/projects/{PROJECT}/imports/openapi"]
    assert payments == {
        "name": "payments-api",
        "service": "payments-api",
        "document": (assurance / "apis" / "payments-api.openapi.yaml").read_text(),
    }
    assert orders["service"] == "orders-api"
    [desk] = api.bodies[f"/api/v1/projects/{PROJECT}/imports/mcp"]
    listed = json.loads((assurance / "mcp" / "support-desk.tools.json").read_text())
    assert desk == {
        "server": {"name": "support-desk", "url": "https://desk.demo-co.example/mcp", "version": "3.2.0"},
        "tools": listed["tools"],
        "protocol_version": "2026-07-28",
        "risk_overrides": {
            "escalateToHuman": "WRITE_REVERSIBLE",
            "sendEmail": "WRITE_REVERSIBLE",
            "searchKnowledgeBase": "READ",
        },
    }
    # Without --changes, no version is compared.
    assert summary["change_sets"] == []
    # What the seed sent and read was checked against the services' contracts.
    used = {"listProjects", "registerManifest", "registerTwin", "saveScenario", "startSimulation"}
    assert used | {"getSimulation", "importOpenApi", "importMcp"} <= api.checker.succeeded()

    # Seeding again changes nothing but starts new runs.
    code, again = seed(capsys, "--simulate", "1.2.4")
    assert code == 0
    assert again["twin"]["created"] is False
    assert [c["created"] for c in again["catalogs"]] == [False] * 3
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


def test_seed_compares_versions_and_says_why_each_scenario_is_required(
    api: FakeAPI, capsys: pytest.CaptureFixture[str]
) -> None:
    # The graph takes in the new registrations asynchronously: the first
    # answer still misses the candidate, and the seed asks again.
    api.impact_lag = 1
    code, summary = seed(
        capsys, "--changes", "1.2.4:1.3.0, 1.3.1:1.3.2, 1.2.4 : 1.3.0", "--impact-timeout", "30"
    )
    assert code == 0
    # One change set per pair (a repeat is dropped), after the scenarios.
    posts = [c for c in api.calls if c.startswith("POST") and "agent-manifests" not in c]
    assert posts[-2:] == [f"POST /api/v1/projects/{PROJECT}/change-sets"] * 2
    assert api.bodies[f"/api/v1/projects/{PROJECT}/change-sets"] == [
        {
            "agent": "support-refund-agent",
            "base_version": base,
            "candidate_version": candidate,
            "title": f"{base} -> {candidate} (demo)",
        }
        for base, candidate in (("1.2.4", "1.3.0"), ("1.3.1", "1.3.2"))
    ]
    prompt, tool = summary["change_sets"]
    assert (prompt["base"], prompt["candidate"], tool["base"], tool["candidate"]) == (
        "1.2.4",
        "1.3.0",
        "1.3.1",
        "1.3.2",
    )
    # What changed, named (a prompt by the tools its changed lines mention).
    assert prompt["changes"] == ["prompt modified; changed lines mention refund_payment"]
    assert tool["changes"] == ["tool refund_payment: description changed; 2 schema changes (2 breaking)"]
    assert tool == tool | {
        "change_set_id": fake.uuid(0xC102),
        "created": True,
        "complete": True,
        "problems": [],
        "unresolved": [],
        "scenarios": {
            "refund-timeout-after-mutation": ["tests refund_payment, which changed"],
            "cross-tenant-order": ["always runs (tagged security)"],
        },
        "new_privileges": ["refund_payment (escalated)"],
    }
    assert api.impact_calls == 3  # the first answer lagged
    assert {"createChangeSet", "getChangeSetImpact"} <= api.checker.succeeded()

    # Seeding again finds the stored change sets.
    code, again = seed(capsys, "--changes", "1.3.1:1.3.2")
    assert code == 0 and api.calls.count(f"POST /api/v1/projects/{PROJECT}/change-sets") == 3
    [same] = again["change_sets"]
    assert (same["change_set_id"], same["created"]) == (fake.uuid(0xC102), False)


def test_seed_fails_on_an_incomplete_impact(api: FakeAPI, capsys: pytest.CaptureFixture[str]) -> None:
    api.impact_problems = [
        {"service": "graph-service", "code": "NOT_CONFIGURED", "message": "not configured"}
    ]
    code, summary = seed(capsys, "--changes", "1.2.4:1.3.0", "--impact-timeout", "0")
    assert code == 1
    [impact] = summary["change_sets"]
    assert (impact["complete"], impact["problems"]) == (False, ["graph-service: NOT_CONFIGURED"])
    assert api.impact_calls == 1  # no time left to ask again


def test_seed_fails_when_the_graph_never_learns_the_change(
    api: FakeAPI, capsys: pytest.CaptureFixture[str]
) -> None:
    api.impact_lag = 1_000
    code, summary = seed(capsys, "--changes", "1.2.4:1.3.0", "--impact-timeout", "0")
    assert code == 1
    [impact] = summary["change_sets"]
    assert impact["unresolved"] == ["AGENT_VERSION:support-refund-agent@1.3.0"] and impact["scenarios"] == {}


@pytest.mark.parametrize("spec", ["1.2.4", "1.3.0:1.3.0", "1.2.4:9.9.9", "1.2.4:1.3.0,:1.3.2"])
def test_seed_rejects_bad_changes_before_calling_the_api(
    api: FakeAPI, capsys: pytest.CaptureFixture[str], spec: str
) -> None:
    assert seed(capsys, "--changes", spec) == (2, {})
    assert api.calls == []


@pytest.mark.parametrize(
    ("edit", "reason"),
    [
        (lambda d: (d / "imports.yaml").write_text("openapi: [\n"), "not YAML"),
        (lambda d: (d / "imports.yaml").write_text("openapi:\n  - service: x\n"), "a catalog without a name"),
        (lambda d: (d / "mcp" / "support-desk.tools.json").write_text("{"), "not JSON"),
        (lambda d: (d / "apis" / "orders-api.openapi.yaml").unlink(), "a missing document"),
    ],
)
def test_seed_imports_nothing_from_a_malformed_imports_file(
    api: FakeAPI, capsys: pytest.CaptureFixture[str], tmp_path: Path, edit: Any, reason: str
) -> None:
    directory = tmp_path / "assurance"
    shutil.copytree(default_assurance_dir(), directory)
    edit(directory)
    code, _ = seed(capsys, "--assurance-dir", str(directory))
    assert code == 1, reason
    assert not [c for c in api.calls if "/imports/" in c], reason
    assert "POST /api/v1/twins" not in api.calls


def test_seed_gates_the_demo_releases(api: FakeAPI, capsys: pytest.CaptureFixture[str]) -> None:
    # The graph takes in the registrations asynchronously: the release is
    # evaluated only once its change is known.
    api.impact_lag = 1
    code, summary = seed(capsys, "--releases", "1.2.4:1.3.0, 1.2.4:1.3.1", "--release-timeout", "30")
    assert code == 0
    got = [
        (r["baseline"], r["candidate"], r["effective_outcome"], r["exit_code"], r["created"], r["revision"])
        for r in summary["releases"]
    ]
    assert got == [("1.2.4", "1.3.0", "BLOCK", 3, True, 1), ("1.2.4", "1.3.1", "PASS", 0, True, 1)]
    blocked = summary["releases"][0]
    assert blocked["rules"] == ["critical_failure"] and blocked["evidence_verified"] is True
    # Created without an evaluation, retried safely, evaluated once.
    created = [b for b in api.bodies["/api/v1/releases"] if b]
    assert created == [
        {
            "project_id": PROJECT,
            "agent": "support-refund-agent",
            "baseline_version": "1.2.4",
            "candidate_version": candidate,
            "title": f"1.2.4 -> {candidate} (demo)",
            "evaluate": False,
        }
        for candidate in ("1.3.0", "1.3.1")
    ]
    first = str(blocked["release_id"])
    assert ("POST", "/api/v1/releases", "seed-release-1.2.4-1.3.0") in api.keys
    assert ("POST", f"/api/v1/releases/{first}/evaluate", f"seed-evaluate-{first}") in api.keys
    at = api.calls.index
    cs = api.release_store[first]["change_set_id"]
    assert (
        at("POST /api/v1/releases")
        < at(f"GET /api/v1/change-sets/{cs}/impact")
        < at(f"POST /api/v1/releases/{first}/evaluate")
        < at(f"GET /api/v1/releases/{first}/gate")
    )
    assert api.calls.count(f"GET /api/v1/change-sets/{cs}/impact") == 2  # the first answer lagged
    assert {"createRelease", "listReleases", "evaluateRelease", "getReleaseGate"} <= api.checker.succeeded()

    # Seeding again keeps the releases and their decisions.
    before = len(api.calls)
    code, again = seed(capsys, "--releases", "1.2.4:1.3.0")
    assert code == 0
    later = api.calls[before:]
    assert "POST /api/v1/releases" not in later and not any(c.endswith("/evaluate") for c in later)
    [same] = again["releases"]
    assert (same["release_id"], same["created"], same["effective_outcome"]) == (first, False, "BLOCK")


def test_seed_fails_on_a_gate_without_evidence(api: FakeAPI, capsys: pytest.CaptureFixture[str]) -> None:
    api.gate_incomplete = True
    code, summary = seed(capsys, "--releases", "1.2.4:1.3.1", "--release-timeout", "30")
    assert code == 1
    [rel] = summary["releases"]
    assert (rel["status"], rel["incomplete"], rel["effective_outcome"]) == ("DECIDED", True, "BLOCK")


def test_seed_fails_on_a_gate_whose_evidence_does_not_verify(
    api: FakeAPI, capsys: pytest.CaptureFixture[str]
) -> None:
    api.gate_unverified = True
    code, summary = seed(capsys, "--releases", "1.2.4:1.3.1", "--release-timeout", "30")
    assert code == 1
    [rel] = summary["releases"]
    assert (rel["status"], rel["incomplete"], rel["evidence_verified"]) == ("DECIDED", False, False)


def test_seed_fails_when_a_gate_does_not_decide(api: FakeAPI, capsys: pytest.CaptureFixture[str]) -> None:
    api.gate_lag = 1_000_000
    code, summary = seed(capsys, "--releases", "1.2.4:1.3.0", "--release-timeout", "0")
    assert code == 1
    [rel] = summary["releases"]
    assert rel["status"] is None and "did not decide" in rel["error"]


@pytest.mark.parametrize("spec", ["1.2.4", "1.3.0:1.3.0", "1.2.4:9.9.9"])
def test_seed_rejects_bad_releases_before_calling_the_api(
    api: FakeAPI, capsys: pytest.CaptureFixture[str], spec: str
) -> None:
    assert seed(capsys, "--releases", spec) == (2, {})
    assert api.calls == []


INCIDENT_TRACE = "1" * 32


def incident_record(version: str = "1.3.0", *, refunds: int = 2, reported: bool = True) -> dict[str, Any]:
    """What ``TrafficGenerator.incident`` returns: on 1.3.0 the retried
    payment paid twice and the verified outcome contradicts the claim."""
    status = "SUCCESS" if refunds == 1 else "FAILURE"
    return {
        "kind": "incident",
        "version": version,
        "order_id": "ORD-9001",
        "trace_id": INCIDENT_TRACE,
        "business_outcome": "REFUND_COMPLETED",
        "claimed_outcome": "SUCCESS",
        "verified_outcome": {"status": status, "refund_count": refunds, "reported": reported},
    }


@pytest.fixture
def incident(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Stubs the incident conversation (its behaviour against the tools is
    tested in test_server_traffic); the seed's own wiring runs for real."""
    from support_refund_agent import cli

    answers: list[Any] = []
    asked: list[str] = []

    def run(self: Any, version: str) -> dict[str, Any]:
        asked.append(version)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        assert isinstance(answer, dict)
        return answer

    monkeypatch.setattr(cli.TrafficGenerator, "incident", run)
    answers.append(asked)  # the first element: the versions asked, for the test
    return answers


def with_inbox(api: FakeAPI, *, lag: int = 1) -> str:
    """An inbox where the miner joins the incident to an existing
    duplicate-refund group after ``lag`` reads."""
    older = fake.regression(
        id=fake.uuid(0xE010),
        title="order_status timed out",
        severity="low",
        taxonomy="TIMEOUT",
        suggested_taxonomy="TIMEOUT",
        status="DISMISSED",
        representative_trace_id="2" * 32,
    )
    dup = fake.regression(id=fake.uuid(0xE011), representative_trace_id="3" * 32, occurrence_count=4)
    api.regressions = [dup, older]
    api.occurrences = {dup["id"]: ["3" * 32], older["id"]: ["2" * 32]}
    api.pending = (dup["id"], INCIDENT_TRACE)
    api.regression_lag = lag
    return str(dup["id"])


def test_seed_sends_a_canary_incident_and_finds_its_regression(
    api: FakeAPI, capsys: pytest.CaptureFixture[str], incident: list[Any]
) -> None:
    asked = incident.pop(0)
    incident.append(incident_record())
    group = with_inbox(api)
    code, summary = seed(capsys, "--incident", "1.3.0", "--incident-timeout", "30")
    assert code == 0
    assert asked == ["1.3.0"]
    got = summary["incident"]
    assert (got["version"], got["trace_id"], got["refund_count"], got["verified_outcome"]) == (
        "1.3.0",
        INCIDENT_TRACE,
        2,
        "FAILURE",
    )
    assert got["outcome_reported"] is True and got["regression_expected"] is True
    assert (got["regression"]["id"], got["regression"]["severity"], got["regression"]["status"]) == (
        group,
        "critical",
        "CANDIDATE",
    )
    # Waited for the miner: the first read did not have the incident yet.
    assert api.inbox_reads >= 2
    assert f"GET /api/v1/regressions/{group}" in api.calls
    inbox = summary["regressions"]
    assert inbox["total"] == 2
    assert inbox["by_status"] == {"CANDIDATE": 1, "DISMISSED": 1}
    assert inbox["by_severity"] == {"critical": 1, "low": 1}
    assert [r["id"] for r in inbox["open"]] == [group]  # a dismissed group is not open
    assert {"listRegressions", "getRegression"} <= api.checker.succeeded()


def test_seed_finds_the_incident_that_represents_its_group(
    api: FakeAPI, capsys: pytest.CaptureFixture[str], incident: list[Any]
) -> None:
    incident.pop(0)
    incident.append(incident_record())
    new = fake.regression(id=fake.uuid(0xE012), representative_trace_id=INCIDENT_TRACE)
    api.regressions = [new]
    api.occurrences = {new["id"]: [INCIDENT_TRACE]}
    code, summary = seed(capsys, "--incident", "1.3.0", "--incident-timeout", "30")
    assert code == 0
    assert summary["incident"]["regression"]["id"] == new["id"]
    # Its representative trace names it: no detail read needed.
    assert not any(c.startswith("GET /api/v1/regressions/0") for c in api.calls)


def test_seed_fails_when_the_incident_is_never_grouped(
    api: FakeAPI, capsys: pytest.CaptureFixture[str], incident: list[Any]
) -> None:
    incident.pop(0)
    incident.append(incident_record())
    with_inbox(api, lag=1_000_000)
    code, summary = seed(capsys, "--incident", "1.3.0", "--incident-timeout", "0")
    assert code == 1
    assert summary["incident"]["regression"] is None
    assert summary["regressions"]["total"] == 2


def test_seed_does_not_wait_when_the_incident_did_not_fail(
    api: FakeAPI, capsys: pytest.CaptureFixture[str], incident: list[Any]
) -> None:
    asked = incident.pop(0)
    incident.append(incident_record("1.3.1", refunds=1))
    code, summary = seed(capsys, "--incident", "1.3.1", "--incident-timeout", "30")
    assert code == 0
    assert asked == ["1.3.1"]
    got = summary["incident"]
    assert (got["verified_outcome"], got["refund_count"], got["regression_expected"]) == ("SUCCESS", 1, False)
    assert got["regression"] is None
    # Only the inbox report reads the inbox.
    assert api.calls.count("GET /api/v1/regressions/candidates") == 1


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (OSError("connection refused"), "the tools are unreachable"),
        (incident_record(reported=False), "the verified outcome was not reported"),
        ({**incident_record(), "trace_id": None}, "the agent returned no trace"),
    ],
)
def test_seed_fails_on_a_broken_incident(
    api: FakeAPI, capsys: pytest.CaptureFixture[str], incident: list[Any], answer: Any, reason: str
) -> None:
    incident.pop(0)
    incident.append(answer)
    with_inbox(api, lag=0)
    code, summary = seed(capsys, "--incident", "1.3.0", "--incident-timeout", "0")
    assert code == 1, reason
    assert summary["incident"]["version"] == "1.3.0", reason


def test_seed_reports_the_inbox_without_an_incident(api: FakeAPI, capsys: pytest.CaptureFixture[str]) -> None:
    group = with_inbox(api)
    code, summary = seed(capsys)
    assert code == 0
    assert summary["incident"] is None
    assert summary["regressions"]["total"] == 2 and [r["id"] for r in summary["regressions"]["open"]] == [
        group
    ]


def test_seed_rejects_an_unknown_incident_version(api: FakeAPI, capsys: pytest.CaptureFixture[str]) -> None:
    assert seed(capsys, "--incident", "9.9.9") == (2, {})
    assert api.calls == []
