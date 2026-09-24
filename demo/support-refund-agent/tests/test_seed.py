"""``support-refund-agent seed`` against a recording fake of the AgentTwin API:
what it registers, in which order, and when it reports failure."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import yaml

from support_refund_agent.cli import default_assurance_dir, main

PROJECT = "0199a0a0-0000-7000-8000-000000000001"
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
            return 200, {"items": [{"id": PROJECT, "slug": "support"}]}
        if method == "POST" and path == f"/api/v1/projects/{PROJECT}/agent-manifests":
            version = yaml.safe_load(body)["metadata"]["version"]
            return 200, {"created": self._created(f"manifest:{version}")}
        if (method, path) == ("POST", "/api/v1/twins"):
            if self.twin_error:
                status, code = self.twin_error
                return status, {"error": {"code": code, "message": "not available"}}
            name = yaml.safe_load(body["yaml"])["metadata"]["name"]
            return 200, {"twin": {"name": name, "version": 1}, "created": self._created(f"twin:{name}")}
        if (method, path) == ("POST", "/api/v1/scenarios"):
            name = yaml.safe_load(body["yaml"])["metadata"]["name"]
            return 200, {"scenario": {"name": name}, "version": 1, "created": self._created(f"sc:{name}")}
        if (method, path) == ("POST", "/api/v1/simulations"):
            run_id = f"run-{len(self.runs) + 1}"
            self.runs[run_id] = body["agent_version"]
            cases = [{"scenario_name": s} for s in SCENARIOS]
            return 202, {"run": {"id": run_id, "status": "QUEUED"}, "cases": cases}
        if method == "GET" and path.startswith("/api/v1/simulations/"):
            run_id = path.rsplit("/", 1)[1]
            status, overrides = self.outcomes.get(self.runs[run_id], ("COMPLETED", {}))
            cases = [{"scenario_name": s, "status": overrides.get(s, "PASSED")} for s in SCENARIOS]
            counts = {
                k: sum(1 for c in cases if c["status"] == k.upper()) for k in ("passed", "failed", "errored")
            }
            run = {"id": run_id, "status": status, "case_count": len(cases), "critical_failures": 0, **counts}
            return 200, {"run": run, "cases": cases}
        return 404, {"error": {"code": "NOT_FOUND", "message": path}}

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
    fake = FakeAPI()
    thread = threading.Thread(target=fake.server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("AGENTTWIN_API_URL", fake.url)
    monkeypatch.setenv("AGENTTWIN_API_KEY", "seed-key")
    monkeypatch.delenv("AGENTTWIN_UI_URL", raising=False)
    yield fake
    fake.server.shutdown()
    fake.server.server_close()


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
        "run_id": "run-1",
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

    # Seeding again changes nothing but starts new runs.
    code, again = seed(capsys, "--simulate", "1.2.4")
    assert code == 0
    assert again["twin"]["created"] is False
    assert again["scenarios"] == {"saved": [], "unchanged": SCENARIOS}
    assert [m["created"] for m in again["manifests"]] == [False] * len(again["manifests"])
    assert again["simulations"][0]["run_id"] == "run-3"


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
