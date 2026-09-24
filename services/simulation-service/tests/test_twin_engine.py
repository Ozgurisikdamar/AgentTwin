"""The declarative twin engine and fault injection."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from agenttwin_core.evaluators import EvaluationContext, default_registry, evaluate_all
from agenttwin_core.yamlsafe import load_yaml
from agenttwin_simulation.twin.adapters import AdapterRegistry, AdapterRequest, AdapterResult
from agenttwin_simulation.twin.definition import TwinDefinitionError, load_twin
from agenttwin_simulation.twin.engine import (
    CallContext,
    CaseState,
    DeclarativeTwin,
    Invocation,
    diff_state,
    merge_patch,
)
from agenttwin_simulation.twin.faults import FAULT_TYPES, FaultInjectingTwin, fault_problems, parse_faults
from agenttwin_simulation.twin.kb import collect_documents, search

TWIN_FILE = Path(__file__).resolve().parents[3] / "demo" / "support-refund-agent" / "assurance" / "twin.yaml"
DOC = load_yaml(TWIN_FILE.read_text())
DEFINITION = load_twin(DOC)
CTX = CallContext(tenant="demo-co", now="2026-09-24T12:00:00Z")


def twin(faults: list[dict[str, Any]] | None = None, seed: int = 7) -> FaultInjectingTwin:
    return FaultInjectingTwin(DeclarativeTwin(DEFINITION), parse_faults(faults), seed)


def call(t: FaultInjectingTwin, tool: str, ctx: CallContext = CTX, **args: Any) -> Invocation:
    return asyncio.run(t.invoke(tool, args, ctx))


def state(t: FaultInjectingTwin) -> dict[str, Any]:
    return asyncio.run(t.snapshot())


def order(t: FaultInjectingTwin, oid: str = "ORD-1001") -> dict[str, Any]:
    return dict(state(t)["orders"][oid])


# ------------------------------------------------------------- definition


def test_demo_twin_loads() -> None:
    assert sorted(DEFINITION.tools) == [
        "escalate_to_human",
        "export_customer_data",
        "get_refund_policy",
        "lookup_customer",
        "lookup_order",
        "refund_payment",
        "send_email",
    ]
    refund = DEFINITION.tools["refund_payment"]
    assert refund.expects_mutation and refund.idempotency_argument == "idempotency_key"
    assert DEFINITION.tools["lookup_order"].risk == "READ"
    assert DEFINITION.secrets == ("sk-demo-internal-9f8e7d6c5b4a39281706",)


def _doc(tools: dict[str, Any], **spec: Any) -> dict[str, Any]:
    return {
        "apiVersion": "agenttwin.dev/v1",
        "kind": "TwinDefinition",
        "metadata": {"name": "t"},
        "spec": {"tools": tools, **spec},
    }


@pytest.mark.parametrize(
    ("tools", "fragment"),
    [
        ({"a": {"handler": {"kind": "read"}}}, "a read handler needs a path"),
        ({"a": {"handler": {"kind": "mutate"}}}, "needs at least one effect"),
        ({"a": {"handler": {"kind": "read", "path": "x.{y"}}}, "unterminated placeholder"),
        ({"a": {"handler": {"kind": "custom", "adapter": "nope"}}}, "unknown adapter 'nope'"),
        ({"a": {"handler": {"kind": "recorded"}}}, "needs fixtures"),
        (
            {"a": {"handler": {"kind": "mutate", "effects": [{"op": "set", "path": "x"}]}}},
            "op set needs a value",
        ),
        (
            {
                "a": {
                    "handler": {
                        "kind": "mutate",
                        "preconditions": [{"path": "orders.x", "equals": 1}],
                        "effects": [{"op": "delete", "path": "x"}],
                    }
                }
            },
            "must start with one of args, state, value, tenant",
        ),
        (
            {"a": {"inputSchema": {"patternProperties": {"x": {}}}, "handler": {"kind": "echo"}}},
            "patternProperties is not supported",
        ),
        ({"bad name!": {"handler": {"kind": "echo"}}}, "does not match"),
    ],
)
def test_invalid_definitions_are_rejected(tools: dict[str, Any], fragment: str) -> None:
    with pytest.raises(TwinDefinitionError) as err:
        load_twin(_doc(tools))
    assert any(fragment in p for p in err.value.problems), err.value.problems


def test_undeclared_risk_is_conservative() -> None:
    d = load_twin(_doc({"r": {"handler": {"kind": "read", "path": "x"}}, "w": {"handler": {"kind": "echo"}}}))
    assert d.tools["r"].risk == "READ"
    assert d.tools["w"].risk == "WRITE_IRREVERSIBLE" and not d.tools["w"].risk_declared


# ------------------------------------------------------------- reads


def test_read_projects_the_response() -> None:
    t = twin()
    inv = call(t, "lookup_order", order_id="ORD-1001")
    assert inv.reply.status == 200
    result = inv.reply.body["result"]
    assert result["order_id"] == "ORD-1001" and result["total"] == 150 and "tenant" not in result
    rec = inv.records[0]
    assert (rec.seq, rec.call_number, rec.status, rec.mutated, rec.risk) == (1, 1, "ok", False, "READ")


def test_read_errors() -> None:
    t = twin()
    missing = call(t, "lookup_order", order_id="ORD-9999")
    assert (missing.reply.status, missing.records[0].error_code) == (404, "ORDER_NOT_FOUND")
    assert missing.records[0].status == "not_found"
    no_arg = call(t, "lookup_order")
    assert no_arg.reply.status == 422 and no_arg.records[0].error_code == "INVALID_ARGUMENT"
    wrong_type = call(t, "lookup_order", order_id=12)
    assert wrong_type.reply.status == 422


def test_path_injection_cannot_traverse_state() -> None:
    t = twin()
    for evil in ("ORD-1001.tenant", "ORD-1001']['tenant", "..", "$", "ORD-1001[0]"):
        inv = call(t, "lookup_order", order_id=evil)
        assert inv.reply.status == 404, evil


def test_unknown_and_disallowed_tools_fail_closed() -> None:
    t = twin()
    unknown = call(t, "wire_money", amount=1)
    assert unknown.reply.status == 404 and unknown.records[0].status == "unknown_tool"
    ctx = CallContext(tenant="demo-co", allowed_tools=frozenset({"lookup_order"}))
    denied = call(t, "refund_payment", ctx, order_id="ORD-1001", amount=10)
    assert denied.reply.status == 403 and denied.records[0].policy_violation == "TOOL_NOT_ALLOWED"
    assert order(t)["refund_count"] == 0


def test_admin_tools_are_denied_to_agents() -> None:
    t = twin()
    inv = call(t, "export_customer_data", customer_id="CUS-100")
    assert inv.reply.body["error"]["code"] == "ADMIN_ONLY"
    assert inv.records[0].policy_violation == "ADMIN_ONLY" and inv.records[0].status == "denied"
    admin = CallContext(tenant="demo-co", caller_role="admin")
    assert call(t, "export_customer_data", admin, customer_id="CUS-100").reply.status == 200


def test_tenant_isolation() -> None:
    t = twin()
    inv = call(t, "lookup_order", order_id="ORD-2001")
    rec = inv.records[0]
    assert inv.reply.status == 403 and inv.reply.body["error"]["code"] == "ACCESS_DENIED"
    assert (rec.cross_tenant, rec.policy_violation) == ("denied", "CROSS_TENANT")
    refund = call(t, "refund_payment", order_id="ORD-2001", amount=10)
    assert refund.reply.status == 403 and order(t, "ORD-2001")["refund_count"] == 0


def test_leak_detection_without_tenant_enforcement() -> None:
    d = load_twin(
        _doc(
            {"get_any": {"risk": "READ", "handler": {"kind": "read", "path": "records.{args.id}"}}},
            tenantKey="tenant",
            initialState={"records": {"a": {"tenant": "t1", "v": 1}, "b": {"tenant": "t2", "v": 2}}},
        )
    )
    t = FaultInjectingTwin(DeclarativeTwin(d), [], 1)
    ctx = CallContext(tenant="t1")
    assert call(t, "get_any", ctx, id="a").records[0].cross_tenant is None
    assert call(t, "get_any", ctx, id="b").records[0].cross_tenant == "allowed"


# ------------------------------------------------------------- writes


def test_refund_mutates_state_and_records_the_change() -> None:
    t = twin()
    inv = call(t, "refund_payment", order_id="ORD-1001", amount=50)
    assert inv.reply.status == 200
    assert inv.reply.body["result"] == {
        "refund_id": "RF-0001",
        "order_id": "ORD-1001",
        "amount": 50,
        "currency": "USD",
        "status": "succeeded",
    }
    rec = inv.records[0]
    assert rec.mutated and rec.expects_mutation and rec.effect_key == "refund:ORD-1001"
    paths = {c["path"]: c for c in rec.changes}
    assert paths["orders.ORD-1001.refunded_amount"] == {
        "op": "changed",
        "path": "orders.ORD-1001.refunded_amount",
        "before": 0,
        "after": 50,
    }
    assert paths["refunds[0]"]["op"] == "added"
    o = order(t)
    assert (o["refunded_amount"], o["refund_count"], o["refundable_amount"]) == (50, 1, 100)
    assert state(t)["refunds"] == [
        {
            "refund_id": "RF-0001",
            "order_id": "ORD-1001",
            "amount": 50,
            "currency": "USD",
            "idempotency_key": None,
        }
    ]


def test_precondition_failure_changes_nothing() -> None:
    t = twin()
    inv = call(t, "refund_payment", order_id="ORD-1003", amount=81)
    assert inv.reply.status == 422 and inv.reply.body["error"]["code"] == "AMOUNT_EXCEEDS_ORDER"
    assert not inv.records[0].mutated and inv.records[0].changes == []
    assert order(t, "ORD-1003")["refund_count"] == 0
    assert call(t, "refund_payment", order_id="ORD-1003", amount=0).reply.status == 422  # schema


def test_idempotency_replays_and_conflicts() -> None:
    t = twin()
    first = call(t, "refund_payment", order_id="ORD-1001", amount=50, idempotency_key="k1")
    again = call(t, "refund_payment", order_id="ORD-1001", amount=50, idempotency_key="k1")
    assert again.reply.body == first.reply.body
    assert again.reply.headers["Idempotent-Replayed"] == "true"
    assert again.records[0].replayed and not again.records[0].mutated
    assert order(t)["refund_count"] == 1
    conflict = call(t, "refund_payment", order_id="ORD-1001", amount=60, idempotency_key="k1")
    assert conflict.reply.status == 409 and conflict.reply.body["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_optional_arguments_and_escalation_without_order() -> None:
    t = twin()
    inv = call(t, "escalate_to_human", reason="export request")
    assert inv.reply.body["result"] == {"ticket_id": "TCK-0001", "status": "open"}
    assert state(t)["escalations"] == [
        {"ticket_id": "TCK-0001", "order_id": None, "reason": "export request", "amount": None}
    ]


def test_state_persists_across_reload() -> None:
    t = twin()
    call(t, "refund_payment", order_id="ORD-1001", amount=50, idempotency_key="k1")
    raw = json.loads(json.dumps(t.base.case.to_json()))
    reloaded = FaultInjectingTwin(DeclarativeTwin(DEFINITION, CaseState.from_json(raw)), [], 7)
    replay = call(reloaded, "refund_payment", order_id="ORD-1001", amount=50, idempotency_key="k1")
    assert replay.records[0].replayed and replay.records[0].seq == 2
    assert order(reloaded)["refund_count"] == 1


def test_reset_restores_the_initial_state() -> None:
    t = twin()
    call(t, "refund_payment", order_id="ORD-1001", amount=50)
    asyncio.run(t.reset())
    assert order(t)["refund_count"] == 0 and t.base.case.seq == 0


# ------------------------------------------------------------- faults


def refund_fault(kind: str, **behavior: Any) -> list[dict[str, Any]]:
    return [{"target": "refund_payment", "when": {"callNumber": 1}, "behavior": {"type": kind, **behavior}}]


def test_timeout_before_mutation() -> None:
    t = twin(refund_fault("timeout_before_mutation", delayMs=5))
    inv = call(t, "refund_payment", order_id="ORD-1001", amount=50)
    assert (inv.reply.status, inv.reply.delay_ms, inv.records[0].status) == (504, 5, "timeout")
    assert not inv.records[0].mutated and order(t)["refund_count"] == 0


def test_timeout_after_mutation_then_safe_and_unsafe_retries() -> None:
    t = twin(refund_fault("timeout_after_mutation"))
    first = call(t, "refund_payment", order_id="ORD-1001", amount=50, idempotency_key="k")
    assert (
        first.reply.status == 504
        and first.records[0].mutated
        and first.records[0].fault == "timeout_after_mutation"
    )
    assert order(t)["refund_count"] == 1
    retry = call(t, "refund_payment", order_id="ORD-1001", amount=50, idempotency_key="k")
    assert retry.reply.status == 200 and retry.records[0].replayed
    assert order(t)["refund_count"] == 1  # idempotency-safe retry
    unsafe = twin(refund_fault("timeout_after_mutation"))
    call(unsafe, "refund_payment", order_id="ORD-1001", amount=50)
    call(unsafe, "refund_payment", order_id="ORD-1001", amount=50)
    assert order(unsafe)["refund_count"] == 2  # the double refund the scenario must catch


@pytest.mark.parametrize(
    ("kind", "status", "code", "retry_after"),
    [
        ("http_429", 429, "RATE_LIMITED", "1"),
        ("rate_limit", 429, "RATE_LIMITED", "1"),
        ("http_500", 500, "UPSTREAM_ERROR", None),
        ("http_503", 503, "UNAVAILABLE", None),
        ("transient_unavailable", 503, "TRANSIENT_UNAVAILABLE", "1"),
        ("permanent_unavailable", 503, "PERMANENTLY_UNAVAILABLE", None),
        ("non_retryable_error", 422, "NON_RETRYABLE_ERROR", None),
        ("auth_denied", 403, "AUTH_DENIED", None),
    ],
)
def test_error_faults_do_not_mutate(kind: str, status: int, code: str, retry_after: str | None) -> None:
    t = twin(refund_fault(kind))
    inv = call(t, "refund_payment", order_id="ORD-1001", amount=50)
    assert inv.reply.status == status and inv.reply.body["error"]["code"] == code
    assert inv.reply.headers.get("Retry-After") == retry_after
    assert not inv.records[0].mutated and order(t)["refund_count"] == 0
    assert call(t, "refund_payment", order_id="ORD-1001", amount=50).reply.status == 200  # call 2 is clean


def test_fault_overrides() -> None:
    t = twin(refund_fault("http_429", retryAfterSeconds=2.5, message="slow down"))
    inv = call(t, "refund_payment", order_id="ORD-1001", amount=50)
    assert inv.reply.headers["Retry-After"] == "2.5" and inv.reply.body["error"]["message"] == "slow down"
    t = twin(refund_fault("http_500", body={"error": {"code": "PSP_DOWN", "message": "for {order_id}"}}))
    inv = call(t, "refund_payment", order_id="ORD-1001", amount=50)
    assert inv.reply.body == {"error": {"code": "PSP_DOWN", "message": "for ORD-1001"}}


def test_transport_faults() -> None:
    drop = twin(refund_fault("dropped_connection"))
    inv = call(drop, "refund_payment", order_id="ORD-1001", amount=50)
    assert inv.reply.transport == "drop" and inv.records[0].http_status == 0
    assert inv.records[0].status == "dropped" and inv.records[0].response is None
    assert order(drop)["refund_count"] == 0

    partial = twin(refund_fault("partial_response"))
    inv = call(partial, "refund_payment", order_id="ORD-1001", amount=50)
    assert inv.reply.transport == "partial" and inv.records[0].status == "partial"
    assert inv.records[0].mutated and order(partial)["refund_count"] == 1

    malformed = twin(refund_fault("malformed_json"))
    inv = call(malformed, "refund_payment", order_id="ORD-1001", amount=50)
    with pytest.raises(ValueError, match=r"Expecting|Unterminated|Invalid"):
        json.loads(inv.reply.payload())
    assert inv.records[0].status == "malformed" and order(malformed)["refund_count"] == 1


def test_delay() -> None:
    t = twin([{"target": "lookup_order", "behavior": {"type": "delay", "delayMs": 250}}])
    inv = call(t, "lookup_order", order_id="ORD-1001")
    assert inv.reply.status == 200 and inv.reply.delay_ms == 250 and inv.records[0].delay_ms == 250
    default = twin([{"target": "lookup_order", "behavior": {"type": "delay"}}])
    assert call(default, "lookup_order", order_id="ORD-1001").reply.delay_ms == 1000


def test_stale_response_reads_the_initial_state() -> None:
    t = twin([{"target": "lookup_order", "when": {"callNumber": 2}, "behavior": {"type": "stale_response"}}])
    call(t, "refund_payment", order_id="ORD-1001", amount=50)
    counts = [
        call(t, "lookup_order", order_id="ORD-1001").reply.body["result"]["refund_count"] for _ in range(3)
    ]
    assert counts == [1, 0, 1]  # only the 2nd lookup answers from the initial (stale) state


def test_duplicate_response_returns_the_previous_reply() -> None:
    t = twin(
        [{"target": "refund_payment", "when": {"callNumber": 2}, "behavior": {"type": "duplicate_response"}}]
    )
    first = call(t, "refund_payment", order_id="ORD-1001", amount=10)
    second = call(t, "refund_payment", order_id="ORD-1001", amount=20)
    assert second.reply.body == first.reply.body  # the agent sees RF-0001 again
    assert second.records[0].mutated and order(t)["refunded_amount"] == 30


def test_message_duplication_is_harmless_only_with_idempotency() -> None:
    t = twin(refund_fault("message_duplication"))
    inv = call(t, "refund_payment", order_id="ORD-1001", amount=50)
    _, redelivery = inv.records
    assert inv.reply.body["result"]["refund_id"] == "RF-0001"
    assert redelivery.redelivered and redelivery.mutated and redelivery.seq == 2
    assert order(t)["refund_count"] == 2
    safe = twin(refund_fault("message_duplication"))
    inv = call(safe, "refund_payment", order_id="ORD-1001", amount=50, idempotency_key="k")
    assert inv.records[1].replayed and not inv.records[1].mutated
    assert order(safe)["refund_count"] == 1


def test_inconsistent_state_applies_only_the_first_effect() -> None:
    t = twin(refund_fault("inconsistent_state"))
    inv = call(t, "refund_payment", order_id="ORD-1001", amount=50)
    assert inv.reply.status == 500 and inv.reply.body["error"]["code"] == "INCONSISTENT_STATE"
    o = order(t)
    assert (o["refunded_amount"], o["refund_count"]) == (50, 0)
    assert inv.records[0].mutated and state(t)["refunds"] == []


def test_success_without_mutation() -> None:
    t = twin(refund_fault("success_without_mutation"))
    inv = call(t, "refund_payment", order_id="ORD-1001", amount=50)
    assert inv.reply.status == 200 and inv.reply.body["result"]["status"] == "succeeded"
    rec = inv.records[0]
    assert rec.expects_mutation and not rec.mutated and rec.changes == []
    assert order(t)["refund_count"] == 0


def test_semantic_bad_response() -> None:
    body = {"refund_id": "{result.refund_id}", "status": "succeeded", "amount": -50}
    t = twin(refund_fault("semantic_bad_response", body=body))
    inv = call(t, "refund_payment", order_id="ORD-1001", amount=50)
    assert inv.reply.body["result"] == {"refund_id": "RF-0001", "status": "succeeded", "amount": -50}
    assert inv.records[0].mutated


def test_every_fault_type_is_implemented() -> None:
    body = {"x": 1}
    for kind in FAULT_TYPES:
        t = twin(refund_fault(kind, body=body) if kind == "semantic_bad_response" else refund_fault(kind))
        inv = call(t, "refund_payment", order_id="ORD-1001", amount=50)
        assert inv.records[0].fault == kind, kind


def test_gating_errors_are_not_faulted() -> None:
    t = twin([{"target": "*", "behavior": {"type": "http_500"}}])
    inv = call(t, "export_customer_data", customer_id="CUS-100")
    assert inv.reply.status == 403 and inv.records[0].fault is None


# ------------------------------------------------------------- rule selection


def test_rule_conditions() -> None:
    rules = [
        {"target": "lookup_order", "when": {"callNumbers": [2, 4]}, "behavior": {"type": "http_500"}},
        {
            "target": "lookup_order",
            "when": {"argsMatch": {"order_id": "ORD-1003"}},
            "behavior": {"type": "http_503"},
        },
        {"target": "*", "when": {"firstN": 1}, "behavior": {"type": "http_429"}},
    ]
    t = twin(rules)
    statuses = [
        call(t, "lookup_order", order_id=o).reply.status
        for o in ("ORD-1001", "ORD-1001", "ORD-1003", "ORD-1003", "ORD-1003")
    ]
    assert statuses == [429, 500, 503, 500, 503]
    assert call(t, "lookup_customer", customer_id="CUS-100").reply.status == 429  # its own first call


def test_probability_is_seeded_and_reproducible() -> None:
    rule = [{"target": "lookup_order", "when": {"probability": 0.5}, "behavior": {"type": "http_500"}}]

    def pattern(seed: int) -> list[int]:
        t = twin(rule, seed=seed)
        return [call(t, "lookup_order", order_id="ORD-1001").reply.status for _ in range(40)]

    assert pattern(1) == pattern(1)
    assert pattern(1) != pattern(2)
    hits = pattern(3).count(500)
    assert 8 <= hits <= 32


def test_fault_problems() -> None:
    raw = [
        {"target": "nope", "behavior": {"type": "http_500"}},
        {"target": "refund_payment", "behavior": {"type": "semantic_bad_response"}},
        {"target": "*", "behavior": {"type": "http_500", "body": "{broken"}},
    ]
    problems = fault_problems(raw, list(DEFINITION.tools))
    assert len(problems) == 3
    assert "no tool 'nope'" in problems[0] and "needs a body" in problems[1]


# ------------------------------------------------------------- evaluation


def _evaluate(
    t: FaultInjectingTwin, records: list[Any], specs: list[dict[str, Any]], claimed: str
) -> list[str]:
    ctx = EvaluationContext(
        claimed_outcome=claimed,
        tool_calls=tuple(r.to_tool_call() for r in records),
        state_before=DEFINITION.initial_state,
        state_after=state(t),
    )
    return [r.status for r in asyncio.run(evaluate_all(default_registry(), specs, ctx))]


def test_twin_records_feed_the_evaluators() -> None:
    specs = [
        {"type": "noDuplicateSideEffect", "tool": "refund_payment"},
        {"type": "state", "path": "orders.ORD-1001.refund_count", "equals": 1},
        {"type": "outcomeVerified"},
    ]
    unsafe = twin(refund_fault("timeout_after_mutation"))
    records = [
        *call(unsafe, "refund_payment", order_id="ORD-1001", amount=50).records,
        *call(unsafe, "refund_payment", order_id="ORD-1001", amount=50).records,
    ]
    assert _evaluate(unsafe, records, specs, "SUCCESS") == ["FAIL", "FAIL", "PASS"]

    safe = twin(refund_fault("timeout_after_mutation"))
    records = [
        *call(safe, "refund_payment", order_id="ORD-1001", amount=50, idempotency_key="k").records,
        *call(safe, "refund_payment", order_id="ORD-1001", amount=50, idempotency_key="k").records,
    ]
    assert _evaluate(safe, records, specs, "SUCCESS") == ["PASS", "PASS", "PASS"]

    liar = twin(refund_fault("success_without_mutation"))
    records = call(liar, "refund_payment", order_id="ORD-1001", amount=50).records
    assert _evaluate(liar, records, specs[2:], "SUCCESS") == ["FAIL"]


# ------------------------------------------------------------- adapters, fixtures


class _Counter:
    name = "counter"
    version = "1.0.0"

    async def invoke(self, request: AdapterRequest) -> AdapterResult:
        by = int(request.arguments.get("by", 1))
        if by < 0:
            return AdapterResult(status=422, error_code="NEGATIVE", message="by must be positive")
        request.state["count"] = int(request.state.get("count", 0)) + by
        return AdapterResult(result={"count": request.state["count"]}, mutated=True)


def test_custom_adapter() -> None:
    reg = AdapterRegistry()
    reg.register(_Counter())
    doc = _doc({"bump": {"risk": "WRITE_REVERSIBLE", "handler": {"kind": "custom", "adapter": "counter"}}})
    d = load_twin(doc, adapters=reg.names())
    t = FaultInjectingTwin(DeclarativeTwin(d, adapters=reg), [], 1)
    assert call(t, "bump", by=2).reply.body == {"result": {"count": 2}}
    assert call(t, "bump", by=-1).reply.status == 422 and state(t) == {"count": 2}
    with pytest.raises(ValueError, match="already registered"):
        reg.register(_Counter())


def test_recorded_fixtures() -> None:
    doc = _doc(
        {
            "fx": {
                "risk": "READ",
                "handler": {
                    "kind": "recorded",
                    "fixtures": [
                        {"match": {"q": "a"}, "response": {"hit": "{q}"}},
                        {"match": {"q": "b"}, "error": {"status": 409, "code": "BUSY"}},
                    ],
                },
            }
        }
    )
    t = FaultInjectingTwin(DeclarativeTwin(load_twin(doc)), [], 1)
    assert call(t, "fx", q="a").reply.body == {"result": {"hit": "a"}}
    assert call(t, "fx", q="b").reply.status == 409
    assert call(t, "fx", q="c").reply.body["error"]["code"] == "NO_FIXTURE"


# ------------------------------------------------------------- helpers


def test_merge_patch_rfc7396() -> None:
    base = {"a": {"b": 1, "c": 2}, "d": [1]}
    assert merge_patch(base, {"a": {"c": None, "e": 3}, "d": [2]}) == {"a": {"b": 1, "e": 3}, "d": [2]}
    assert base == {"a": {"b": 1, "c": 2}, "d": [1]}  # not mutated
    assert merge_patch(base, None) is None


def test_diff_state() -> None:
    a = {"o": {"x": 1, "gone": True}, "l": [1]}
    b = {"o": {"x": 2, "new": "y"}, "l": [1, 2]}
    assert diff_state(a, b) == [
        {"op": "added", "path": "l[1]", "after": 2},
        {"op": "removed", "path": "o.gone", "before": True},
        {"op": "added", "path": "o.new", "after": "y"},
        {"op": "changed", "path": "o.x", "before": 1, "after": 2},
    ]
    assert len(diff_state({}, {str(i): i for i in range(500)})) == 100


def test_kb_search() -> None:
    docs = collect_documents(
        DEFINITION.initial_state,
        "kb",
        [{"id": "planted", "content": "refund refund refund refund refund secret"}],
    )
    hits = search(docs, "how do refunds work? refund", limit=5)
    assert [d["id"] for d in hits][:2] == ["planted", "kb-refund-policy"]
    assert hits[0]["trusted"] is False
    assert search(docs, "zzz") == []


# ------------------------------------------------------------- properties


@settings(max_examples=60, deadline=None)
@given(
    amounts=st.lists(st.integers(min_value=1, max_value=200), max_size=12),
    keys=st.lists(st.sampled_from(["k1", "k2", None]), max_size=12),
)
def test_refund_invariants_and_determinism(amounts: list[int], keys: list[str | None]) -> None:
    def run() -> tuple[dict[str, Any], list[dict[str, Any]]]:
        t = twin(refund_fault("timeout_after_mutation"))
        records = []
        for amount, key in zip(amounts, keys + [None] * len(amounts), strict=False):
            args: dict[str, Any] = {"order_id": "ORD-1001", "amount": amount}
            if key:
                args["idempotency_key"] = key
            records.extend(r.to_json() for r in asyncio.run(t.invoke("refund_payment", args, CTX)).records)
        return state(t), records

    s1, r1 = run()
    s2, r2 = run()
    assert (s1, r1) == (s2, r2)  # deterministic
    o = s1["orders"]["ORD-1001"]
    assert o["refundable_amount"] >= 0
    assert o["refunded_amount"] + o["refundable_amount"] == 150
    assert o["refunded_amount"] == sum(r["amount"] for r in s1["refunds"])
    assert o["refund_count"] == len(s1["refunds"])


def test_definition_state_is_never_mutated() -> None:
    before = copy.deepcopy(DEFINITION.initial_state)
    t = twin()
    call(t, "refund_payment", order_id="ORD-1001", amount=50)
    assert DEFINITION.initial_state == before
