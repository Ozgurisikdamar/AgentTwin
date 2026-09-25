"""Human review, the review queue and judge calibration, end to end over the
real simulation service; and the contract walk: every operation of the
evaluation service answers as documented at least once (ADR-0021)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from test_eval_runs_integration import AGENT, OUTSIDER, VIEWER, detail, evaluate, register, start

from agenttwin_core.auth import Principal, Role
from agenttwin_core.yamlsafe import load_yaml
from eval_testutil import ASSURANCE, ORG, PROJECT, Stack, evaluation_with_simulation

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

REVIEWER = Principal(org_id=ORG, actor="user:reviewer", role=Role.REVIEWER, project_ids=(PROJECT,))
RUBRIC = "The reply confirms the refund."


def judged_scenario(*, critical: bool) -> str:
    doc = load_yaml((ASSURANCE / "scenarios" / "refund-happy-path.yaml").read_text())
    doc["metadata"]["name"] = "refund-judged"
    doc["spec"]["expectations"] += [
        {
            "id": "confirms",
            "type": "semantic",
            "category": "task_completion",
            "rubric": RUBRIC,
            "critical": critical,
        },
        {"id": "polite", "type": "semantic", "rubric": "The reply thanks the customer."},
    ]
    return json.dumps(doc)


def examples(n: int) -> list[dict[str, Any]]:
    """Labels the deterministic judge agrees with: half confirm the refund."""
    return [
        {
            "id": f"x{i}",
            "rubric": RUBRIC,
            "customer_message": "Can I get a refund of $40?",
            "answer": "We confirm your refund has been issued."
            if i % 2 == 0
            else "Please contact the store.",
            "human_label": "pass" if i % 2 == 0 else "fail",
        }
        for i in range(n)
    ]


async def review(ev: Stack, run_id: str, scenario: str, side: str, eid: str, status: str, **kw: Any) -> Any:
    body = {"side": side, "expectation_id": eid, "status": status, "note": kw.pop("note", "Checked by hand.")}
    return await ev.ok("POST", f"/api/v1/eval-runs/{run_id}/cases/{scenario}/reviews", body, status=201, **kw)


async def test_a_review_replaces_a_verdict_and_classifies_the_case_again() -> None:
    async with evaluation_with_simulation() as (ev, sim):
        run = await start(ev, seed=42)
        done = await evaluate(ev, sim, run["id"])
        assert done["run"]["counts"]["REGRESSED"] == 1
        case = await ev.ok("GET", f"/api/v1/eval-runs/{run['id']}/cases/refund-happy-path")
        [broken] = [e for e in case["comparison"]["expectations"] if e["change"] == "broken"]

        out = await review(
            ev,
            run["id"],
            "refund-happy-path",
            "CANDIDATE",
            broken["id"],
            "PASS",
            note="Checked out of band.",
            as_=REVIEWER,
        )
        assert (out["previous_classification"], out["classification"]) == ("REGRESSED", "UNCHANGED")
        assert out["run"]["counts"]["REGRESSED"] == 0 and out["run"]["counts"]["UNCHANGED"] == 7
        assert (out["review"]["original_status"], out["review"]["reviewer"]) == ("FAIL", "user:reviewer")
        after = await ev.ok("GET", f"/api/v1/eval-runs/{run['id']}/cases/refund-happy-path")
        assert after["reviewed"] is True and [r["status"] for r in after["reviews"]] == ["PASS"]
        [result] = [r for r in after["results"]["candidate"] if r["expectation"]["id"] == broken["id"]]
        assert (result["evaluator"], result["status"]) == ("human.review", "PASS")
        assert "Checked out of band." in result["reason"]
        assert (await detail(ev, run["id"]))["summary"]["regressed"] == []

        # The latest review counts: a second one puts it back.
        again = await review(
            ev, run["id"], "refund-happy-path", "CANDIDATE", broken["id"], "FAIL", as_=REVIEWER
        )
        assert again["classification"] == "REGRESSED" and again["run"]["counts"]["REGRESSED"] == 1
        audits = [e["payload"] for e in await ev.outbox("audit.recorded.v1")]
        overrides = [a for a in audits if a["action"] == "review.override"]
        assert [(o["reason"], o["metadata"]["to"]) for o in overrides] == [
            ("Checked out of band.", "PASS"),
            ("Checked by hand.", "FAIL"),
        ]
        assert overrides[0]["metadata"]["classification_after"] == "UNCHANGED"

        path = f"/api/v1/eval-runs/{run['id']}/cases/refund-happy-path/reviews"
        body = {"side": "CANDIDATE", "expectation_id": "nope", "status": "PASS", "note": "x"}
        await ev.fails("POST", path, body, status=404, code="NOT_FOUND", as_=REVIEWER)
        await ev.fails(
            "POST", path, body | {"expectation_id": broken["id"]}, status=403, code="FORBIDDEN", as_=VIEWER
        )
        await ev.fails("POST", path, body | {"note": ""}, status=400, code="INVALID_REQUEST", as_=REVIEWER)
        await ev.fails("POST", path, body, status=404, code="NOT_FOUND", as_=OUTSIDER)
        queued = await start(ev)
        await ev.fails(
            "POST",
            f"/api/v1/eval-runs/{queued['id']}/cases/refund-happy-path/reviews",
            body,
            status=409,
            code="EVAL_RUN_NOT_COMPLETED",
            as_=REVIEWER,
        )


async def test_the_queue_holds_what_an_uncalibrated_judge_decided_alone() -> None:
    async with evaluation_with_simulation() as (ev, sim):
        await register(sim, judged_scenario(critical=True))
        run = await start(ev, "1.2.4", "1.2.4", scenarios=["refund-judged"])
        await evaluate(ev, sim, run["id"])
        case = await ev.ok("GET", f"/api/v1/eval-runs/{run['id']}/cases/refund-judged")
        assert case["needs_review"] == ["BASELINE:confirms", "CANDIDATE:confirms"]
        queue = await ev.ok("GET", "/api/v1/reviews", project_id=PROJECT)
        [item] = queue["items"]
        assert (item["eval_run_id"], item["scenario_name"], item["agent_name"]) == (
            run["id"],
            "refund-judged",
            AGENT,
        )
        assert [(p["side"], p["expectation_id"], p["critical"]) for p in item["pending"]] == [
            ("BASELINE", "confirms", True),
            ("CANDIDATE", "confirms", True),
        ]
        await review(ev, run["id"], "refund-judged", "BASELINE", "confirms", "PASS", as_=REVIEWER)
        [item] = (await ev.ok("GET", "/api/v1/reviews"))["items"]
        assert [p["side"] for p in item["pending"]] == ["CANDIDATE"]
        await review(ev, run["id"], "refund-judged", "CANDIDATE", "confirms", "PASS", as_=REVIEWER)
        assert (await ev.ok("GET", "/api/v1/reviews"))["items"] == []
        assert (await ev.ok("GET", "/api/v1/reviews", as_=OUTSIDER))["items"] == []
        await ev.fails("GET", "/api/v1/reviews", status=400, code="INVALID_CURSOR", cursor="!!")


async def test_a_calibrated_judge_is_recorded_as_calibrated_for_its_criterion() -> None:
    async with evaluation_with_simulation() as (ev, sim):
        body = {"project_id": PROJECT, "criterion": "task_completion", "examples": examples(20)}
        started = await ev.ok("POST", "/api/v1/judges/calibrations", body, status=202, as_=REVIEWER)
        cal = started["calibration"]
        assert (cal["status"], cal["example_count"], cal["calibrated"]) == ("QUEUED", 20, None)
        assert await ev.worker.calibrate_next() == cal["id"]
        assert await ev.worker.calibrate_next() is None
        done = (await ev.ok("GET", f"/api/v1/judges/calibrations/{cal['id']}"))["calibration"]
        assert (done["status"], done["calibrated"], done["disagreements"]) == ("COMPLETED", True, [])
        assert (done["metrics"]["accuracy"], done["metrics"]["kappa"]) == (1.0, 1.0)
        assert done["judge"]["provider"] == "deterministic-fake"

        few = body | {"criterion": "rubric", "examples": examples(5)}
        small = (await ev.ok("POST", "/api/v1/judges/calibrations", few, status=202))["calibration"]
        await ev.worker.calibrate_next()
        small = (await ev.ok("GET", f"/api/v1/judges/calibrations/{small['id']}"))["calibration"]
        assert small["calibrated"] is False and "at least 20" in small["reason"]

        judge = await ev.ok("GET", "/api/v1/judges", project_id=PROJECT)
        states = {c["criterion"]: c["calibrated"] for c in judge["criteria"]}
        assert states == {
            "task_completion": True,
            "intent_fidelity": False,
            "relevance": False,
            "rubric": False,
        }
        assert judge["requirements"] == {"min_examples": 20, "min_agreement": 0.8, "min_kappa": 0.6}
        listed = await ev.ok("GET", "/api/v1/judges/calibrations", criterion="task_completion")
        assert [c["id"] for c in listed["items"]] == [cal["id"]]

        # The critical expectation of the calibrated criterion needs no review.
        await register(sim, judged_scenario(critical=True))
        run = await start(ev, "1.2.4", "1.2.4", scenarios=["refund-judged"])
        out = await evaluate(ev, sim, run["id"])
        assert out["run"]["judge"]["calibrated"] == "false"  # the rubric criterion is not calibrated
        case = await ev.ok("GET", f"/api/v1/eval-runs/{run['id']}/cases/refund-judged")
        marks = {v["expectation_id"]: v["judge"]["calibrated"] for v in case["verdicts"]["baseline"]}
        assert marks == {"confirms": "true", "polite": "false"}
        assert case["needs_review"] == []

        dupes = body | {"examples": examples(2) + examples(1)}
        await ev.fails("POST", "/api/v1/judges/calibrations", dupes, status=400, code="INVALID_PARAMETER")
        await ev.fails(
            "POST",
            "/api/v1/judges/calibrations",
            body | {"criterion": "x"},
            status=400,
            code="INVALID_PARAMETER",
        )
        await ev.fails("POST", "/api/v1/judges/calibrations", body, status=403, code="FORBIDDEN", as_=VIEWER)
        await ev.fails(
            "GET", f"/api/v1/judges/calibrations/{cal['id']}", status=404, code="NOT_FOUND", as_=OUTSIDER
        )
        await ev.fails("GET", "/api/v1/judges", status=400, code="INVALID_PARAMETER")
        await ev.fails(
            "GET", "/api/v1/judges", status=404, code="NOT_FOUND", as_=OUTSIDER, project_id=PROJECT
        )


async def test_every_operation_answers_as_documented() -> None:
    """The contract walk: one successful exchange of each operation (the
    others' errors are covered by the tests above), then the coverage gate."""
    async with evaluation_with_simulation() as (ev, sim):
        ds = await ev.ok(
            "POST",
            "/api/v1/datasets",
            {"project_id": PROJECT, "name": "walk", "cases": [{"scenario": "refund-happy-path"}]},
            status=201,
        )
        dataset = ds["dataset"]["id"]
        await ev.ok("GET", "/api/v1/datasets")
        await ev.ok("GET", f"/api/v1/datasets/{dataset}")
        await ev.ok(
            "POST",
            f"/api/v1/datasets/{dataset}/cases",
            {"cases": [{"scenario": "refund-over-limit"}]},
            status=201,
        )
        await ev.ok("DELETE", f"/api/v1/datasets/{dataset}/cases/refund-over-limit")
        run = await start(ev, dataset_id=dataset, seed=42)
        await evaluate(ev, sim, run["id"])
        await ev.ok("GET", "/api/v1/eval-runs")
        await ev.ok("GET", f"/api/v1/eval-runs/{run['id']}/cases/refund-happy-path")
        await ev.ok("POST", f"/api/v1/eval-runs/{run['id']}/cancel")
        await review(
            ev, run["id"], "refund-happy-path", "CANDIDATE", "policy-before-refund", "PASS", as_=REVIEWER
        )
        await ev.ok("GET", "/api/v1/reviews")
        cal = await ev.ok(
            "POST",
            "/api/v1/judges/calibrations",
            {"project_id": PROJECT, "criterion": "relevance", "examples": examples(1)},
            status=202,
        )
        await ev.ok("GET", "/api/v1/judges/calibrations")
        await ev.ok("GET", f"/api/v1/judges/calibrations/{cal['calibration']['id']}")
        await ev.ok("GET", "/api/v1/judges", project_id=PROJECT)
        await ev.ok("POST", f"/api/v1/datasets/{dataset}/archive")
        assert ev.contract.uncovered() == []
