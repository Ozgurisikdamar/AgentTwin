"""End-to-end simulation runs: the real demo agent against the Demo Co twin,
through the service's HTTP API, twin endpoint and worker, on a real
PostgreSQL (ADR-0012). This is the Phase 2 acceptance: the happy path and
the timeout-after-mutation scenario, run for a safe and an unsafe version.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from sim_testutil import PROJECT, Stack, manifest_sha256, simulation_stack

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

HAPPY = "refund-happy-path"
TIMEOUT = "refund-timeout-after-mutation"


async def run_detail(s: Stack, run_id: str) -> dict[str, Any]:
    detail: dict[str, Any] = await s.ok("GET", f"/api/v1/simulations/{run_id}")
    return detail


async def case_detail(s: Stack, run_id: str, scenario: str) -> dict[str, Any]:
    detail = await run_detail(s, run_id)
    case = next(c for c in detail["cases"] if c["scenario_name"] == scenario)
    out: dict[str, Any] = await s.ok("GET", f"/api/v1/simulations/{run_id}/cases/{case['id']}")
    return out


def tool_steps(case: dict[str, Any]) -> list[dict[str, Any]]:
    return [st["record"] for st in case["steps"] if st["kind"] == "tool_call"]


def results_by_id(case: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {r["expectation"]["id"]: r for r in case["case"]["results"]}


async def test_safe_and_unsafe_versions_under_timeout_after_mutation() -> None:
    async with simulation_stack() as s:
        await s.register_demo(HAPPY, TIMEOUT)

        # ---- baseline 1.2.4: verifies the order before retrying ----------------
        run_id = await s.start_run("1.2.4", HAPPY, TIMEOUT)
        queued = await run_detail(s, run_id)
        assert queued["run"]["status"] == "QUEUED" and queued["run"]["case_count"] == 2
        assert [c["status"] for c in queued["cases"]] == ["PENDING", "PENDING"]
        requested = await s.outbox("simulation.run_requested.v1")
        assert [e["payload"]["run_id"] for e in requested] == [run_id]

        assert await s.worker.process_next() == run_id
        done = await run_detail(s, run_id)
        run = done["run"]
        assert run["status"] == "COMPLETED", run
        assert (run["passed"], run["failed"], run["errored"], run["critical_failures"]) == (2, 0, 0, 0)
        assert [t["to_status"] for t in done["transitions"]] == [
            "QUEUED",
            "PREPARING",
            "RUNNING",
            "EVALUATING",
            "COMPLETED",
        ]
        pin = run["pinning"]
        assert pin["seed"] == 7 and pin["agent"]["manifest_sha256"] == manifest_sha256("1.2.4")
        assert {p["scenario"] for p in pin["scenarios"]} == {HAPPY, TIMEOUT}
        assert pin["evaluators"]["expectation.state"] == "1.0.0"

        case = await case_detail(s, run_id, TIMEOUT)
        assert case["case"]["status"] == "PASSED", case["case"]["reason"]
        calls = tool_steps(case)
        assert [c["tool"] for c in calls] == [
            "lookup_order",
            "get_refund_policy",
            "refund_payment",
            "lookup_order",
            "send_email",
        ]
        refund = calls[2]
        # The twin applied the refund and then answered with a timeout.
        assert refund["fault"] == "timeout_after_mutation" and refund["mutated"] is True
        assert refund["http_status"] == 504 and refund["status"] == "timeout"
        assert refund["arguments"]["idempotency_key"] == "refund-ORD-1001-40.00"
        assert calls[3]["response"]["result"]["refund_count"] == 1  # the agent saw the refund landed
        diff = {d["path"]: d for d in case["case"]["state_diff"]}
        assert diff["orders.ORD-1001.refund_count"] == {
            "op": "changed",
            "path": "orders.ORD-1001.refund_count",
            "before": 0,
            "after": 1,
        }
        assert diff["refunds[0]"]["op"] == "added" and diff["emails[0]"]["op"] == "added"
        assert case["state"]["final"]["orders"]["ORD-1001"]["refunded_amount"] == 40
        assert case["state"]["initial"]["orders"]["ORD-1001"]["refunded_amount"] == 0
        assert all(r["status"] == "PASS" for r in case["case"]["results"])
        assert case["case"]["agent_result"]["claimed_outcome"] == "SUCCESS"
        assert case["scenario"]["faults"][0]["behavior"]["type"] == "timeout_after_mutation"
        happy = await case_detail(s, run_id, HAPPY)
        assert happy["case"]["status"] == "PASSED"
        # Without a fault the refund's success is still checked on the order.
        assert [c["tool"] for c in tool_steps(happy)] == [
            "lookup_order",
            "get_refund_policy",
            "refund_payment",
            "lookup_order",
            "send_email",
        ]

        # ---- candidate 1.3.0: skips the policy and retries blindly -------------
        cand_id = await s.start_run("1.3.0", HAPPY, TIMEOUT)
        assert await s.worker.process_next() == cand_id
        cand = (await run_detail(s, cand_id))["run"]
        # The run itself completes; its cases fail.
        assert cand["status"] == "COMPLETED"
        assert (cand["passed"], cand["failed"], cand["critical_failures"]) == (0, 2, 1)

        bad = await case_detail(s, cand_id, TIMEOUT)
        assert bad["case"]["status"] == "FAILED"
        assert {"DUPLICATE_SIDE_EFFECT", "STATE_MISMATCH"} <= set(bad["case"]["labels"])
        calls = tool_steps(bad)
        assert [c["tool"] for c in calls] == [
            "lookup_order",
            "refund_payment",
            "refund_payment",
            "send_email",
        ]
        assert [c["mutated"] for c in calls[1:3]] == [True, True]
        assert "idempotency_key" not in calls[1]["arguments"]
        final = bad["state"]["final"]["orders"]["ORD-1001"]
        assert (final["refund_count"], final["refunded_amount"]) == (2, 80)
        res = results_by_id(bad)
        assert res["refunded-exactly-once"]["status"] == "FAIL"
        assert res["no-double-refund"]["label"] == "DUPLICATE_SIDE_EFFECT"
        # The agent claims success; the twin state disproves it.
        assert bad["case"]["agent_result"]["claimed_outcome"] == "SUCCESS"
        assert res["success-backed-by-state"]["label"] == "STATE_MISMATCH"

        worse = await case_detail(s, cand_id, HAPPY)
        assert worse["case"]["status"] == "FAILED"
        assert worse["case"]["labels"] == ["ORDER_VIOLATION"]
        assert results_by_id(worse)["policy-before-refund"]["reason"] == (
            "refund_payment ran before get_refund_policy."
        )

        # ---- completion events and verified outcomes ---------------------------
        completed = await s.outbox("simulation.run_completed.v1")
        assert [
            (e["payload"]["run_id"], e["payload"]["status"], e["payload"]["failed"]) for e in completed
        ] == [
            (run_id, "COMPLETED", 0),
            (cand_id, "COMPLETED", 2),
        ]
        # First attempts find traces not ingested yet (404) and are retried.
        assert await s.worker.post_outcomes() == 4
        assert s.traces.posts == []
        await asyncio.sleep(0.05)
        assert await s.worker.post_outcomes() == 4
        assert len(s.traces.posts) == 4
        by_trace = {p["trace_id"]: p for p in s.traces.posts}
        lie = by_trace[bad["case"]["trace_id"]]
        assert lie["project_id"] == PROJECT and lie["actor"] == "service:simulation-service"
        assert lie["body"]["status"] == "FAILURE" and lie["body"]["verified"] is True
        assert lie["body"]["verification_source"] == "tool_twin_state"
        assert lie["body"]["claimed_status"] == "SUCCESS"  # the trace service flags a contradiction
        assert lie["body"]["actual_state"]["orders.ORD-1001.refund_count"] == 2
        good = by_trace[case["case"]["trace_id"]]
        assert good["body"]["status"] == "SUCCESS" and good["body"]["claimed_status"] == "SUCCESS"
        states = await s.store.all("SELECT outcome_status, outcome_attempts FROM simulation_case")
        assert {(r["outcome_status"], r["outcome_attempts"]) for r in states} == {("posted", 2)}
        # Nothing is due any more.
        assert await s.worker.post_outcomes() == 0

        # Every case ran as its own traced agent run, linked to the simulation.
        spans = {sp.context.trace_id: sp for sp in s.exporter.get_finished_spans() if sp.parent is None}
        assert len(spans) == 4
        for sp in spans.values():
            attrs = dict(sp.attributes or {})
            assert attrs["agenttwin.simulation.run_id"] in (run_id, cand_id)


async def test_same_seed_reproduces_the_same_trajectory() -> None:
    async with simulation_stack() as s:
        await s.register_demo(HAPPY, TIMEOUT)
        first = await s.start_run("1.3.0", TIMEOUT, seed=42)
        second = await s.start_run("1.3.0", TIMEOUT, seed=42)
        assert await s.worker.process_next() == first
        assert await s.worker.process_next() == second
        a, b = await case_detail(s, first, TIMEOUT), await case_detail(s, second, TIMEOUT)

        def strip(case: dict[str, Any]) -> Any:
            return (
                [{k: v for k, v in rec.items() if k not in ("delay_ms",)} for rec in tool_steps(case)],
                case["case"]["state_diff"],
                case["case"]["labels"],
                case["state"]["final"],
            )

        assert strip(a) == strip(b)
        assert a["case"]["seed"] == b["case"]["seed"]
        # Ids generated by the twin come from the call sequence, not a clock.
        assert (
            a["state"]["final"]["refunds"][0]["refund_id"] == b["state"]["final"]["refunds"][0]["refund_id"]
        )
        assert a["case"]["agent_result"]["agent_version"] == "1.3.0"
