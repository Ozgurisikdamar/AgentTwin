"""What a person reviews (spec §16.5), independent of the API: which
expectations of a compared case need a look, and how a review replaces an
evaluator's result when the case is classified again from its snapshots.

A review is a person's verdict on one *expectation*. The simulation's finding
about the run itself (``agentRun``: the agent was unreachable, rejected the
request, timed out…) is not one — it records what happened, and overriding it
would turn an infrastructure failure into a verdict.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from agenttwin_core.evaluators import EvaluationResult
from agenttwin_evaluation.common import pick
from agenttwin_evaluation.comparison import CaseComparison, Side, compare_case

__all__ = [
    "REVIEWER",
    "SIDES",
    "needs_review",
    "rebuild_case",
    "review_json",
    "reviewable",
]

REVIEWER = "human.review"
SEMANTIC = "semantic.judge"
SIDES = {"BASELINE": "baseline", "CANDIDATE": "candidate"}


def review_json(row: Mapping[str, Any]) -> dict[str, Any]:
    return pick(
        row,
        (
            "id",
            "eval_run_id",
            "scenario_name",
            "side",
            "expectation_id",
            "original_status",
            "original_label",
            "status",
            "note",
            "reviewer",
            "created_at",
        ),
    )


def reviewable(expectation: Mapping[str, Any]) -> bool:
    return expectation.get("type") != "agentRun"


def needs_review(results: Sequence[Mapping[str, Any]], verdicts: Sequence[Mapping[str, Any]]) -> list[str]:
    """The expectation ids of one side a person should look at: the judge
    could not grade them (ERROR, SKIPPED), or graded a critical one without
    being calibrated for its criterion."""
    uncalibrated = {
        str(v["expectation_id"]) for v in verdicts if (v.get("judge") or {}).get("calibrated") != "true"
    }
    out = []
    for r in results:
        if r.get("evaluator") != SEMANTIC:
            continue
        rid = str((r.get("expectation") or {}).get("id"))
        if r.get("status") in ("ERROR", "SKIPPED") or (r.get("critical") and rid in uncalibrated):
            out.append(rid)
    return out


def _override(original: EvaluationResult, review: Mapping[str, Any]) -> EvaluationResult:
    passed = review["status"] == "PASS"
    return EvaluationResult(
        status="PASS" if passed else "FAIL",
        reason=f"Reviewed by {review['reviewer']}: {review['note']}",
        evaluator=REVIEWER,
        evaluator_version="1.0.0",
        score=original.score,
        label=None if passed else (original.label or "HUMAN_REVIEW_FAIL"),
        evidence=original.evidence,
        expectation=original.expectation,
    )


def _side(snapshot: Mapping[str, Any], reviews: Sequence[Mapping[str, Any]]) -> Side:
    """One side rebuilt from its stored snapshot, the latest review of each
    expectation (``reviews`` in the order they were made) replacing its
    result."""
    detail = snapshot.get("detail") or {}
    if not detail.get("case"):
        return Side.missing("The case was not run.")
    judged = {
        str(r["expectation"]["id"]): EvaluationResult.from_json(r) for r in snapshot.get("judged") or ()
    }
    raw = {
        str(r["expectation"]["id"]): EvaluationResult.from_json(r)
        for r in detail["case"].get("results") or ()
    }
    for review in reviews:
        key = str(review["expectation_id"])
        original = judged.get(key) or raw.get(key)
        if original is not None and reviewable(original.expectation):
            judged[key] = _override(original, review)
    return Side.from_case_detail(
        detail, judged=list(judged.values()), tokens=snapshot.get("tokens"), cost_usd=snapshot.get("cost_usd")
    )


def rebuild_case(
    row: Mapping[str, Any], reviews: Sequence[Mapping[str, Any]]
) -> tuple[CaseComparison, dict[str, Any]]:
    """A stored case compared again with its reviews applied, and its sides
    with their merged results updated."""
    sides = dict(row["sides"] or {})
    built = {}
    for side, key in SIDES.items():
        mine = [r for r in reviews if r["side"] == side]
        built[key] = _side(sides.get(key) or {}, mine)
        sides[key] = dict(sides.get(key) or {}) | {"results": [r.to_json() for r in built[key].results]}
    comparison = compare_case(
        str(row["scenario_name"]),
        str(row["severity"]),
        list(row["tags"] or ()),
        built["baseline"],
        built["candidate"],
    )
    return comparison, sides
