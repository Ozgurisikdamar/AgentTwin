"""Human review, the review queue and judge calibration, end to end over the
real simulation service; and the contract walk: every operation of the
evaluation service answers as documented at least once (ADR-0021)."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from test_eval_runs_integration import AGENT, OUTSIDER, VIEWER, detail, evaluate, register, start

from agenttwin_core.auth import Principal, Role
from agenttwin_core.yamlsafe import load_yaml
from agenttwin_evaluation.calibrations import CRITERION_NAMES
from agenttwin_evaluation.judges import FakeJudge, JudgeRequest, JudgeVerdict
from eval_testutil import ASSURANCE, ORG, PROJECT, Stack, evaluation_stack, evaluation_with_simulation

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

REVIEWER = Principal(org_id=ORG, actor="user:reviewer", role=Role.REVIEWER, project_ids=(PROJECT,))
RUBRIC = "The reply confirms the refund."


def judged_scenario(*, critical: bool, name: str = "refund-judged") -> str:
    doc = load_yaml((ASSURANCE / "scenarios" / "refund-happy-path.yaml").read_text())
    doc["metadata"]["name"] = name
    doc["spec"]["expectations"] = [e for e in doc["spec"]["expectations"] if e["type"] != "semantic"]
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
        first = await start(ev, "1.2.4", "1.2.4", scenarios=["refund-judged"])
        await evaluate(ev, sim, first["id"])
        case = await ev.ok("GET", f"/api/v1/eval-runs/{first['id']}/cases/refund-judged")
        # Only the critical expectation the uncalibrated judge graded alone;
        # the other one was graded and is not critical.
        assert case["needs_review"] == ["BASELINE:confirms", "CANDIDATE:confirms"]
        second = await start(ev, "1.2.4", "1.2.4", scenarios=["refund-judged"])
        await evaluate(ev, sim, second["id"])

        # Newest run first, one case per page.
        page = await ev.ok("GET", "/api/v1/reviews", project_id=PROJECT, limit=1)
        assert [i["eval_run_id"] for i in page["items"]] == [second["id"]] and page["next_cursor"]
        rest = await ev.ok("GET", "/api/v1/reviews", limit=1, cursor=page["next_cursor"])
        [item] = rest["items"]
        assert rest["next_cursor"] is None
        assert (item["eval_run_id"], item["scenario_name"], item["agent_name"]) == (
            first["id"],
            "refund-judged",
            AGENT,
        )
        assert [(p["side"], p["expectation_id"], p["critical"]) for p in item["pending"]] == [
            ("BASELINE", "confirms", True),
            ("CANDIDATE", "confirms", True),
        ]

        # A review takes its expectation off the queue; the case leaves it
        # when nothing of it is left.
        await review(ev, first["id"], "refund-judged", "BASELINE", "confirms", "PASS", as_=REVIEWER)
        items = (await ev.ok("GET", "/api/v1/reviews"))["items"]
        pending = {i["eval_run_id"]: [p["side"] for p in i["pending"]] for i in items}
        assert pending == {second["id"]: ["BASELINE", "CANDIDATE"], first["id"]: ["CANDIDATE"]}
        await review(ev, first["id"], "refund-judged", "CANDIDATE", "confirms", "PASS", as_=REVIEWER)
        assert [i["eval_run_id"] for i in (await ev.ok("GET", "/api/v1/reviews"))["items"]] == [second["id"]]
        assert (await ev.ok("GET", "/api/v1/reviews", as_=OUTSIDER))["items"] == []
        await ev.fails("GET", "/api/v1/reviews", status=400, code="INVALID_CURSOR", cursor="!!")


async def test_a_review_classifies_its_own_case_only() -> None:
    async with evaluation_with_simulation() as (ev, sim):
        # Two cases with the same expectation ids.
        await register(sim, judged_scenario(critical=True))
        await register(sim, judged_scenario(critical=True, name="refund-judged-again"))
        run = await start(ev, "1.2.4", "1.2.4", scenarios=["refund-judged", "refund-judged-again"])
        out = await evaluate(ev, sim, run["id"])
        # Judged, the critical semantic expectation the simulation skipped no
        # longer leaves the cases ERRORED: they are compared (both sides fail
        # the same non-critical rubric, so nothing changed).
        assert out["run"]["counts"]["UNCHANGED"] == 2
        case = await ev.ok("GET", f"/api/v1/eval-runs/{run['id']}/cases/refund-judged")
        assert [case["comparison"][s]["status"] for s in ("baseline", "candidate")] == ["FAILED", "FAILED"]

        failed = await review(ev, run["id"], "refund-judged", "CANDIDATE", "confirms", "FAIL", as_=REVIEWER)
        assert failed["classification"] == "NEW_CRITICAL_FAILURE"
        counts = failed["run"]["counts"]
        assert (counts["NEW_CRITICAL_FAILURE"], counts["UNCHANGED"]) == (1, 1)
        other = await ev.ok("GET", f"/api/v1/eval-runs/{run['id']}/cases/refund-judged-again")
        assert (other["comparison"]["classification"], other["reviewed"]) == ("UNCHANGED", False)


async def test_a_side_whose_agent_did_not_run_is_neither_judged_nor_reviewable(tmp_path: Path) -> None:
    # The agent deployment can run 1.2.4 only: every candidate case is blocked.
    manifests = ASSURANCE.parent / "manifests"
    shutil.copy(manifests / "1.2.4.yaml", tmp_path / "1.2.4.yaml")
    async with evaluation_with_simulation(simulation={"manifest_dir": tmp_path}) as (ev, sim):
        await register(sim, judged_scenario(critical=True))
        run = await start(ev, "1.2.4", "1.3.0", scenarios=["refund-judged"])
        out = await evaluate(ev, sim, run["id"])
        assert out["run"]["counts"]["INCOMPLETE"] == 1
        # Only the side that ran was judged (two expectations, no cache hit).
        assert out["run"]["budget"]["calls"] == 2
        case = await ev.ok("GET", f"/api/v1/eval-runs/{run['id']}/cases/refund-judged")
        assert [v["expectation_id"] for v in case["verdicts"]["baseline"]] == ["confirms", "polite"]
        assert case["verdicts"]["candidate"] == []
        candidate = {r["expectation"]["id"]: r for r in case["results"]["candidate"]}
        assert candidate["agent-run"]["status"] == "ERROR"
        assert {candidate[e]["status"] for e in ("confirms", "polite")} == {"SKIPPED"}
        assert case["needs_review"] == ["BASELINE:confirms"]
        await ev.fails(
            "POST",
            f"/api/v1/eval-runs/{run['id']}/cases/refund-judged/reviews",
            {"side": "CANDIDATE", "expectation_id": "agent-run", "status": "PASS", "note": "x"},
            status=409,
            code="EXPECTATION_NOT_REVIEWABLE",
            as_=REVIEWER,
        )
        # A person may grade a skipped expectation, but the run it belongs to
        # still did not happen: the case stays incomplete.
        out = await review(ev, run["id"], "refund-judged", "CANDIDATE", "confirms", "PASS", as_=REVIEWER)
        assert out["classification"] == "INCOMPLETE"


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
        assert [c["criterion"] for c in judge["criteria"]] == list(CRITERION_NAMES)  # each once
        states = {c["criterion"]: c["calibrated"] for c in judge["criteria"]}
        assert states == dict.fromkeys(CRITERION_NAMES, False) | {"task_completion": True}
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

        # The latest completed calibration of this judge counts: a newer one
        # that falls short undoes it…
        redo = body | {"examples": examples(5)}
        newer = (await ev.ok("POST", "/api/v1/judges/calibrations", redo, status=202))["calibration"]
        assert await ev.worker.calibrate_next() == newer["id"]
        assert await is_calibrated(ev) is False
        # … unless it measured another judge (another model): it says nothing
        # about this one.
        await ev.store.one(
            """UPDATE judge_calibration SET judge = jsonb_set(judge, '{model}', '"another-model"')
               WHERE id = %s RETURNING id""",
            (newer["id"],),
        )
        assert await is_calibrated(ev) is True


async def is_calibrated(ev: Stack, criterion: str = "task_completion") -> bool:
    judge = await ev.ok("GET", "/api/v1/judges", project_id=PROJECT)
    return bool(next(c["calibrated"] for c in judge["criteria"] if c["criterion"] == criterion))


class SlowJudge(FakeJudge):
    async def judge(self, request: JudgeRequest) -> JudgeVerdict:
        await asyncio.sleep(0.05)
        return await super().judge(request)


async def test_a_calibration_keeps_its_lease_and_is_retried_then_failed_when_its_worker_is_lost() -> None:
    body = {"project_id": PROJECT, "criterion": "task_completion", "examples": examples(20)}
    async with evaluation_stack(judge=SlowJudge(), lease_seconds=0.3) as ev:
        cal = (await ev.ok("POST", "/api/v1/judges/calibrations", body, status=202))["calibration"]
        # A worker takes it and is lost; its lease expires and another worker
        # takes it over. The lost worker's result is refused.
        lost = await ev.store.claim_calibration("lost-worker", 60, 3)
        assert lost is not None and (lost["id"], lost["attempts"]) == (cal["id"], 1)
        assert await ev.store.claim_calibration("other-worker", 60, 3) is None  # still leased
        await expire(ev, cal["id"])
        taken = await ev.store.claim_calibration("other-worker", 60, 3)
        assert taken is not None and taken["attempts"] == 2
        assert await ev.store.finish_calibration(cal["id"], "lost-worker", {"status": "COMPLETED"}) is None
        await expire(ev, cal["id"])

        # The third attempt takes longer than the lease: the worker keeps it.
        running = asyncio.create_task(ev.worker.calibrate_next())
        await asyncio.sleep(0.6)
        row = await ev.store.one(
            """SELECT status, lease_owner, lease_expires_at > now() AS held
               FROM judge_calibration WHERE id = %s""",
            (cal["id"],),
        )
        assert row == {"status": "RUNNING", "lease_owner": ev.worker.owner, "held": True}
        assert await running == cal["id"]
        done = (await ev.ok("GET", f"/api/v1/judges/calibrations/{cal['id']}"))["calibration"]
        assert (done["status"], done["calibrated"]) == ("COMPLETED", True)

        # One whose worker lost the lease on every attempt fails with the reason.
        gone = (await ev.ok("POST", "/api/v1/judges/calibrations", body, status=202))["calibration"]
        for owner in ("w1", "w2", "w3"):
            assert (await ev.store.claim_calibration(owner, 60, 3) or {}).get("id") == gone["id"]
            await expire(ev, gone["id"])
        assert await ev.worker.calibrate_next() is None
        failed = (await ev.ok("GET", f"/api/v1/judges/calibrations/{gone['id']}"))["calibration"]
        assert failed["status"] == "FAILED"
        assert failed["error"] == "Gave up after 3 attempts (the worker lost its lease each time)."


async def expire(ev: Stack, calibration_id: str) -> None:
    await ev.store.one(
        """UPDATE judge_calibration SET lease_expires_at = now() - interval '1 second'
           WHERE id = %s RETURNING id""",
        (calibration_id,),
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
