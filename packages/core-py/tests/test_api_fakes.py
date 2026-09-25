"""The payload builders the client tests' fakes answer with are what the
AgentTwin APIs' contracts describe, and the exchange checker holds each
exchange to the contract of the service that owns the path (ADR-0021)."""

from __future__ import annotations

import json

import pytest

from agenttwin_core import api_fakes as fake
from agenttwin_core.openapi_contract import Contract, ContractViolation, contract_path


@pytest.fixture(scope="module")
def contract() -> Contract:
    return Contract.load(contract_path("simulation-service"))


def test_control_plane_and_trace_builders_are_valid_contract_payloads() -> None:
    control_plane = Contract.load(contract_path("control-plane"))
    control_plane.check_schema("Project", fake.project(description="Support automation agents"))
    control_plane.check_schema("Agent", fake.agent())
    control_plane.check_schema("AgentVersion", fake.agent_version(version="1.3.1"))
    control_plane.check_schema("AgentVersionDetail", fake.agent_version_detail(tools=[]))
    for created in (True, False):
        control_plane.check_schema("RegisteredVersion", fake.registered_version(created=created))
    control_plane.check_schema("Error", fake.error("UNAUTHENTICATED", "Authentication is required."))
    for created in (True, False):
        control_plane.check_schema("ImportedCatalog", fake.imported_catalog(created=created))
        control_plane.check_schema("CreatedChangeSet", fake.change_set(created=created))
    tool = fake.change_item("tool", "refund_payment", breaking=True)
    assert fake.change_set(items=[tool])["summary"]["breaking"] == 1
    graph_reason = {
        "from": {"kind": "TOOL", "key": "refund_payment"},
        "from_label": "refund_payment",
        "via": {"kind": "TOOL", "key": "refund_payment"},
        "via_label": "refund_payment",
        "direct": True,
        "hops": 1,
        "score": 1.0,
        "path": [],
    }
    linked = fake.impact_scenario(
        "refund-happy-path",
        reasons={"graph": [graph_reason], "similar": [], "always_run_tags": [], "known_regression": False},
    )
    impact = fake.change_impact([linked, fake.impact_scenario("cross-tenant-order", tags=["security"])])
    control_plane.check_schema("ChangeImpact", impact)
    assert impact["complete"] and impact["counts"]["scenarios"] == 2 and impact["counts"]["graph"] == 1
    problem = {"service": "graph-service", "code": "NOT_CONFIGURED", "message": "not configured"}
    incomplete = fake.change_impact(problems=[problem])
    control_plane.check_schema("ChangeImpact", incomplete)
    assert incomplete["complete"] is False
    traces = Contract.load(contract_path("trace-service"))
    contradicted = fake.outcome(
        status="FAILURE",
        claimed_status="SUCCESS",
        verified=True,
        verification_source="state_assertion",
        actual_state={"refund_count": 2},
    )
    assert contradicted["contradiction"] is True and fake.outcome()["contradiction"] is False
    traces.check_schema("Outcome", contradicted)
    with pytest.raises(ContractViolation, match="'maybe' is not one of"):
        traces.check_schema("Outcome", fake.outcome(source="maybe"))


def test_builders_are_valid_contract_payloads(contract: Contract) -> None:
    contract.check_schema("Twin", fake.twin())
    contract.check_schema("TwinSummary", fake.twin_summary(description="Support tools."))
    contract.check_schema("Scenario", fake.scenario(tags=["smoke"]))
    for status in ("QUEUED", "RUNNING", "EVALUATING", "COMPLETED", "FAILED", "CANCELLED"):
        contract.check_schema("PinnedRun", fake.run(status=status))
    contract.check_schema("QueuedCase", fake.queued_case(3, "refund-happy-path"))
    pair = fake.simulation_pair(fake.uuid(0xA001), ["refund-timeout-after-mutation", "refund-happy-path"])
    contract.check_schema("SimulationPair", pair)
    assert pair["baseline"]["pinning"]["scenarios"] == pair["candidate"]["pinning"]["scenarios"]
    assert fake.owner("/internal/v1/simulation-pairs") == "simulation-service"
    assert fake.owner("/internal/v1/agent-versions") == "control-plane"
    cases = [
        fake.case_summary(i, f"s{i}", status)
        for i, status in enumerate(("PENDING", "RUNNING", "PASSED", "FAILED", "ERRORED", "CANCELLED"))
    ]
    contract.check_schema("RunDetail", fake.run_detail(fake.run(status="RUNNING"), cases))
    contract.check_schema("Error", fake.error("SCENARIO_INVALID", "invalid", problems=["x"]))
    steps = [
        fake.tool_step(1, "lookup_order", {"order_id": "ORD-1"}),
        fake.retrieval_step(2, "refund policy", ["kb-1"]),
        fake.tool_step(3, "refund_payment", {"amount": 40}, risk="WRITE_IRREVERSIBLE", mutated=True),
    ]
    for status in ("PASSED", "FAILED", "ERRORED"):
        results = [
            fake.expectation_result("e1", "PASS"),
            fake.expectation_result("e2", "FAIL", type_="state", critical=True, label="STATE_MISMATCH"),
            fake.expectation_result("e3", "SKIPPED", type_="semantic"),
        ]
        detail = fake.case_detail(0, "refund-happy-path", status, steps=steps, results=results)
        contract.check_schema("CaseDetail", detail)
    no_answer = fake.agent_result(kind="timeout", http_status=None, error="The agent did not answer.")
    contract.check_schema("CaseDetail", fake.case_detail(1, "s1", "ERRORED", agent=no_answer))
    # Overrides are checked like everything else.
    with pytest.raises(ContractViolation, match="'SOMETIMES' is not one of"):
        contract.check_schema("PinnedRun", fake.run(status="SOMETIMES"))


def test_the_checker_holds_each_path_to_its_service_and_counts_only_what_passed() -> None:
    checker = fake.ExchangeChecker()
    run = f"/api/v1/simulations/{fake.uuid(1)}"
    outcome = f"/api/v1/traces/{'4bf92f3577b34da6a3ce929d0e0e4736'}/outcome"
    posted = {"content-type": "application/json"}, json.dumps({"status": "SUCCESS"}).encode()
    checker.check("GET", "/health/ready", {}, b"", 200, {"status": "ready"})  # no contract documents it
    checker.check("GET", run, {}, b"", 200, {"run": {}})
    checker.check("GET", "/api/v1/projects", {}, b"", 200, {"items": [{"id": fake.PROJECT}]})
    checker.check("POST", outcome, *posted, 200, {"status": "SUCCESS", "recorded": True})
    assert [v.split(" does not match")[0] for v in checker.violations] == [
        "simulation-service: getSimulation 200 response",
        "control-plane: listProjects 200 response",
        "trace-service: recordOutcome 200 response",
    ]
    assert checker.succeeded() == set()  # a failed check is not coverage
    checker.check("GET", f"{run}?verbose=1", {}, b"", 200, fake.run_detail(fake.run(), []))
    assert "undocumented query parameter 'verbose'" in checker.violations[3]
    checker.check("GET", run, {}, b"", 200, fake.run_detail(fake.run(), []))
    checker.check("GET", "/api/v1/projects", {}, b"", 200, {"items": [fake.project()]})
    checker.check("POST", outcome, *posted, 200, fake.outcome())
    assert checker.succeeded() == {"getSimulation", "listProjects", "recordOutcome"}
    assert len(checker.violations) == 4
    # A path under a service's prefix but outside its operations is reported.
    checker.check("GET", "/api/v1/tracesx", {}, b"", 200, {})
    assert "GET /api/v1/tracesx is not a documented operation" in checker.violations[4]


def test_each_path_belongs_to_one_service() -> None:
    assert fake.owner("/v1/traces") == "trace-service"
    assert fake.owner("/api/v1/traces/abc/flag") == "trace-service"
    assert fake.owner("/api/v1/trace-stats") == "trace-service"
    assert fake.owner("/api/v1/simulations/x/cases/y") == "simulation-service"
    assert fake.owner("/twin/v1/tools/refund_payment") == "simulation-service"
    assert fake.owner("/api/v1/projects/x/agent-manifests") == "control-plane"
    assert fake.owner("/internal/v1/agent-versions") == "control-plane"
    assert fake.owner("/api/v1/tracesx") == "control-plane"  # a prefix is a whole segment
    assert fake.owner("/health/ready") is None
