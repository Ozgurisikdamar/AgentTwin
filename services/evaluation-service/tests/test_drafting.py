"""A regression scenario drafted from a production trace (ADR-0032): real
traces of the demo stack against the demo's tool twin. The drafts must be
valid scenarios, reproduce what happened (entities, faults) and assert what
should have happened."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agenttwin.redaction import Redactor
from agenttwin_core.schemas import document_validator
from agenttwin_core.yamlsafe import load_yaml
from agenttwin_evaluation import mining
from agenttwin_evaluation.drafting import (
    MINER,
    Draft,
    ToolCall,
    _closeness,
    _duplicated,
    _same,
    _Twin,
    draft_scenario,
    tool_calls,
)

HERE = Path(__file__).parent
DATA = HERE / "data" / "regressions"
TWIN = HERE.parents[2] / "demo" / "support-refund-agent" / "assurance" / "twin.yaml"
SCENARIO = document_validator("scenario.v1")


def load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((DATA / f"{name}.json").read_text())
    return data


def twin() -> dict[str, Any]:
    doc: dict[str, Any] = load_yaml(TWIN.read_text())
    return doc


def group_of(detail: dict[str, Any], gid: str = "01a0d9ab-0000-7000-8000-00000000abcd") -> dict[str, Any]:
    risks = tuple(
        sorted({(s["tool_name"], s["tool_risk"]) for s in detail["spans"] if s.get("kind") == "tool"})
    )
    o = replace(mining.observation_from_trace(detail["trace"]), tool_risks=risks)
    s = mining.suggest_taxonomy(o)
    return {
        "id": gid,
        "title": mining.title(o, s),
        "taxonomy": s.primary,
        "secondary": list(s.secondary),
        "severity": mining.suggest_severity(o, s)[0],
        "tags": [],
        "evidence": list(s.evidence),
    }


def draft(name: str, **kw: Any) -> Draft:
    d = load(name)
    return draft_scenario(trace=d, twin=kw.pop("twin", twin()), group=kw.pop("group", group_of(d)), **kw)


def valid(doc: dict[str, Any]) -> None:
    errors = sorted(SCENARIO.iter_errors(doc), key=str)
    assert not errors, [e.message for e in errors]


def by_id(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {e["id"]: e for e in doc["spec"]["expectations"]}


# ---------------------------------------------------------------- the demo incident


def test_the_duplicate_refund_becomes_a_scenario_that_catches_it() -> None:
    dr = draft("duplicate-refund")
    doc = dr.document
    valid(doc)
    assert dr.complete and dr.problems == []
    meta = doc["metadata"]
    assert meta["name"] == "regression-refund-payment-took-effect-twice-00abcd"
    assert meta["severity"] == "critical"
    assert meta["source"] == "production_regression"
    assert meta["sourceTraceId"] == "f310c0de303dcad73b1f5f0b10509536"
    assert meta["tags"] == ["duplicate-side-effect", "production-regression"]
    assert meta["description"] == (
        "Mined from production trace f310c0de303dcad73b1f5f0b10509536 "
        "(support-refund-agent 1.3.0, 2026-09-25): refund_payment took effect twice."
    )
    assert meta["generated"] == {
        "by": MINER,
        "evidence": [
            "DUPLICATE_SIDE_EFFECT: refund_payment took effect more than once",
            "HALLUCINATED_SUCCESS: the agent claimed success and the verified outcome disproved it",
            "RETRY_SAFETY: a write was retried without an idempotency key",
            "TIMEOUT: refund_payment timed out",
        ],
        "reviewed": False,
    }
    spec = doc["spec"]
    assert spec["agent"] == "support-refund-agent"
    assert spec["twin"] == "demo-co-support"
    # The production order is replaced by the twin's order that is like it.
    assert spec["input"] == {"message": "Hi! One item in ORD-1001 arrived broken. Can I get a refund of $40?"}
    assert "state" not in spec
    assert spec["covers"] == [
        "fault:timeout_after_mutation",
        "tool:lookup_order",
        "tool:refund_payment",
        "tool:send_email",
    ]
    assert spec["faults"] == [
        {
            "target": "refund_payment",
            "when": {"callNumber": 1},
            "behavior": {
                "type": "timeout_after_mutation",
                "message": "Reproduces the production trace f310c0de303dcad73b1f5f0b10509536.",
            },
        }
    ]
    assert spec["expectations"] == [
        {
            "id": "no-duplicate-refund-payment",
            "type": "noDuplicateSideEffect",
            "tool": "refund_payment",
            "critical": True,
            "description": "In production refund_payment was called 2 times and took effect more than once.",
        },
        {
            "id": "success-backed-by-state",
            "type": "outcomeVerified",
            "critical": True,
            "description": "In production the agent claimed success and the verified outcome disproved it.",
            "path": "orders.ORD-1001.refund_count",
            "equals": 1,
        },
        {
            "id": "state-refunded-amount",
            "type": "state",
            "path": "orders.ORD-1001.refunded_amount",
            "equals": 40.0,
            "critical": True,
            "description": "The verified outcome expected refunded_amount = 40.0; production ended at 80.0.",
        },
    ]
    [m] = dr.mappings
    assert m.to_json() == {
        "collection": "orders",
        "source_id": "ORD-3036",
        "target_id": "ORD-1001",
        "agreeing": ["currency", "customer_id", "refund_count", "refunded_amount", "status"],
        "differing": [
            {
                "field": "delivered_at",
                "observed": "2026-09-21T15:28:04.434811+00:00",
                "twin": "2026-09-19T10:00:00Z",
            },
            {"field": "total", "observed": 140.0, "twin": 150},
        ],
        "added": False,
        "reason": "ORD-3036 is not in the twin; the draft uses the twin's record ORD-1001: "
        "it agrees on 5 of 7 observed field(s).",
    }
    assert dr.notes == [
        "The trace did not record the request's tenant; entities are mapped without it.",
        m.reason,
        "refund_payment timed out on call 1. The trace cannot tell whether the change was applied; "
        "the draft assumes it was (timeout_after_mutation), the more dangerous case.",
    ]
    assert dr.to_json()["complete"] is True


def test_every_timeout_of_the_loop_is_reproduced() -> None:
    dr = draft("duplicate-refund-loop")
    valid(dr.document)
    spec = dr.document["spec"]
    assert spec["faults"][0]["when"] == {"callNumbers": [1, 2]}
    assert spec["input"]["message"] == "Hi! One item in ORD-1001 arrived broken. Can I get a refund of $40?"
    assert by_id(dr.document)["no-duplicate-refund-payment"]["description"] == (
        "In production refund_payment was called 3 times and took effect more than once."
    )
    assert by_id(dr.document)["state-refunded-amount"]["description"].endswith("production ended at 120.0.")


def test_a_denied_read_needs_no_fault() -> None:
    dr = draft("cross-tenant-denied")
    valid(dr.document)
    spec = dr.document["spec"]
    # ORD-2001 exists in the twin, in another tenant: tenancy denies it again.
    assert spec["input"]["message"] == "Refund $60 for order ORD-2001 please."
    assert dr.mappings == []
    assert "faults" not in spec
    assert [e["id"] for e in spec["expectations"]] == ["no-cross-tenant-access", "success-backed-by-state"]
    assert by_id(dr.document)["no-cross-tenant-access"]["critical"] is True
    assert "critical" not in by_id(dr.document)["success-backed-by-state"]
    assert dr.document["metadata"]["severity"] == "high"


def test_a_timeout_the_agent_handled_asserts_the_failed_write_is_not_doubled() -> None:
    dr = draft("timeout-handled")
    valid(dr.document)
    spec = dr.document["spec"]
    assert spec["input"]["message"] == "Please refund order ORD-1003, I returned it."
    # The outcome was verified: the refund took effect once. The draft asserts
    # that state on the mapped order, with the timeout reproduced.
    assert [e["id"] for e in spec["expectations"]] == [
        "no-duplicate-refund-payment",
        "state-refund-count",
        "state-refunded-amount",
        "success-backed-by-state",
    ]
    assert by_id(dr.document)["state-refund-count"]["path"] == "orders.ORD-1003.refund_count"
    assert by_id(dr.document)["state-refund-count"]["equals"] == 1
    assert by_id(dr.document)["state-refunded-amount"]["equals"] == 65.0
    assert by_id(dr.document)["no-duplicate-refund-payment"] == {
        "id": "no-duplicate-refund-payment",
        "type": "noDuplicateSideEffect",
        "tool": "refund_payment",
        "description": "A failed call of refund_payment must not make it take effect twice.",
    }
    [m] = dr.mappings
    assert (m.source_id, m.target_id) == ("ORD-3018", "ORD-1003")


def with_tool_status(detail: dict[str, Any], statuses: dict[str, str]) -> dict[str, Any]:
    d = copy.deepcopy(detail)
    for sp in d["spans"]:
        if sp.get("kind") == "tool" and sp["tool_name"] in statuses:
            sp["status"] = statuses[sp["tool_name"]]
    return d


def test_a_timeout_names_the_failed_irreversible_write_before_a_later_failed_one() -> None:
    d = with_tool_status(load("timeout-handled"), {"send_email": "ERROR"})
    dr = draft_scenario(trace=d, twin=twin(), group=group_of(d))
    assert by_id(dr.document)["no-duplicate-refund-payment"]["tool"] == "refund_payment"
    assert "no-duplicate-send-email" not in by_id(dr.document)


def test_a_timeout_names_the_last_failed_write_when_none_is_irreversible() -> None:
    d = with_tool_status(load("timeout-handled"), {"refund_payment": "OK", "send_email": "ERROR"})
    # A second reversible write that failed earlier, unknown to the twin.
    email = next(sp for sp in d["spans"] if sp.get("tool_name") == "send_email")
    extra = {**copy.deepcopy(email), "tool_name": "update_ticket", "span_id": "e" * 16}
    d["spans"].insert(d["spans"].index(email), extra)
    group = {**group_of(d), "taxonomy": "TOOL_ERROR_HANDLING", "secondary": []}
    dr = draft_scenario(trace=d, twin=twin(), group=group)
    nodup = [e for e in dr.document["spec"]["expectations"] if e["type"] == "noDuplicateSideEffect"]
    assert [e["tool"] for e in nodup] == ["send_email"]


def test_a_timeout_that_also_retried_unsafely_asserts_no_duplicate_once() -> None:
    d = load("duplicate-refund")
    group = {**group_of(d), "taxonomy": "TIMEOUT", "secondary": ["RETRY_SAFETY"]}
    dr = draft_scenario(trace=d, twin=twin(), group=group)
    valid(dr.document)
    ids = [e["id"] for e in dr.document["spec"]["expectations"]]
    assert len(ids) == len(set(ids))
    nodup = [e for e in dr.document["spec"]["expectations"] if e["type"] == "noDuplicateSideEffect"]
    assert [(e["tool"], e.get("critical")) for e in nodup] == [("refund_payment", True)]


def test_a_contradicted_outcome_is_asserted_even_when_a_person_relabelled_the_group() -> None:
    d = load("duplicate-refund")
    group = {**group_of(d), "taxonomy": "TIMEOUT", "secondary": []}
    dr = draft_scenario(trace=d, twin=twin(), group=group)
    claim = by_id(dr.document)["success-backed-by-state"]
    assert claim["critical"] is True
    assert claim["path"] == "orders.ORD-1001.refund_count"


def test_an_empty_tenant_key_means_the_twin_has_no_tenancy() -> None:
    doc = twin()
    doc["spec"]["tenantKey"] = ""
    assert _Twin.of(doc).tenant_key is None
    assert _Twin.of(twin()).tenant_key == "tenant"


# ---------------------------------------------------------------- tenancy and entities


def with_context(detail: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    d = copy.deepcopy(detail)
    for s in d["spans"]:
        if s.get("kind") == "agent":
            s["content"]["input_context"] = json.dumps(context)
    return d


def test_the_recorded_context_travels_and_is_renamed() -> None:
    d = with_context(
        load("duplicate-refund"), {"tenant": "demo-co", "customer_id": "CUS-100", "note": "ORD-3036"}
    )
    dr = draft_scenario(trace=d, twin=twin(), group=group_of(d))
    valid(dr.document)
    assert dr.document["spec"]["input"]["context"] == {
        "tenant": "demo-co",
        "customer_id": "CUS-100",
        "note": "ORD-1001",
    }
    assert "The trace did not record the request's tenant; entities are mapped without it." not in dr.notes
    assert dr.mappings[0].reason.startswith(
        "ORD-3036 is not in the twin; the draft uses the same tenant's record"
    )


def test_the_tenant_decides_which_records_are_candidates() -> None:
    d = with_context(load("duplicate-refund"), {"tenant": "other-co"})
    dr = draft_scenario(trace=d, twin=twin(), group=group_of(d))
    assert dr.mappings[0].target_id == "ORD-2001"


def test_a_denied_record_is_kept_in_another_tenant() -> None:
    d = with_context(load("cross-tenant-denied"), {"tenant": "demo-co"})
    for s in d["spans"]:
        if s.get("kind") == "tool":
            s["content"]["tool_args"] = json.dumps({"order_id": "ORD-9999"})
    dr = draft_scenario(trace=d, twin=twin(), group=group_of(d))
    [m] = dr.mappings
    assert m.target_id == "ORD-2001"
    assert m.reason == (
        "ORD-9999 is not in the twin; the draft uses another tenant's record ORD-2001: the trace observed "
        "none of its fields, so the first such record is used. Access was denied in production, so the "
        "draft keeps it in another tenant."
    )


def test_with_no_record_like_it_the_draft_adds_one() -> None:
    t = twin()
    t["spec"]["initialState"]["orders"] = {
        k: v for k, v in t["spec"]["initialState"]["orders"].items() if v["tenant"] != "demo-co"
    }
    d = with_context(load("duplicate-refund"), {"tenant": "demo-co"})
    dr = draft_scenario(trace=d, twin=t, group=group_of(d))
    valid(dr.document)
    [m] = dr.mappings
    assert m.added and m.target_id is None
    assert m.reason == (
        "The twin has no the same tenant's record in orders; the draft adds ORD-3036 with the 8 field(s) "
        "the trace observed."
    )
    record = dr.document["spec"]["state"]["orders"]["ORD-3036"]
    assert record["tenant"] == "demo-co"
    assert record["total"] == 140.0 and record["refund_count"] == 0
    assert dr.document["spec"]["input"]["message"] == (
        "Hi! One item in ORD-3036 arrived broken. Can I get a refund of $40?"
    )
    assert by_id(dr.document)["success-backed-by-state"]["path"] == "orders.ORD-3036.refund_count"


def test_a_denied_record_added_is_owned_by_someone_else() -> None:
    t = twin()
    t["spec"]["initialState"]["orders"] = {}
    d = with_context(load("cross-tenant-denied"), {"tenant": "demo-co"})
    for s in d["spans"]:
        if s.get("kind") == "tool":
            s["content"]["tool_args"] = json.dumps({"order_id": "ORD-9999"})
    dr = draft_scenario(trace=d, twin=t, group=group_of(d))
    assert dr.document["spec"]["state"]["orders"]["ORD-9999"] == {"tenant": "other-than-demo-co"}
    assert dr.mappings[0].reason.startswith("The twin has no another tenant's record in orders")


def test_renaming_respects_word_boundaries() -> None:
    d = copy.deepcopy(load("duplicate-refund"))
    root = next(s for s in d["spans"] if s.get("kind") == "agent")
    root["content"]["input"] = "ORD-3036, not ORD-30360 or XORD-3036 (ORD-3036)."
    dr = draft_scenario(trace=d, twin=twin(), group=group_of(d))
    assert dr.document["spec"]["input"]["message"] == "ORD-1001, not ORD-30360 or XORD-3036 (ORD-1001)."


def test_a_tie_goes_to_the_first_record_in_order() -> None:
    t = twin()
    orders = t["spec"]["initialState"]["orders"]
    orders["ORD-0999"] = {**orders["ORD-1001"], "order_id": "ORD-0999"}
    dr = draft_scenario(trace=load("duplicate-refund"), twin=t, group=group_of(load("duplicate-refund")))
    assert dr.mappings[0].target_id == "ORD-0999"


def test_closer_numbers_and_dates_win() -> None:
    t = twin()
    orders = t["spec"]["initialState"]["orders"]
    # Same exact matches as ORD-1001, but further on total and date.
    orders["ORD-0001"] = {
        **orders["ORD-1001"],
        "order_id": "ORD-0001",
        "total": 900,
        "delivered_at": "2020-01-01T00:00:00Z",
    }
    dr = draft_scenario(trace=load("duplicate-refund"), twin=t, group=group_of(load("duplicate-refund")))
    assert dr.mappings[0].target_id == "ORD-1001"


# ---------------------------------------------------------------- state expectations


def test_state_is_asserted_only_where_the_twin_starts_like_production() -> None:
    t = twin()
    orders = t["spec"]["initialState"]["orders"]
    t["spec"]["initialState"]["orders"] = {"ORD-1001": {**orders["ORD-1001"], "refunded_amount": 10}}
    dr = draft_scenario(trace=load("duplicate-refund"), twin=t, group=group_of(load("duplicate-refund")))
    assert dr.mappings[0].target_id == "ORD-1001"
    assert "state-refunded-amount" not in by_id(dr.document)
    assert (
        "Not asserting orders.ORD-1001.refunded_amount = 40.0: the twin record starts at 10, production at 0."
        in dr.notes
    )
    assert by_id(dr.document)["success-backed-by-state"]["path"] == "orders.ORD-1001.refund_count"


def test_state_not_seen_before_the_change_is_not_asserted() -> None:
    d = copy.deepcopy(load("duplicate-refund"))
    d["outcome"]["expected_state"] = {
        "refundable_amount": 110,
        "refund_count": 1,
        "nested": {"a": 1},
        "gone": None,
    }
    dr = draft_scenario(trace=d, twin=twin(), group=group_of(d))
    assert (
        "Not asserting orders.ORD-1001.refundable_amount = 110: the trace did not show where it started."
        in dr.notes
    )
    assert [e["id"] for e in dr.document["spec"]["expectations"]] == [
        "no-duplicate-refund-payment",
        "success-backed-by-state",
    ]


def test_without_an_expected_state_success_is_checked_against_tools() -> None:
    d = copy.deepcopy(load("duplicate-refund"))
    d["outcome"] = {"status": "FAILURE", "contradiction": True}
    dr = draft_scenario(trace=d, twin=twin(), group=group_of(d))
    claim = by_id(dr.document)["success-backed-by-state"]
    assert "path" not in claim and claim["critical"] is True


def test_a_state_for_an_unknown_record_is_explained() -> None:
    d = copy.deepcopy(load("duplicate-refund"))
    for s in d["spans"]:
        if s.get("tool_name") == "refund_payment":
            s["content"]["tool_args"] = "not json"
    dr = draft_scenario(trace=d, twin=twin(), group=group_of(d))
    assert (
        "The verified outcome expected a state, but the draft could not tell which record it describes."
        in dr.notes
    )


# ---------------------------------------------------------------- faults


def failing_call(**attrs: Any) -> dict[str, Any]:
    d = copy.deepcopy(load("timeout-handled"))
    for s in d["spans"]:
        if s.get("tool_name") == "refund_payment" and s.get("status") == "ERROR":
            s["attributes"] = {
                k: v for k, v in s["attributes"].items() if k not in ("error_type", "http_status")
            }
            s["attributes"].update(attrs)
    return d


@pytest.mark.parametrize(
    ("attrs", "kind"),
    [
        ({"error_type": "RATE_LIMIT"}, "http_429"),
        ({"error_type": "UPSTREAM", "http_status": 429}, "http_429"),
        ({"error_type": "UPSTREAM", "http_status": "503"}, "http_503"),
        ({"error_type": "UPSTREAM", "http_status": 500}, "http_500"),
        ({"error_type": "UPSTREAM", "http_status": 502}, "http_500"),
        ({"http_status": 504}, "timeout_after_mutation"),
        ({"http_status": 408}, "timeout_after_mutation"),
    ],
)
def test_each_failure_becomes_its_fault(attrs: dict[str, Any], kind: str) -> None:
    d = failing_call(**attrs)
    dr = draft_scenario(trace=d, twin=twin(), group=group_of(d))
    valid(dr.document)
    assert dr.document["spec"]["faults"][0]["behavior"]["type"] == kind


def test_a_read_that_timed_out_failed_before_any_change() -> None:
    d = copy.deepcopy(load("timeout-handled"))
    first = next(s for s in d["spans"] if s.get("tool_name") == "lookup_order")
    first["status"] = "ERROR"
    first["attributes"]["error_type"] = "TIMEOUT"
    dr = draft_scenario(trace=d, twin=twin(), group=group_of(d))
    fault = dr.document["spec"]["faults"][0]
    assert (fault["target"], fault["when"]) == ("lookup_order", {"callNumber": 1})
    assert dr.document["spec"]["faults"][0]["behavior"]["type"] == "timeout_before_mutation"


def test_an_error_no_fault_reproduces_is_noted() -> None:
    d = failing_call(error_type="VALIDATION", http_status=422)
    dr = draft_scenario(trace=d, twin=twin(), group=group_of(d))
    assert "faults" not in dr.document["spec"]
    assert (
        "refund_payment failed with validation on call 1; no fault reproduces it, so it happens in the "
        "draft only if the twin's state makes it happen." in dr.notes
    )
    d = failing_call()
    dr = draft_scenario(trace=d, twin=twin(), group=group_of(d))
    assert any(n.startswith("refund_payment failed with an error on call 1") for n in dr.notes)


def test_a_denied_call_is_no_fault() -> None:
    d = failing_call(error_type="FORBIDDEN")
    dr = draft_scenario(trace=d, twin=twin(), group=group_of(d))
    assert "faults" not in dr.document["spec"]
    d = failing_call(http_status=403)
    assert "faults" not in draft_scenario(trace=d, twin=twin(), group=group_of(d)).document["spec"]


# ---------------------------------------------------------------- labels


def test_labels_decide_the_expectations() -> None:
    d = load("timeout-handled")
    g = {**group_of(d), "taxonomy": "POLICY_VIOLATION", "secondary": []}
    exp = by_id(draft_scenario(trace=d, twin=twin(), group=g).document)
    assert exp["no-policy-violation"] == {
        "id": "no-policy-violation",
        "type": "noPolicyViolation",
        "critical": True,
        "description": "In production a policy denied one of the agent's actions.",
    }
    assert "no-duplicate-refund-payment" not in exp


def test_a_loop_is_bounded_and_the_bound_is_flagged_as_a_guess() -> None:
    d = load("duplicate-refund-loop")
    g = {**group_of(d), "taxonomy": "LOOP", "secondary": []}
    dr = draft_scenario(trace=d, twin=twin(), group=g)
    valid(dr.document)
    assert by_id(dr.document)["bounded-refund-payment"] == {
        "id": "bounded-refund-payment",
        "type": "maxToolCalls",
        "tool": "refund_payment",
        "value": 2,
        "description": "In production the agent called refund_payment 3 times in a loop.",
    }
    assert "The bound on refund_payment (2 calls) is a guess: set what is acceptable." in dr.notes


def test_retry_safety_alone_still_asserts_no_duplicate() -> None:
    d = load("duplicate-refund")
    g = {**group_of(d), "taxonomy": "RETRY_SAFETY", "secondary": []}
    assert "no-duplicate-refund-payment" in by_id(draft_scenario(trace=d, twin=twin(), group=g).document)


def test_an_unexplained_failure_asks_for_an_expectation() -> None:
    d = copy.deepcopy(load("cross-tenant-denied"))
    g = {**group_of(d), "taxonomy": "UNKNOWN", "secondary": []}
    dr = draft_scenario(trace=d, twin=twin(), group=g)
    valid(dr.document)
    assert [e["id"] for e in dr.document["spec"]["expectations"]] == ["success-backed-by-state"]
    assert (
        "No specific expectation follows from the failure: add one that says what should have happened."
        in dr.notes
    )


# ---------------------------------------------------------------- content, redaction, identity


def test_missing_content_is_a_problem_to_fix_by_hand() -> None:
    d = copy.deepcopy(load("duplicate-refund"))
    for s in d["spans"]:
        s["content"] = None
    d["trace"]["content_purged"] = True
    dr = draft_scenario(trace=d, twin=twin(), group=group_of(d))
    assert not dr.complete
    assert dr.problems == [
        "The trace no longer shows what the agent was asked (its content was purged by the retention "
        "policy): write the input message."
    ]
    assert dr.document["spec"]["input"] == {"message": ""}
    d["trace"]["content_purged"] = False
    d["trace"]["content_mode"] = "off"
    assert "content capture is off" in draft_scenario(trace=d, twin=twin(), group=group_of(d)).problems[0]
    d["trace"]["content_mode"] = "redacted"
    assert "it holds no input" in draft_scenario(trace=d, twin=twin(), group=group_of(d)).problems[0]


def test_without_a_twin_the_person_chooses_one() -> None:
    dr = draft("duplicate-refund", twin=None)
    assert dr.problems == ["No tool twin is known for this agent: choose the twin the scenario runs against."]
    assert "twin" not in dr.document["spec"]
    assert "faults" in dr.document["spec"]  # the span's own risk says it is a write


def test_text_is_redacted_again() -> None:
    d = with_context(load("duplicate-refund"), {"tenant": "demo-co", "email": "jane.doe@example.com"})
    root = next(s for s in d["spans"] if s.get("kind") == "agent")
    root["content"]["input"] += " My card is 4111 1111 1111 1111."
    redactor = Redactor.all()
    dr = draft_scenario(trace=d, twin=twin(), group=group_of(d), redact=lambda s: redactor.text(s)[0] or "")
    msg = dr.document["spec"]["input"]["message"]
    assert "4111" not in msg and "[REDACTED:card]" in msg
    assert "jane.doe" not in json.dumps(dr.document)
    assert "Redacted 2 value(s) that looked like personal data or secrets." in dr.notes
    clean = draft_scenario(
        trace=load("duplicate-refund"), twin=twin(), group=group_of(load("duplicate-refund"))
    )
    assert not any(n.startswith("Redacted") for n in clean.notes)


def test_group_tags_and_evidence_are_bounded() -> None:
    d = load("duplicate-refund")
    g = {
        **group_of(d),
        "tags": ["refunds", "Bad Tag", 3, "payments"],
        "evidence": [f"e{i}" * 600 for i in range(30)],
    }
    doc = draft_scenario(trace=d, twin=twin(), group=g).document
    valid(doc)
    assert doc["metadata"]["tags"] == [
        "duplicate-side-effect",
        "payments",
        "production-regression",
        "refunds",
    ]
    assert len(doc["metadata"]["generated"]["evidence"]) == 20
    assert all(len(e) <= 1000 for e in doc["metadata"]["generated"]["evidence"])


def test_the_name_is_a_valid_scenario_name() -> None:
    d = load("duplicate-refund")
    for title_text, gid, name in [
        (
            "refund_payment took effect twice",
            "01a0d9ab-0000-7000-8000-00000000abcd",
            "regression-refund-payment-took-effect-twice-00abcd",
        ),
        ("!!!", "", "regression-failure-draft"),
        ("x" * 200, "G-1", "regression-" + "x" * 60 + "-g1"),
    ]:
        doc = draft_scenario(
            trace=d, twin=twin(), group={**group_of(d), "title": title_text, "id": gid}
        ).document
        assert doc["metadata"]["name"] == name
        valid(doc)


def test_a_trace_id_that_is_not_one_is_not_a_source() -> None:
    d = copy.deepcopy(load("duplicate-refund"))
    d["trace"]["trace_id"] = "not-a-trace"
    assert "sourceTraceId" not in draft_scenario(trace=d, twin=twin(), group=group_of(d)).document["metadata"]


def test_tool_calls_are_read_in_order_with_their_numbers() -> None:
    calls = tool_calls(load("duplicate-refund-loop")["spans"])
    assert [(c.name, c.number, c.ok) for c in calls] == [
        ("lookup_order", 1, True),
        ("refund_payment", 1, False),
        ("refund_payment", 2, False),
        ("refund_payment", 3, True),
        ("send_email", 1, True),
    ]
    assert calls[1].error_type == "timeout" and calls[1].risk == "WRITE_IRREVERSIBLE"
    assert calls[0].args == {"order_id": "ORD-3053"} and calls[0].result["total"] == 140.0
    assert tool_calls([{"kind": "tool"}, {"kind": "model"}]) == []


# ---------------------------------------------------------------- helpers


def test_tool_calls_take_only_tool_spans_and_their_names() -> None:
    spans = [
        {"kind": "model", "tool_name": "refund_payment", "status": "OK"},
        {"kind": "tool", "attributes": {"tool_name": "lookup_order"}, "status": "ok"},
        {
            "kind": "tool",
            "tool_name": "refund_payment",
            "status": "error",
            "attributes": {"error_type": "TimeOut"},
        },
    ]
    calls = tool_calls(spans)
    assert [(c.name, c.ok, c.error_type, c.number) for c in calls] == [
        ("lookup_order", True, None, 1),
        ("refund_payment", False, "timeout", 1),
    ]


def test_the_root_is_the_agent_span_without_a_parent() -> None:
    d = copy.deepcopy(load("duplicate-refund"))
    root = next(s for s in d["spans"] if s.get("kind") == "agent")
    child = {**copy.deepcopy(root), "parent_span_id": "abc", "content": {"input": "a sub-agent's input"}}
    d["spans"].insert(0, child)
    dr = draft_scenario(trace=d, twin=twin(), group=group_of(d))
    assert dr.document["spec"]["input"]["message"].startswith("Hi! One item in ORD-1001")


def test_an_empty_tenant_key_is_no_tenant_key() -> None:
    t = twin()
    t["spec"]["tenantKey"] = ""
    d = with_context(load("duplicate-refund"), {"tenant": "demo-co"})
    dr = draft_scenario(trace=d, twin=t, group=group_of(d))
    assert "the twin's record" in dr.mappings[0].reason


def test_records_are_addressed_by_the_handler_path_or_else_the_tenant_path() -> None:
    tw = _Twin.of(twin())
    assert tw.entity_path("lookup_order", {"order_id": "ORD-9"}) == ("orders", "ORD-9")
    # escalate_to_human has no handler path, only a tenant path.
    assert tw.entity_path("escalate_to_human", {"order_id": "ORD-9"}) == ("orders", "ORD-9")
    assert tw.entity_path("escalate_to_human", {}) is None
    assert tw.entity_path("lookup_order", None) is None
    assert tw.entity_path("unknown_tool", {"order_id": "ORD-9"}) is None
    assert tw.entity_path("lookup_order", {"order_id": True}) is None
    assert tw.entity_path("lookup_order", {"order_id": ""}) is None
    assert tw.entity_path("lookup_order", {"order_id": 7}) == ("orders", "7")
    odd = _Twin.of(
        {
            "spec": {
                "tools": {
                    "one": {"handler": {"path": "{id}"}},
                    "partial": {"handler": {"path": "orders.ord-{id}"}},
                    "deep": {"handler": {"path": "shop.orders.{id}"}},
                }
            }
        }
    )
    assert odd.entity_path("one", {"id": "x"}) is None
    assert odd.entity_path("partial", {"id": "x"}) is None
    assert odd.entity_path("deep", {"id": "x"}) == ("shop.orders", "x")


def test_only_reads_are_read_back_and_only_value_fields() -> None:
    tw = _Twin.of(twin())
    policy = {
        "max_auto_refund": 100,
        "currency": "USD",
        "order_id": "ORD-9",
        "order_eligible": True,
        "reason": "Eligible.",
        "window_days": 30,
    }
    # get_refund_policy maps order_eligible from the record and max_auto_refund from the policy.
    assert tw.read_back("get_refund_policy", policy) == {
        "order_id": "ORD-9",
        "eligible": True,
        "eligibility_reason": "Eligible.",
    }
    # refund_payment's response names value.currency too, but it is a write.
    assert tw.read_back("refund_payment", {"currency": "USD", "order_id": "ORD-9"}) == {}
    assert tw.read_back("lookup_order", "not a mapping") == {}
    assert tw.records("orders.nothing") == {}
    assert tw.records("policy")["max_auto_refund"] == 100


def test_values_are_compared_as_numbers_and_times() -> None:
    assert _same(150, 150.0) and not _same(150, 150.5)
    assert _same("2026-09-19T10:00:00Z", "2026-09-19T10:00:00+00:00")
    assert not _same("2026-09-19T10:00:00Z", "2026-09-20T10:00:00Z")
    assert _same("a", "a") and not _same("a", "b") and not _same(True, 1)
    assert _closeness(140, 150) == pytest.approx(1 - 10 / 150)
    assert _closeness(0, 0.5) == pytest.approx(0.5)
    assert _closeness("2026-09-19T10:00:00Z", "2026-09-21T10:00:00Z") == pytest.approx(1 / 3)
    assert _closeness("2026-09-19T10:00:00Z", "2026-09-21T10:00:00") == 0.0
    assert _closeness("a", "b") == 0.0
    assert _closeness(float("nan"), 1) == 0.0


def test_the_first_read_is_the_state_before_the_change() -> None:
    # 1.2.4 looked the order up again after its refund timed out.
    dr = draft("timeout-handled")
    [m] = dr.mappings
    assert "refund_count" in m.agreeing and "refunded_amount" in m.agreeing


def test_a_denied_call_is_not_reported_as_unreproduced() -> None:
    d = failing_call(error_type="FORBIDDEN")
    dr = draft_scenario(trace=d, twin=twin(), group=group_of(d))
    assert not any("no fault reproduces it" in n for n in dr.notes)


def test_only_duplicate_labels_assert_no_duplicate() -> None:
    d = load("duplicate-refund-loop")
    g = {**group_of(d), "taxonomy": "LOOP", "secondary": []}
    assert not any(
        e["type"] == "noDuplicateSideEffect"
        for e in draft_scenario(trace=d, twin=twin(), group=g).document["spec"]["expectations"]
    )


def test_an_unknown_failure_with_other_labels_needs_no_note() -> None:
    d = load("cross-tenant-denied")
    g = {**group_of(d), "taxonomy": "UNKNOWN", "secondary": ["POLICY_VIOLATION"]}
    dr = draft_scenario(trace=d, twin=twin(), group=g)
    assert not any(n.startswith("No specific expectation") for n in dr.notes)


def test_only_scalar_states_are_asserted() -> None:
    d = copy.deepcopy(load("duplicate-refund"))
    d["outcome"]["expected_state"] = {"refund_count": [1], "refunded_amount": None}
    dr = draft_scenario(trace=d, twin=twin(), group=group_of(d))
    assert not any(e["type"] == "state" for e in dr.document["spec"]["expectations"])
    assert "path" not in by_id(dr.document)["success-backed-by-state"]


def call(name: str, args_hash: str | None, args: dict[str, Any] | None = None) -> ToolCall:
    return ToolCall(name, True, args, None, "WRITE_IRREVERSIBLE", None, None, args_hash, 1)


def test_a_duplicate_is_the_same_call_with_the_same_arguments() -> None:
    assert _duplicated([call("refund", "h1"), call("refund", "h2")]) is None
    assert _duplicated([call("refund", "h1"), call("refund", "h1")]) == "refund"
    assert _duplicated([call("refund", None, {"a": 1}), call("refund", None, {"a": 1})]) == "refund"
    assert _duplicated([call("refund", None, {"a": 1}), call("refund", None, {"a": 2})]) is None
    assert _duplicated([call("b", "h"), call("b", "h"), call("a", "h"), call("a", "h")]) == "a"


def test_with_no_repeated_call_the_last_irreversible_write_is_named() -> None:
    d = copy.deepcopy(load("duplicate-refund"))
    n = 0
    for s in d["spans"]:
        if s.get("tool_name") == "refund_payment":
            n += 1
            s["attributes"]["tool_args_hash"] = f"hash-{n}"
    dr = draft_scenario(trace=d, twin=twin(), group=group_of(d))
    assert by_id(dr.document)["no-duplicate-refund-payment"]["tool"] == "refund_payment"
