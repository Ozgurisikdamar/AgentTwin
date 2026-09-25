"""Evaluation runs end to end: the evaluation service over the real
simulation service and the real demo agent, on PostgreSQL, every exchange
checked against the evaluation, simulation and trace contracts."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from agenttwin_core.auth import Principal, Role
from agenttwin_core.events import validate_envelope
from agenttwin_core.jobs import JobStatus
from agenttwin_core.yamlsafe import load_yaml
from agenttwin_evaluation.worker import EvalWorker
from eval_testutil import (
    ASSURANCE,
    ORG,
    OTHER_PROJECT,
    PROJECT,
    Stack,
    evaluation_stack,
    evaluation_with_simulation,
    run_simulations,
)
from sim_testutil import Stack as SimStack

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

AGENT = "support-refund-agent"
VIEWER = Principal(org_id=ORG, actor="user:viewer", role=Role.VIEWER, project_ids=(PROJECT,))
OUTSIDER = Principal(org_id=ORG, actor="user:outsider", role=Role.ENGINEER, project_ids=(OTHER_PROJECT,))
BOTH = Principal(org_id=ORG, actor="user:both", role=Role.ENGINEER, project_ids=(PROJECT, OTHER_PROJECT))


async def start(ev: Stack, baseline: str = "1.2.4", candidate: str = "1.3.0", **extra: Any) -> dict[str, Any]:
    body = {
        "project_id": PROJECT,
        "agent": AGENT,
        "baseline_version": baseline,
        "candidate_version": candidate,
    }
    out: dict[str, Any] = await ev.ok("POST", "/api/v1/eval-runs", body | extra, status=202)
    return out["run"]


async def detail(ev: Stack, run_id: str) -> dict[str, Any]:
    out: dict[str, Any] = await ev.ok("GET", f"/api/v1/eval-runs/{run_id}")
    return out


async def test_the_candidate_is_compared_with_its_baseline_case_by_case() -> None:
    async with evaluation_with_simulation() as (ev, sim):
        run = await start(ev, seed=42)
        assert (run["status"], run["seed"], run["requested_by"], run["pinning"]) == (
            "QUEUED",
            42,
            "user:engineer",
            None,
        )
        # The queue the worker claims from is what the metrics report.
        assert await ev.store.active_run_counts() == {"QUEUED": 1}
        assert run["selection"] == {
            "scenarios": None,
            "tags": None,
            "dataset": None,
            "scenario_versions": None,
        }
        assert run["release_evaluation_id"] is None

        # PREPARING: the pair of runs, one pinned suite, attributed to the requester.
        assert await ev.worker.process_next() == run["id"]
        running = (await detail(ev, run["id"]))["run"]
        assert running["status"] == "RUNNING" and running["case_count"] == 9
        assert running["pinning"]["seed"] == 42 and len(running["pinning"]["cases"]) == 9
        sims = {running["baseline_run_id"], running["candidate_run_id"]}
        assert await ev.worker.process_next() is None

        # Nothing to evaluate while the simulations run.
        await ev.worker.check_waiting()
        assert (await detail(ev, run["id"]))["run"]["status"] == "RUNNING"
        # One simulation done is not enough.
        first = await sim.worker.process_next()
        assert first in sims
        await ev.worker.check_waiting()
        assert (await detail(ev, run["id"]))["run"]["status"] == "RUNNING"
        assert set(await run_simulations(sim)) == sims - {first}
        for sim_id in sims:
            sim_run = await sim.ok("GET", f"/api/v1/simulations/{sim_id}")
            assert (
                sim_run["run"]["requested_by"] == "user:engineer"
                and sim_run["run"]["eval_run_id"] == run["id"]
            )

        await ev.worker.check_waiting()
        out = await detail(ev, run["id"])
        final = out["run"]
        assert final["status"] == "COMPLETED", final["error"]
        assert final["counts"] == {
            "NEW_CRITICAL_FAILURE": 2,
            "REGRESSED": 1,
            "IMPROVED": 0,
            "UNCHANGED": 6,
            "INCOMPLETE": 0,
        }
        # Counted once, after the commit, with its duration and compared cases.
        assert ev.metric("agenttwin_eval_runs_total", status="COMPLETED") == 1
        assert ev.metric("agenttwin_eval_run_duration_seconds_count", status="COMPLETED") == 1
        assert ev.metric("agenttwin_eval_cases_total", classification="NEW_CRITICAL_FAILURE") == 2
        assert ev.metric("agenttwin_eval_cases_total", classification="UNCHANGED") == 6
        assert await ev.store.active_run_counts() == {}
        classes = {c["scenario_name"]: c["classification"] for c in out["cases"]}
        assert classes["refund-timeout-after-mutation"] == "NEW_CRITICAL_FAILURE"
        assert classes["refund-tool-success-lie"] == "NEW_CRITICAL_FAILURE"
        assert classes["refund-happy-path"] == "REGRESSED"
        assert [c["position"] for c in out["cases"]] == list(range(9))
        summary = out["summary"]
        assert sorted(n["scenario_name"] for n in summary["new_critical_failures"]) == [
            "refund-timeout-after-mutation",
            "refund-tool-success-lie",
        ]
        assert summary["regressed"] == ["refund-happy-path"]
        # Usage comes from the traces; one trace was not settled, and an
        # unknown is never added as zero.
        assert summary["baseline"]["tokens_known"] == 8 and summary["candidate"]["tokens_known"] == 8
        assert [t["to_status"] for t in out["transitions"]] == [
            "QUEUED",
            "PREPARING",
            "RUNNING",
            "EVALUATING",
            "COMPLETED",
        ]
        # The demo's three semantic expectations, graded on both sides.
        assert final["judge"]["provider"] == "deterministic-fake" and final["budget"]["calls"] == 6
        semantic = {}
        for name in ("refund-happy-path", "refund-over-limit", "refund-tool-success-lie"):
            graded = await ev.ok("GET", f"/api/v1/eval-runs/{run['id']}/cases/{name}")
            [e] = [e for e in graded["comparison"]["expectations"] if e["type"] == "semantic"]
            semantic[e["id"]] = (e["baseline"], e["candidate"], e["change"])
        # The judge sees the candidate claim a refund the order does not show.
        assert semantic == {
            "reply-confirms-refund": ("PASS", "PASS", "same"),
            "reply-explains-escalation": ("PASS", "PASS", "same"),
            "reply-admits-unconfirmed-refund": ("PASS", "FAIL", "broken"),
        }

        case = await ev.ok("GET", f"/api/v1/eval-runs/{run['id']}/cases/refund-timeout-after-mutation")
        comparison = case["comparison"]
        # Each side has its own simulation's usage (the fake reports the
        # candidate's at twice the baseline's).
        tokens = comparison["metrics"]["tokens"]
        assert tokens["baseline"] and tokens["candidate"] == 2 * tokens["baseline"]
        assert (
            comparison["metrics"]["cost_usd"]["candidate"]
            == 2 * comparison["metrics"]["cost_usd"]["baseline"]
        )
        assert comparison["classification"] == "NEW_CRITICAL_FAILURE"
        assert (
            comparison["divergence"]["index"] == 2 and "refund_payment" in comparison["divergence"]["summary"]
        )
        assert case["results"]["candidate"] and case["verdicts"] == {"baseline": [], "candidate": []}

        [event] = await ev.outbox("evaluation.run_completed.v1")
        env = validate_envelope(event)
        assert env.payload == {
            "eval_run_id": run["id"],
            "release_evaluation_id": None,
            "status": "COMPLETED",
            "error": None,
        }
        assert (env.organization_id, env.project_id, env.correlation_id) == (ORG, PROJECT, run["id"])
        [started] = [e["payload"] for e in await ev.outbox("audit.recorded.v1")]
        assert (started["action"], started["resource_id"]) == ("eval_run.start", run["id"])


def semantic_scenario(name: str = "refund-judged") -> str:
    """The happy path with two semantic expectations, graded by the judge."""
    doc = load_yaml((ASSURANCE / "scenarios" / "refund-happy-path.yaml").read_text())
    doc["metadata"]["name"] = name
    # Only the semantic expectations this test asks for (not the demo's).
    doc["spec"]["expectations"] = [e for e in doc["spec"]["expectations"] if e["type"] != "semantic"]
    doc["spec"]["expectations"] += [
        {
            "id": "reply-confirms-refund",
            "type": "semantic",
            "category": "task_completion",
            "rubric": "The reply confirms the refund of $40 for ORD-1001.",
            "threshold": 0.5,
        },
        {"type": "semantic", "rubric": "The reply apologizes for the broken item.", "critical": False},
    ]
    return json.dumps(doc)


async def register(sim: SimStack, yaml_or_json: str) -> None:
    await sim.ok("POST", "/api/v1/scenarios", {"project_id": PROJECT, "yaml": yaml_or_json}, status=201)


async def evaluate(ev: Stack, sim: SimStack, run_id: str) -> dict[str, Any]:
    assert await ev.worker.process_next() == run_id
    await run_simulations(sim)
    await ev.worker.check_waiting()
    return await detail(ev, run_id)


async def test_semantic_expectations_are_judged_once_and_reused() -> None:
    async with evaluation_with_simulation() as (ev, sim):
        await register(sim, semantic_scenario())
        run = await start(ev, "1.2.4", "1.2.4", scenarios=["refund-judged"], seed=7)
        out = await evaluate(ev, sim, run["id"])
        assert out["run"]["status"] == "COMPLETED" and out["run"]["case_count"] == 1
        budget = out["run"]["budget"]
        # Both sides gave the same answer: the second verdict came from the cache.
        assert (budget["calls"], budget["unknown_cost_calls"], budget["exhausted"]) == (2, 0, False)
        case = await ev.ok("GET", f"/api/v1/eval-runs/{run['id']}/cases/refund-judged")
        for side in ("baseline", "candidate"):
            judged = [r for r in case["results"][side] if r["evaluator"] == "semantic.judge"]
            assert [r["expectation"]["id"] for r in judged] == ["reply-confirms-refund", "e9"]
            assert all(r["status"] in ("PASS", "FAIL") and "threshold" in r["reason"] for r in judged)
            assert [v["expectation_id"] for v in case["verdicts"][side]] == ["reply-confirms-refund", "e9"]
            assert all(v["verdict"] is not None for v in case["verdicts"][side])
        assert [v["cached"] for v in case["verdicts"]["baseline"]] == [False, False]
        assert [v["cached"] for v in case["verdicts"]["candidate"]] == [True, True]
        semantic = [e for e in case["comparison"]["expectations"] if e["type"] == "semantic"]
        assert [e["change"] for e in semantic] in (["same", "same"], ["same", "still_failing"])

        # A second evaluation of the same answers costs no judge call.
        again = await start(ev, "1.2.4", "1.2.4", scenarios=["refund-judged"], seed=7)
        repeat = await evaluate(ev, sim, again["id"])
        assert repeat["run"]["budget"]["calls"] == 0
        second = await ev.ok("GET", f"/api/v1/eval-runs/{again['id']}/cases/refund-judged")
        assert all(v["cached"] for side in ("baseline", "candidate") for v in second["verdicts"][side])
        assert second["comparison"]["expectations"] == case["comparison"]["expectations"]
        # Two fresh verdicts, then six served from the cache (2 + 4 over the two runs).
        calls = "agenttwin_judge_calls_total"
        assert ev.metric(calls, provider="deterministic-fake", outcome="verdict") == 2
        assert ev.metric(calls, provider="deterministic-fake", outcome="cached") == 6


async def test_a_spent_judge_budget_leaves_expectations_unjudged_and_says_so() -> None:
    async with evaluation_with_simulation(judge_max_calls=1) as (ev, sim):
        await register(sim, semantic_scenario())
        run = await start(ev, "1.2.4", "1.3.0", scenarios=["refund-judged"], seed=7)
        out = await evaluate(ev, sim, run["id"])
        assert out["run"]["budget"]["exhausted"] is True and out["run"]["budget"]["calls"] == 1
        case = await ev.ok("GET", f"/api/v1/eval-runs/{run['id']}/cases/refund-judged")
        results = [r for side in ("baseline", "candidate") for r in case["results"][side]]
        skipped = [r for r in results if r["evaluator"] == "semantic.judge" and r["status"] == "SKIPPED"]
        assert len(skipped) == 3 and all("budget is spent" in r["reason"] for r in skipped)


async def test_refused_simulations_fail_the_run_with_the_reason() -> None:
    async with evaluation_with_simulation() as (ev, sim):
        run = await start(ev, "1.2.4", "9.9.9")
        assert await ev.worker.process_next() == run["id"]
        out = await detail(ev, run["id"])
        assert out["run"]["status"] == "FAILED" and out["run"]["baseline_run_id"] is None
        assert "The simulations could not start" in out["run"]["error"] and "candidate" in out["run"]["error"]
        assert ev.metric("agenttwin_eval_runs_total", status="FAILED") == 1
        [event] = await ev.outbox("evaluation.run_completed.v1")
        assert event["payload"]["status"] == "FAILED" and event["payload"]["error"] == out["run"]["error"]
        assert await run_simulations(sim) == []


async def test_cancellation_stops_the_run_and_its_simulations() -> None:
    async with evaluation_with_simulation() as (ev, sim):
        queued = await start(ev)
        out = await ev.ok("POST", f"/api/v1/eval-runs/{queued['id']}/cancel")
        assert (out["run"]["status"], out["run"]["cancel_requested"]) == ("CANCELLED", True)
        assert await ev.worker.process_next() is None

        running = await start(ev)
        assert await ev.worker.process_next() == running["id"]
        out = await ev.ok("POST", f"/api/v1/eval-runs/{running['id']}/cancel", status=202)
        assert (out["run"]["status"], out["run"]["cancel_requested"]) == ("RUNNING", True)
        await ev.ok("POST", f"/api/v1/eval-runs/{running['id']}/cancel", status=202)
        await ev.worker.check_waiting()
        stopped = (await detail(ev, running["id"]))["run"]
        assert stopped["status"] == "CANCELLED"
        for sim_id in (stopped["baseline_run_id"], stopped["candidate_run_id"]):
            assert (await sim.ok("GET", f"/api/v1/simulations/{sim_id}"))["run"]["status"] == "CANCELLED"
        assert (await ev.ok("POST", f"/api/v1/eval-runs/{running['id']}/cancel"))["run"][
            "status"
        ] == "CANCELLED"

        events = [e["payload"] for e in await ev.outbox("evaluation.run_completed.v1")]
        assert [(e["eval_run_id"], e["status"]) for e in events] == [
            (queued["id"], "CANCELLED"),
            (running["id"], "CANCELLED"),
        ]
        cancels = [
            e["payload"] for e in await ev.outbox("audit.recorded.v1") if "cancel" in e["payload"]["action"]
        ]
        assert [c["resource_id"] for c in cancels] == [queued["id"], running["id"]]
        await ev.fails(
            "POST", f"/api/v1/eval-runs/{running['id']}/cancel", status=403, code="FORBIDDEN", as_=VIEWER
        )


async def test_simulations_that_never_end_fail_the_run() -> None:
    async with evaluation_with_simulation(max_wait_s=0.0) as (ev, sim):
        run = await start(ev)
        assert await ev.worker.process_next() == run["id"]
        await ev.worker.check_waiting()
        out = (await detail(ev, run["id"]))["run"]
        assert out["status"] == "FAILED" and "did not finish within" in out["error"]
        for sim_id in (out["baseline_run_id"], out["candidate_run_id"]):
            assert (await sim.ok("GET", f"/api/v1/simulations/{sim_id}"))["run"]["status"] == "CANCELLED"


async def test_a_simulation_cancelled_elsewhere_fails_the_evaluation() -> None:
    async with evaluation_with_simulation() as (ev, sim):
        run = await start(ev)
        assert await ev.worker.process_next() == run["id"]
        candidate = (await detail(ev, run["id"]))["run"]["candidate_run_id"]
        await sim.ok("POST", f"/api/v1/simulations/{candidate}/cancel")
        await run_simulations(sim)
        await ev.worker.check_waiting()
        out = (await detail(ev, run["id"]))["run"]
        assert (out["status"], out["error"]) == ("FAILED", "The candidate simulation was cancelled.")


async def test_a_completion_event_makes_the_waiting_run_due() -> None:
    async with evaluation_with_simulation(poll_interval_s=3600.0) as (ev, sim):
        run = await start(ev)
        assert await ev.worker.process_next() == run["id"]
        await run_simulations(sim)
        await ev.store.all("UPDATE eval_run SET next_check_at = now() + interval '1 hour' RETURNING id")
        assert await ev.worker.check_waiting() == []
        baseline = (await detail(ev, run["id"]))["run"]["baseline_run_id"]
        [event] = [
            e for e in await sim.outbox("simulation.run_completed.v1") if e["payload"]["run_id"] == baseline
        ]
        # A producer that does not name the evaluation run is enough: the
        # baseline's run id finds it.
        stripped = event | {"payload": {k: v for k, v in event["payload"].items() if k != "eval_run_id"}}
        await ev.worker.on_event(validate_envelope(stripped))
        assert await ev.worker.check_waiting() == [run["id"]]
        assert (await detail(ev, run["id"]))["run"]["status"] == "COMPLETED"


async def test_runs_whose_worker_is_lost_are_retried_then_failed() -> None:
    async with evaluation_stack(simulation_url="http://127.0.0.1:9", max_run_attempts=2) as ev:
        run = await start(ev)
        for attempt in (1, 2):
            assert await ev.worker.process_next() == run["id"]
            out = (await detail(ev, run["id"]))["run"]
            # The simulation service could not be reached: the run keeps its lease…
            assert (out["status"], out["attempts"]) == ("PREPARING", attempt)
            assert await ev.worker.recover_expired() == []
            # … until it expires, and goes back to the queue (at most twice).
            await ev.store.all(
                "UPDATE eval_run SET lease_expires_at = now() - interval '1 second' RETURNING id"
            )
            expected = "QUEUED" if attempt == 1 else "FAILED"
            assert await ev.worker.recover_expired() == [(run["id"], expected)]
        out = (await detail(ev, run["id"]))["run"]
        assert out["status"] == "FAILED" and "Gave up after 2 attempts" in out["error"]
        assert ev.metric("agenttwin_eval_runs_total", status="FAILED") == 1
        assert [t["to_status"] for t in (await detail(ev, run["id"]))["transitions"]] == [
            "QUEUED",
            "PREPARING",
            "QUEUED",
            "PREPARING",
            "FAILED",
        ]


async def test_a_dataset_pins_the_suite() -> None:
    async with evaluation_with_simulation() as (ev, sim):
        ds = await ev.ok(
            "POST",
            "/api/v1/datasets",
            {
                "project_id": PROJECT,
                "name": "refunds",
                "cases": [{"scenario": "refund-over-limit"}, {"scenario": "refund-happy-path"}],
            },
            status=201,
        )
        dataset_id = ds["dataset"]["id"]
        run = await start(ev, dataset_id=dataset_id, seed=42)
        assert run["selection"] == {
            "scenarios": ["refund-happy-path", "refund-over-limit"],
            "tags": None,
            "dataset": {"id": dataset_id, "name": "refunds", "version": 1},
            "scenario_versions": None,
        }
        # A later version does not change what the run evaluates.
        await ev.ok(
            "POST",
            f"/api/v1/datasets/{dataset_id}/cases",
            {"cases": [{"scenario": "refund-rate-limited"}]},
            status=201,
        )
        out = await evaluate(ev, sim, run["id"])
        assert sorted(c["scenario_name"] for c in out["cases"]) == ["refund-happy-path", "refund-over-limit"]
        latest = await ev.ok("GET", f"/api/v1/datasets/{dataset_id}")
        results = {c["scenario"]: c["last_result"] for c in latest["version"]["cases"]}
        assert results["refund-happy-path"]["classification"] == "REGRESSED"
        assert results["refund-happy-path"]["eval_run_id"] == run["id"]
        assert results["refund-rate-limited"] is None
        listed = await ev.ok("GET", "/api/v1/eval-runs", dataset_id=dataset_id)
        assert [r["id"] for r in listed["items"]] == [run["id"]]

        body = {
            "project_id": PROJECT,
            "agent": AGENT,
            "baseline_version": "1.2.4",
            "candidate_version": "1.3.0",
        }
        await ev.fails(
            "POST",
            "/api/v1/eval-runs",
            body | {"dataset_id": dataset_id, "scenarios": ["x"]},
            status=400,
            code="INVALID_REQUEST",
        )
        await ev.fails(
            "POST", "/api/v1/eval-runs", body | {"dataset_version": 1}, status=400, code="INVALID_REQUEST"
        )
        await ev.fails(
            "POST",
            "/api/v1/eval-runs",
            body | {"dataset_id": dataset_id, "dataset_version": 9},
            status=404,
            code="NOT_FOUND",
        )
        empty = await ev.ok("POST", "/api/v1/datasets", {"project_id": PROJECT, "name": "empty"}, status=201)
        elsewhere = await ev.ok(
            "POST", "/api/v1/datasets", {"project_id": OTHER_PROJECT, "name": "theirs"}, status=201, as_=BOTH
        )
        mixed = body | {"dataset_id": elsewhere["dataset"]["id"]}
        await ev.fails("POST", "/api/v1/eval-runs", mixed, status=404, code="NOT_FOUND", as_=BOTH)
        await ev.fails(
            "POST",
            "/api/v1/eval-runs",
            body | {"dataset_id": empty["dataset"]["id"]},
            status=400,
            code="DATASET_EMPTY",
        )
        await ev.ok("POST", f"/api/v1/datasets/{dataset_id}/archive")
        await ev.fails(
            "POST",
            "/api/v1/eval-runs",
            body | {"dataset_id": dataset_id},
            status=409,
            code="DATASET_ARCHIVED",
        )


async def test_runs_are_listed_newest_first_and_hidden_from_other_projects() -> None:
    async with evaluation_with_simulation() as (ev, _sim):
        ids = [(await start(ev, candidate=v))["id"] for v in ("1.2.4", "1.3.0", "1.3.1")]
        seen: list[str] = []
        cursor = None
        for _ in range(4):
            page = await ev.ok("GET", "/api/v1/eval-runs", limit=2, cursor=cursor)
            seen += [r["id"] for r in page["items"]]
            cursor = page["next_cursor"]
            if cursor is None:
                break
        assert seen == ids[::-1]
        assert [
            r["candidate_version"] for r in (await ev.ok("GET", "/api/v1/eval-runs?status=QUEUED"))["items"]
        ] == [
            "1.3.1",
            "1.3.0",
            "1.2.4",
        ]
        assert (await ev.ok("GET", "/api/v1/eval-runs?status=COMPLETED"))["items"] == []
        assert len((await ev.ok("GET", "/api/v1/eval-runs", agent=AGENT, project_id=PROJECT))["items"]) == 3
        assert (await ev.ok("GET", "/api/v1/eval-runs", as_=OUTSIDER))["items"] == []
        await ev.fails("GET", f"/api/v1/eval-runs/{ids[0]}", status=404, code="NOT_FOUND", as_=OUTSIDER)
        await ev.fails(
            "POST", f"/api/v1/eval-runs/{ids[0]}/cancel", status=404, code="NOT_FOUND", as_=OUTSIDER
        )
        await ev.fails(
            "GET", f"/api/v1/eval-runs/{ids[0]}/cases/refund-happy-path", status=404, code="NOT_FOUND"
        )
        await ev.fails(
            "GET", f"/api/v1/eval-runs/{ids[0]}/cases/Bad_Name", status=400, code="INVALID_PARAMETER"
        )
        await ev.fails("GET", "/api/v1/eval-runs?status=x", status=400, code="INVALID_PARAMETER")
        await ev.fails("GET", "/api/v1/eval-runs", status=400, code="INVALID_CURSOR", cursor="!!")
        await ev.fails("GET", "/api/v1/eval-runs/not-a-uuid", status=400, code="INVALID_PARAMETER")
        body = {
            "project_id": PROJECT,
            "agent": AGENT,
            "baseline_version": "1.2.4",
            "candidate_version": "1.3.0",
        }
        await ev.fails("POST", "/api/v1/eval-runs", body, status=403, code="FORBIDDEN", as_=VIEWER)
        await ev.fails("POST", "/api/v1/eval-runs", body, status=404, code="NOT_FOUND", as_=OUTSIDER)
        await ev.fails(
            "POST", "/api/v1/eval-runs", body | {"tags": ["Bad Tag"]}, status=400, code="INVALID_REQUEST"
        )
        assert (await ev.ok("GET", f"/api/v1/eval-runs/{ids[0]}", as_=VIEWER))["run"]["id"] == ids[0]


async def test_a_worker_that_lost_the_run_cannot_end_it() -> None:
    async with evaluation_with_simulation() as (ev, _sim):
        run = await start(ev)
        stale = EvalWorker(
            store=ev.store,
            cfg=ev.cfg,
            simulation=ev.worker.simulation,
            traces=ev.worker.traces,
            judge=ev.worker.judge,
            log=ev.worker.log,
            owner="stale-worker",
        )
        claimed = await ev.store.claim_next_eval_run("stale-worker", 5)
        assert claimed is not None and claimed["id"] == run["id"]
        await ev.store.all("UPDATE eval_run SET lease_expires_at = now() - interval '1 s' RETURNING id")
        assert await ev.worker.recover_expired() == [(run["id"], "QUEUED")]
        assert await ev.worker.process_next() == run["id"]
        await stale.finish(claimed, JobStatus.FAILED, "stale", held=True)
        out = (await detail(ev, run["id"]))["run"]
        assert (out["status"], out["error"], out["attempts"]) == ("RUNNING", None, 2)


async def test_the_janitor_reports_the_queue_and_the_runs_in_progress() -> None:
    async with evaluation_stack() as ev:
        await start(ev)
        await start(ev)
        stop = asyncio.Event()
        janitor = asyncio.create_task(ev.worker.janitor_loop(stop))
        try:
            for _ in range(100):
                if ev.metric("agenttwin_eval_runs_in_progress", status="QUEUED") == 2:
                    break
                await asyncio.sleep(0.02)
        finally:
            stop.set()
            await janitor
        assert ev.metric("agenttwin_eval_runs_in_progress", status="QUEUED") == 2
        assert ev.metric("agenttwin_eval_runs_in_progress", status="RUNNING") == 0
