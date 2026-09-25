"""Pure case logic: seeds, the agent request, the evaluation context built
from the twin's records and the verified outcome."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from agenttwin_core.yamlsafe import load_yaml
from agenttwin_simulation.cases import (
    DEFAULT_NOW,
    agent_request,
    case_seed,
    case_tenant,
    evaluation_context,
    foreign_values,
    initial_state,
    outcome_body,
    scenario_runtime,
    synthetic_result,
    valid_trace_id,
)
from agenttwin_simulation.twin.definition import load_twin

ASSURANCE = Path(__file__).resolve().parents[3] / "demo" / "support-refund-agent" / "assurance"
TWIN = load_twin(load_yaml((ASSURANCE / "twin.yaml").read_text()))
TIMEOUT = load_yaml((ASSURANCE / "scenarios" / "refund-timeout-after-mutation.yaml").read_text())


def test_case_seed_is_stable_and_pinnable() -> None:
    a = case_seed(42, "refund-happy-path")
    assert a == case_seed(42, "refund-happy-path")
    assert a != case_seed(43, "refund-happy-path") and a != case_seed(42, "other")
    assert 0 <= a < 2**32
    assert case_seed(42, "x", pinned=7) == 7


def test_scenario_runtime_and_tenant() -> None:
    rt = scenario_runtime(TIMEOUT)
    assert rt.now == DEFAULT_NOW and rt.allowed_tools is None and rt.documents == ()
    assert [r.behavior.type for r in rt.rules] == ["timeout_after_mutation"]
    assert case_tenant(TIMEOUT) == "demo-co"
    doc = copy.deepcopy(TIMEOUT)
    doc["spec"]["input"]["context"] = {"now": "2026-05-01T00:00:00Z"}
    doc["spec"]["allowedTools"] = ["lookup_order"]
    rt2 = scenario_runtime(doc)
    assert rt2.now == "2026-05-01T00:00:00Z" and rt2.allowed_tools == frozenset({"lookup_order"})
    assert case_tenant(doc) is None


def test_initial_state_merges_the_scenario_state() -> None:
    doc = copy.deepcopy(TIMEOUT)
    doc["spec"]["state"] = {"orders": {"ORD-1001": {"refunded_amount": 10}, "ORD-2001": None}}
    state = initial_state(TWIN, doc)
    assert state["orders"]["ORD-1001"]["refunded_amount"] == 10
    assert state["orders"]["ORD-1001"]["total"] == 150  # the rest of the record is kept
    assert "ORD-2001" not in state["orders"]
    # The definition itself is never modified.
    assert TWIN.initial_state["orders"]["ORD-1001"]["refunded_amount"] == 0
    assert "ORD-2001" in TWIN.initial_state["orders"]


def test_foreign_values_are_unique_to_other_tenants() -> None:
    values = foreign_values(TWIN.initial_state, "tenant", "demo-co")
    for leak in ("CUS-200", "Mallory Moss", "mallory@other.example", "ORD-2001", "other-co"):
        assert leak in values
    # Values other tenants share with the case's tenant are not leaks.
    for shared in ("demo-co", "USD", "delivered", "gold", "Eligible.", "CUS-100"):
        assert shared not in values
    assert foreign_values(TWIN.initial_state, None, "demo-co") == ()
    assert foreign_values(TWIN.initial_state, "tenant", None) == ()


def test_agent_request_carries_the_case_capability_only() -> None:
    run = {"id": "run-1", "agent_version": "1.2.4", "release_id": None}
    case = {
        "id": "case-1",
        "scenario_id": "sc-1",
        "scenario_name": "refund-timeout-after-mutation",
        "tenant": "demo-co",
    }
    body = agent_request(doc=TIMEOUT, run=run, case=case, twin_url="http://twin/twin/v1", token="tok")
    assert body["input"].startswith("Hi! One item in ORD-1001")
    assert body["customer_id"] == "CUS-100" and body["tenant"] == "demo-co"
    assert body["tools_base_url"] == "http://twin/twin/v1"
    assert body["tool_headers"] == {"Authorization": "Bearer tok"}
    assert body["session_id"] == "sim-case-1"
    assert body["run_context"]["source"] == "simulation"
    assert body["run_context"]["simulation_run_id"] == "run-1"
    assert body["run_context"]["scenario_id"] == "sc-1"
    assert "context" not in body  # tenant/now/customer_id are consumed, nothing else was set


def _step(seq: int, tool: str, **record: Any) -> dict[str, Any]:
    base = {
        "seq": seq,
        "call_number": 1,
        "tool": tool,
        "arguments": {},
        "http_status": 200,
        "status": "ok",
        "risk": "READ",
    }
    return {"kind": "tool_call", "seq": seq, "tool": tool, "record": base | record, "latency_ms": 3.0}


def test_evaluation_context_uses_the_twin_records() -> None:
    steps = [
        _step(1, "lookup_order"),
        {"kind": "retrieval", "seq": 2, "tool": None, "record": {"query": "x"}, "latency_ms": None},
        _step(
            3,
            "refund_payment",
            risk="WRITE_IRREVERSIBLE",
            mutated=True,
            expects_mutation=True,
            http_status=504,
            status="timeout",
            fault="timeout_after_mutation",
        ),
    ]
    ctx = evaluation_context(
        doc=TIMEOUT,
        definition=TWIN,
        agent_result={"output": "Done!", "claimed_outcome": "success", "status": "completed", "steps": 4},
        steps=steps,
        state_before={"a": 1},
        state_after={"a": 2},
        tenant="demo-co",
        latency_ms=12.5,
    )
    assert [c.tool for c in ctx.tool_calls] == [
        "lookup_order",
        "refund_payment",
    ]  # retrievals are not tool calls
    assert ctx.tool_calls[1].mutated and ctx.tool_calls[1].fault == "timeout_after_mutation"
    assert ctx.claimed_outcome == "SUCCESS" and ctx.agent_status == "completed" and ctx.steps == 4
    assert ctx.secrets == tuple(TWIN.secrets) and ctx.tenant == "demo-co"
    assert ctx.latency_ms == 12.5 and ctx.cost_usd is None
    empty = evaluation_context(
        doc=TIMEOUT,
        definition=TWIN,
        agent_result=None,
        steps=[],
        state_before={},
        state_after={},
        tenant=None,
        latency_ms=1.0,
    )
    assert empty.output is None and empty.claimed_outcome is None and empty.foreign_values == ()


def test_valid_trace_id() -> None:
    assert valid_trace_id("0af7651916cd43dd8448eb211c80319c") == "0af7651916cd43dd8448eb211c80319c"
    for bad in (None, 5, "0AF7651916CD43DD8448EB211C80319C", "abc", "0af7651916cd43dd8448eb211c80319c0"):
        assert valid_trace_id(bad) is None


def test_outcome_body_reports_the_verdict_against_the_twin_state() -> None:
    results = [
        {
            "status": "FAIL",
            "evidence": [
                {
                    "kind": "state",
                    "ref": "orders.ORD-1001.refund_count",
                    "expected": {"equals": 1},
                    "actual": 2,
                },
                {"kind": "tool_call", "ref": "tool_call:3", "detail": "x"},
            ],
        }
    ]
    failed = outcome_body(
        {
            "status": "FAILED",
            "scenario_name": "refund-timeout-after-mutation",
            "agent_result": {"claimed_outcome": "SUCCESS", "business_outcome": "REFUND_COMPLETED"},
            "verdict": {"reason": "State orders.ORD-1001.refund_count: expected 1, got 2."},
            "results": results,
        }
    )
    assert failed["status"] == "FAILURE" and failed["verified"] is True
    assert failed["verification_source"] == "tool_twin_state"
    # Claimed success + verified failure: the trace service flags a contradiction.
    assert failed["claimed_status"] == "SUCCESS" and failed["business_outcome"] == "REFUND_COMPLETED"
    assert failed["expected_state"] == {"orders.ORD-1001.refund_count": {"equals": 1}}
    assert failed["actual_state"] == {"orders.ORD-1001.refund_count": 2}
    assert failed["notes"].startswith("Simulation case refund-timeout-after-mutation: State")
    passed = outcome_body(
        {"status": "PASSED", "scenario_name": "s", "agent_result": {}, "verdict": {}, "results": []}
    )
    assert passed["status"] == "SUCCESS" and passed["verified"] is True and "claimed_status" not in passed
    errored = outcome_body({"status": "ERRORED", "scenario_name": "s", "agent_result": None, "results": None})
    assert errored == {
        "status": "UNKNOWN",
        "verified": False,
        "verification_source": "unavailable",
        "notes": "Simulation case s: ",
    }


def test_synthetic_results_are_critical() -> None:
    r = synthetic_result("FAIL", "AGENT_TIMEOUT", "too slow")
    assert r.critical and r.label == "AGENT_TIMEOUT" and r.evaluator == "simulation.agent_run"
