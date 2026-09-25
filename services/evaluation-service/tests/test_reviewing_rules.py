"""The rules of review and judging, without a database: which results a
person should look at, which a review may replace, and which cases the judge
grades."""

from __future__ import annotations

from typing import Any

from agenttwin_evaluation.reviewing import needs_review, rebuild_case
from agenttwin_evaluation.worker import judgeable


def result(eid: str, status: str, *, evaluator: str = "state", critical: bool = False) -> dict[str, Any]:
    return {
        "status": status,
        "reason": f"{eid} {status}",
        "evaluator": evaluator,
        "evaluator_version": "1.0.0",
        "score": None,
        "label": None,
        "critical": critical,
        "expectation": {
            "id": eid,
            "type": "agentRun" if eid == "agent-run" else "state",
            "critical": critical,
        },
        "evidence": [],
    }


def snapshot(status: str, results: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "detail": {"case": {"id": f"case-{status}", "status": status, "results": results}, "steps": []},
        "judged": [],
        "verdicts": [],
        "results": results,
        "tokens": None,
        "cost_usd": None,
    }


def review(side: str, eid: str, status: str) -> dict[str, Any]:
    return {"side": side, "expectation_id": eid, "status": status, "note": "checked", "reviewer": "user:r"}


def test_a_stored_review_of_the_run_finding_is_not_applied() -> None:
    # The candidate's agent could not be run: its expectations were skipped.
    blocked = [
        result("agent-run", "ERROR", evaluator="simulation.agent_run", critical=True),
        result("refunded", "SKIPPED", critical=True),
    ]
    row = {
        "scenario_name": "refund-happy-path",
        "severity": "critical",
        "tags": [],
        "sides": {
            "baseline": snapshot("PASSED", [result("refunded", "PASS", critical=True)]),
            "candidate": snapshot("ERRORED", blocked),
        },
    }
    comparison, sides = rebuild_case(
        row, [review("CANDIDATE", "agent-run", "PASS"), review("CANDIDATE", "refunded", "PASS")]
    )
    by_id = {r["expectation"]["id"]: r for r in sides["candidate"]["results"]}
    # The expectation is overridden; the finding about the run is not.
    assert (by_id["refunded"]["evaluator"], by_id["refunded"]["status"]) == ("human.review", "PASS")
    assert (by_id["agent-run"]["evaluator"], by_id["agent-run"]["status"]) == (
        "simulation.agent_run",
        "ERROR",
    )
    assert comparison.classification == "INCOMPLETE"


def test_a_review_decides_a_case_the_judge_could_not() -> None:
    judge_error = result("confirms", "ERROR", evaluator="semantic.judge", critical=True)
    row = {
        "scenario_name": "refund-judged",
        "severity": "critical",
        "tags": [],
        "sides": {
            "baseline": snapshot("PASSED", [result("confirms", "PASS", critical=True)]),
            "candidate": snapshot("ERRORED", [result("refunded", "PASS"), judge_error]),
        },
    }
    unreviewed, _ = rebuild_case(row, [])
    assert unreviewed.classification == "INCOMPLETE"
    passed, _ = rebuild_case(row, [review("CANDIDATE", "confirms", "PASS")])
    assert passed.classification == "UNCHANGED"
    failed, _ = rebuild_case(row, [review("CANDIDATE", "confirms", "FAIL")])
    assert failed.classification == "NEW_CRITICAL_FAILURE"


def test_what_needs_review() -> None:
    results = [
        result("could-not-judge", "ERROR", evaluator="semantic.judge"),
        result("over-budget", "SKIPPED", evaluator="semantic.judge"),
        result("critical-uncalibrated", "PASS", evaluator="semantic.judge", critical=True),
        result("critical-calibrated", "FAIL", evaluator="semantic.judge", critical=True),
        result("minor-uncalibrated", "FAIL", evaluator="semantic.judge"),
        result("deterministic", "FAIL", critical=True),
    ]
    verdicts = [
        {"expectation_id": "critical-uncalibrated", "judge": {"calibrated": "false"}},
        {"expectation_id": "critical-calibrated", "judge": {"calibrated": "true"}},
        {"expectation_id": "minor-uncalibrated", "judge": {"calibrated": "false"}},
    ]
    assert needs_review(results, verdicts) == ["could-not-judge", "over-budget", "critical-uncalibrated"]


def test_which_cases_are_judged() -> None:
    ran = [result("refunded", "PASS")]
    assert judgeable({"status": "PASSED", "results": ran})
    assert judgeable({"status": "FAILED", "results": ran})
    # A critical semantic expectation the simulation skipped makes it ERRORED.
    assert judgeable({"status": "ERRORED", "results": [*ran, result("confirms", "SKIPPED", critical=True)]})
    # A timed-out agent ran: its expectations were evaluated (it has no reply).
    timeout = result("agent-run", "FAIL", evaluator="simulation.agent_run", critical=True)
    assert judgeable({"status": "FAILED", "results": [timeout, *ran]})
    unreachable = result("agent-run", "ERROR", evaluator="simulation.agent_run", critical=True)
    assert not judgeable({"status": "ERRORED", "results": [unreachable]})
    for status in ("CANCELLED", "PENDING", "RUNNING", None):
        assert not judgeable({"status": status, "results": ran})
