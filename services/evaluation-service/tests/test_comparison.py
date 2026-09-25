"""Baseline versus candidate (spec §27): cases are classified from their
expectations, metrics are compared alongside, and no summary averages a
single new critical failure away."""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from agenttwin_core import api_fakes as fake
from agenttwin_core.evaluators import EvaluationResult
from agenttwin_evaluation.comparison import CLASSIFICATIONS, CaseComparison, Side, compare_case, summarize

ORDER = {"order_id": "ORD-1001"}


def result(
    eid: str, status: str, *, critical: bool = False, type_: str = "state", **over: Any
) -> dict[str, Any]:
    return fake.expectation_result(eid, status, type_=type_, critical=critical, **over)


def side(
    status: str, *results: dict[str, Any], steps: list[dict[str, Any]] | None = None, **over: Any
) -> Side:
    judged = over.pop("judged", ())
    usage = {k: over.pop(k) for k in ("tokens", "cost_usd") if k in over}
    detail = fake.case_detail(0, "refund-happy-path", status, results=list(results), steps=steps, **over)
    return Side.from_case_detail(detail, judged=judged, **usage)


def compare(
    b: Side, c: Side, severity: str = "critical", tags: tuple[str, ...] = ("refunds",)
) -> CaseComparison:
    return compare_case("refund-happy-path", severity, tags, b, c)


PASS = side("PASSED", result("refunded-once", "PASS", critical=True), result("polite", "PASS"))


@pytest.mark.parametrize(
    ("baseline", "candidate", "expected", "reason"),
    [
        (PASS, PASS, "UNCHANGED", "Same result on both sides."),
        (
            PASS,
            side(
                "FAILED",
                result("refunded-once", "FAIL", critical=True, reason="refund_count 2"),
                result("polite", "PASS"),
            ),
            "NEW_CRITICAL_FAILURE",
            "New critical failure refunded-once: refund_count 2",
        ),
        (
            PASS,
            side("FAILED", result("refunded-once", "PASS", critical=True), result("polite", "FAIL")),
            "REGRESSED",
            "Newly failing: polite.",
        ),
        (
            side("FAILED", result("refunded-once", "PASS", critical=True), result("polite", "FAIL")),
            PASS,
            "IMPROVED",
            "Now passing: polite.",
        ),
        (
            side("FAILED", result("refunded-once", "FAIL", critical=True), result("polite", "PASS")),
            side("FAILED", result("refunded-once", "FAIL", critical=True), result("polite", "PASS")),
            "UNCHANGED",
            "Still failing on both sides: refunded-once.",
        ),
        (
            # Fixed one, broke another: the new failure wins.
            side("FAILED", result("refunded-once", "PASS", critical=True), result("polite", "FAIL")),
            side(
                "FAILED",
                result("refunded-once", "PASS", critical=True),
                result("polite", "PASS"),
                result("x", "FAIL"),
            ),
            "REGRESSED",
            "Newly failing: x.",
        ),
        (
            PASS,
            side(
                "ERRORED", result("refunded-once", "ERROR", critical=True), reason="The agent did not answer."
            ),
            "INCOMPLETE",
            "The candidate's case ended ERRORED: The agent did not answer.",
        ),
        (
            side("CANCELLED", result("refunded-once", "SKIPPED", critical=True)),
            PASS,
            "INCOMPLETE",
            "The baseline's case ended CANCELLED",
        ),
    ],
)
def test_classification(baseline: Side, candidate: Side, expected: str, reason: str) -> None:
    c = compare(baseline, candidate)
    assert (c.classification, c.reason) == (expected, reason)


def test_a_failure_the_baseline_could_not_evaluate_still_counts_as_new() -> None:
    # A regression must be visible; an improvement must be shown.
    unverified = side("FAILED", result("refunded-once", "ERROR", critical=True), result("polite", "PASS"))
    failing = side("FAILED", result("refunded-once", "FAIL", critical=True), result("polite", "PASS"))
    broken = compare(unverified, failing).expectations[0]
    assert broken["change"] == "broken" and broken["baseline_unverified"] is True
    passing = side("FAILED", result("refunded-once", "PASS", critical=True), result("polite", "PASS"))
    fixed = compare(unverified, passing)
    assert fixed.expectations[0]["change"] == "not_comparable" and fixed.classification == "UNCHANGED"


def test_expectations_side_by_side() -> None:
    b = side(
        "PASSED",
        result("refunded-once", "PASS", critical=True),
        result("tone", "PASS", type_="semantic", score=0.9),
    )
    c = side(
        "FAILED",
        result(
            "refunded-once",
            "FAIL",
            critical=True,
            reason="refund_count: expected 1, got 2",
            label="STATE_MISMATCH",
        ),
        result("tone", "PASS", type_="semantic", score=0.8),
    )
    rows = compare(b, c).expectations
    assert rows[0] == {
        "id": "refunded-once",
        "type": "state",
        "critical": True,
        "baseline": "PASS",
        "candidate": "FAIL",
        "change": "broken",
        "candidate_reason": "refund_count: expected 1, got 2",
        "label": "STATE_MISMATCH",
    }
    assert rows[1]["baseline_score"] == 0.9 and rows[1]["candidate_score"] == 0.8


def test_judged_semantic_results_replace_the_skipped_ones() -> None:
    skipped = result("tone", "SKIPPED", type_="semantic")
    judged_fail = EvaluationResult.from_json(
        result("tone", "FAIL", type_="semantic", score=0.2, label="SEMANTIC_FAIL")
    )
    judged_error = EvaluationResult.from_json(
        result("tone", "ERROR", type_="semantic", label="EVALUATION_ERROR")
    )
    b = side("PASSED", result("refunded-once", "PASS"), skipped)
    c = side("PASSED", result("refunded-once", "PASS"), skipped, judged=[judged_fail])
    assert c.status == "FAILED" and [r.status for r in c.results] == ["PASS", "FAIL"]
    assert compare(b, c).classification == "REGRESSED"
    # A judge that could not evaluate never passes the case.
    e = side("PASSED", result("refunded-once", "PASS"), skipped, judged=[judged_error])
    assert e.status == "ERRORED" and compare(b, e).classification == "INCOMPLETE"


def refund_step(seq: int, **over: Any) -> dict[str, Any]:
    over.setdefault("risk", "WRITE_IRREVERSIBLE")
    over.setdefault("mutated", True)
    return fake.tool_step(seq, "refund_payment", {"amount": 40, **ORDER}, **over)


def test_metrics_are_compared_with_a_direction() -> None:
    b = side(
        "PASSED",
        result("e1", "PASS"),
        steps=[fake.tool_step(1, "lookup_order", ORDER), refund_step(2)],
        latency_ms=1000.0,
        tokens=1000,
        cost_usd=0.01,
    )
    c = side(
        "FAILED",
        result("e1", "FAIL"),
        steps=[
            fake.tool_step(1, "lookup_order", ORDER),
            refund_step(2, http_status=504, status="timeout", effect_key="refund:ORD-1001"),
            refund_step(3, effect_key="refund:ORD-1001"),
            fake.tool_step(
                4, "admin_export", {}, http_status=403, status="denied", policy_violation="ADMIN_ONLY"
            ),
        ],
        latency_ms=1050.0,  # within the jitter tolerance
        tokens=2000,
    )
    m = compare(b, c).metrics
    assert m["duplicate_side_effects"] == {"baseline": 0, "candidate": 1, "delta": 1.0, "change": "worse"}
    assert m["retries"]["change"] == "worse" and m["retries"]["candidate"] == 1
    assert m["policy_violations"]["change"] == "worse"
    assert m["tool_calls"]["change"] == "changed"  # neither better nor worse
    assert m["latency_ms"]["change"] == "same"
    assert m["tokens"]["change"] == "worse"
    assert m["cost_usd"] == {"baseline": 0.01, "candidate": None, "delta": None, "change": "unknown"}
    assert compare(b, c).tool_selection == {"added": ["admin_export"], "removed": []}


def test_the_comparison_carries_the_divergence_and_the_state_changes() -> None:
    b = side(
        "PASSED",
        result("e1", "PASS"),
        steps=[fake.tool_step(1, "lookup_order", ORDER), fake.tool_step(2, "get_refund_policy", ORDER)],
        final={"orders": {"ORD-1001": {"refund_count": 1}}},
    )
    c = side(
        "FAILED",
        result("e1", "FAIL", critical=True),
        steps=[fake.tool_step(1, "lookup_order", ORDER), refund_step(2)],
        final={"orders": {"ORD-1001": {"refund_count": 2}}},
    )
    j = compare(b, c).to_json()
    assert j["divergence"]["index"] == 2 and j["divergence"]["kind"] == "tool"
    assert j["state_changes"] == [
        {"path": "orders.ORD-1001.refund_count", "change": "changed", "baseline": 1, "candidate": 2}
    ]
    assert [r["match"] for r in j["trajectories"]["alignment"]] == [
        "same",
        "baseline_only",
        "candidate_only",
        "same",
    ]
    assert j["risk"] == "WRITE_IRREVERSIBLE"
    assert j["candidate"]["metrics"]["critical_failures"] == 1
    assert j["baseline"]["metrics"]["critical_failures"] == 0
    assert j["baseline"]["status"] == "PASSED" and j["candidate"]["labels"] == []


def test_a_summary_never_averages_away_a_new_critical_failure() -> None:
    better = [
        compare_case(
            f"s{i}",
            "low",
            ("smoke",),
            side("FAILED", result("e", "FAIL")),
            side("PASSED", result("e", "PASS")),
        )
        for i in range(99)
    ]
    worse = compare_case(
        "refund-timeout-after-mutation",
        "critical",
        ("refunds",),
        side("PASSED", result("refunded-once", "PASS", critical=True)),
        side("FAILED", result("refunded-once", "FAIL", critical=True, label="DUPLICATE_SIDE_EFFECT")),
    )
    s = summarize([*better, worse])
    assert s["counts"] == {
        "NEW_CRITICAL_FAILURE": 1,
        "REGRESSED": 0,
        "IMPROVED": 99,
        "UNCHANGED": 0,
        "INCOMPLETE": 0,
    }
    assert s["new_critical_failures"] == [
        {"scenario_name": "refund-timeout-after-mutation", "expectations": ["refunded-once"]}
    ]
    assert s["slices"]["severity"]["critical"]["NEW_CRITICAL_FAILURE"] == 1
    assert s["slices"]["tag"]["smoke"]["IMPROVED"] == 99
    assert s["slices"]["failure_class"]["DUPLICATE_SIDE_EFFECT"] == {"baseline": 0, "candidate": 1}
    assert s["candidate"]["critical_failures"] == 1 and s["baseline"]["critical_failures"] == 0


def test_unknown_usage_is_never_summed_as_zero() -> None:
    known = compare(
        side("PASSED", result("e", "PASS"), tokens=100), side("PASSED", result("e", "PASS"), tokens=150)
    )
    unknown = compare(side("PASSED", result("e", "PASS")), side("PASSED", result("e", "PASS")))
    s = summarize([known, unknown])
    assert (s["baseline"]["tokens"], s["baseline"]["tokens_known"]) == (100, 1)
    assert (s["candidate"]["cost_usd"], s["candidate"]["cost_known"]) == (None, 0)


STATUSES = st.sampled_from(["PASS", "FAIL", "ERROR", "SKIPPED"])


@st.composite
def pair(draw: st.DrawFn) -> tuple[list[tuple[str, str, bool]], list[str]]:
    n = draw(st.integers(1, 6))
    b = [(f"e{i}", draw(STATUSES), draw(st.booleans())) for i in range(n)]
    return b, [draw(STATUSES) for _ in range(n)]


def _case(rows: list[tuple[str, str, bool]]) -> Side:
    results = [result(eid, status, critical=critical) for eid, status, critical in rows]
    status = "FAILED" if any(s == "FAIL" for _, s, _ in rows) else "PASSED"
    return side(status, *results)


@settings(max_examples=300, deadline=None)
@given(pair())
def test_classification_properties(data: tuple[list[tuple[str, str, bool]], list[str]]) -> None:
    rows, candidate_statuses = data
    b = _case(rows)
    c = _case([(eid, s, critical) for (eid, _, critical), s in zip(rows, candidate_statuses, strict=True)])
    cmp = compare(b, c)
    assert cmp.classification in CLASSIFICATIONS
    new_critical = any(
        crit and cs == "FAIL" and bs != "FAIL"
        for (_, bs, crit), cs in zip(rows, candidate_statuses, strict=True)
    )
    new_failure = any(
        cs == "FAIL" and bs != "FAIL" for (_, bs, _), cs in zip(rows, candidate_statuses, strict=True)
    )
    # A candidate failure the baseline did not have is never hidden.
    assert (cmp.classification == "NEW_CRITICAL_FAILURE") == new_critical
    if new_failure:
        assert cmp.classification in ("NEW_CRITICAL_FAILURE", "REGRESSED")
    if cmp.classification == "IMPROVED":
        assert not new_failure
