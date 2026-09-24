"""Run lifecycle on a real PostgreSQL: cancellation (queued and in flight),
worker crashes (expired leases), stale workers and agent failures."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agenttwin_core.db import transaction
from agenttwin_core.jobs import JobStatus
from agenttwin_simulation.config import AgentEndpoint
from sim_testutil import AGENT, PROJECT, Stack, simulation_stack

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

HAPPY = "refund-happy-path"
TIMEOUT = "refund-timeout-after-mutation"

SLOW: dict[str, Any] = {
    "apiVersion": "agenttwin.dev/v1",
    "kind": "Scenario",
    # Critical: runs before the demo scenarios (cases run in severity order).
    "metadata": {"name": "slow-order-system", "severity": "critical"},
    "spec": {
        "agent": AGENT,
        "twin": "demo-co-support",
        "input": {
            "message": "Where is my order ORD-1001?",
            "context": {"tenant": "demo-co", "customer_id": "CUS-100"},
        },
        "faults": [{"target": "lookup_order", "behavior": {"type": "delay", "delayMs": 4000}}],
        "expectations": [{"type": "toolCalled", "tool": "lookup_order"}],
    },
}


async def statuses(s: Stack, run_id: str) -> tuple[str, list[str]]:
    detail = await s.ok("GET", f"/api/v1/simulations/{run_id}")
    return detail["run"]["status"], [c["status"] for c in detail["cases"]]


async def wait_inside_tool_call(s: Stack, run_id: str, scenario: str) -> None:
    """Returns once the scenario's case is running and the twin has recorded
    a call of it (the twin sleeps a delay fault after recording the call)."""
    for _ in range(400):
        row = await s.store.one(
            """SELECT c.status, (SELECT count(*) FROM simulation_step st WHERE st.case_id = c.id) AS steps
               FROM simulation_case c WHERE c.run_id = %s AND c.scenario_name = %s""",
            (run_id, scenario),
        )
        if row is not None and row["status"] == "RUNNING" and row["steps"] >= 1:
            return
        await asyncio.sleep(0.025)
    raise AssertionError(f"{scenario} never reached its first tool call")


def cancel_reasons(detail: dict[str, Any]) -> dict[str, tuple[str, str]]:
    return {c["scenario_name"]: (c["status"], c["reason"]) for c in detail["cases"]}


async def test_cancel_a_queued_run() -> None:
    async with simulation_stack() as s:
        await s.register_demo(HAPPY, TIMEOUT)
        run_id = await s.start_run("1.2.4", HAPPY, TIMEOUT)
        out = await s.ok("POST", f"/api/v1/simulations/{run_id}/cancel")
        assert out["run"]["status"] == "CANCELLED"
        assert await statuses(s, run_id) == ("CANCELLED", ["CANCELLED", "CANCELLED"])
        detail = await s.ok("GET", f"/api/v1/simulations/{run_id}")
        assert detail["run"]["cancelled"] == 2
        assert detail["transitions"][-1]["reason"] == "cancelled by user:engineer"
        [event] = await s.outbox("simulation.run_completed.v1")
        assert event["payload"] == event["payload"] | {
            "run_id": run_id,
            "status": "CANCELLED",
            "cancelled": 2,
        }
        # Nothing is left for a worker; a repeated cancel is a no-op.
        assert await s.worker.process_next() is None
        again = await s.ok("POST", f"/api/v1/simulations/{run_id}/cancel")
        assert again["run"]["status"] == "CANCELLED"
        assert len(await s.outbox("simulation.run_completed.v1")) == 1


async def test_cancel_while_a_case_is_running() -> None:
    async with simulation_stack(agent_tool_timeout_s=10.0) as s:
        await s.register_demo(HAPPY)
        await s.ok("POST", "/api/v1/scenarios", {"project_id": PROJECT, "document": SLOW}, status=201)
        run_id = await s.start_run("1.2.4", "slow-order-system", HAPPY)
        worker = asyncio.create_task(s.worker.process_next())
        # The agent is inside the slow tool call (a 4 s delay fault).
        await wait_inside_tool_call(s, run_id, "slow-order-system")
        out = await s.ok("POST", f"/api/v1/simulations/{run_id}/cancel", status=202)
        assert out["run"]["status"] == "RUNNING" and out["run"]["cancel_requested"] is True
        started = asyncio.get_running_loop().time()
        assert await asyncio.wait_for(worker, 10) == run_id
        # The worker noticed within its polling interval, not after the 4 s delay.
        assert asyncio.get_running_loop().time() - started < 3.0
        detail = await s.ok("GET", f"/api/v1/simulations/{run_id}")
        assert detail["run"]["status"] == "CANCELLED"
        assert detail["run"]["cancelled"] == 2 and detail["run"]["passed"] == 0
        assert cancel_reasons(detail) == {
            "slow-order-system": ("CANCELLED", "The run was cancelled during this case."),
            HAPPY: ("CANCELLED", "The run was cancelled."),
        }
        assert [t["to_status"] for t in detail["transitions"]] == [
            "QUEUED",
            "PREPARING",
            "RUNNING",
            "CANCELLED",
        ]
        # The cancelled case's credential is revoked.
        row = await s.store.one("SELECT count(*) AS n FROM simulation_case WHERE token_hash IS NOT NULL")
        assert row is not None and row["n"] == 0
        [event] = await s.outbox("simulation.run_completed.v1")
        assert event["payload"]["status"] == "CANCELLED"


async def test_a_cancel_seen_after_the_agent_answered_still_cancels_the_case() -> None:
    """Race: the agent answers (quickly, maybe because the twin refused its
    calls with RUN_CANCELLED) before the worker's next cancellation poll."""
    async with simulation_stack() as s:
        await s.register_demo(HAPPY, TIMEOUT)
        run_id = await s.start_run("1.2.4", TIMEOUT, HAPPY)
        real_run = s.worker.agents.run

        async def answer_then_cancel(*args: Any, **kwargs: Any) -> Any:
            call = await real_run(*args, **kwargs)
            out = await s.ok("POST", f"/api/v1/simulations/{run_id}/cancel", status=202)
            assert out["run"]["cancel_requested"] is True
            return call

        s.worker.agents.run = answer_then_cancel  # type: ignore[method-assign]
        assert await s.worker.process_next() == run_id
        detail = await s.ok("GET", f"/api/v1/simulations/{run_id}")
        assert detail["run"]["status"] == "CANCELLED"
        # The answered case is not judged: nothing is reported as verified.
        assert cancel_reasons(detail) == {
            TIMEOUT: ("CANCELLED", "The run was cancelled during this case."),
            HAPPY: ("CANCELLED", "The run was cancelled."),
        }
        rows = await s.store.all("SELECT outcome_status, token_hash FROM simulation_case")
        assert rows == [{"outcome_status": "none", "token_hash": None}] * 2
        assert await s.worker.post_outcomes() == 0


async def test_a_cancel_after_the_last_case_is_still_honoured() -> None:
    """Race: every case is judged, then the cancel request lands before the
    worker completes the run. The run row lock orders the two."""
    async with simulation_stack() as s:
        await s.register_demo(HAPPY)
        run_id = await s.start_run("1.2.4", HAPPY)
        real_finish = s.store.finish_case

        async def finish_then_cancel(conn: Any, case_id: str, fields: dict[str, Any]) -> None:
            await real_finish(conn, case_id, fields)
            await s.ok("POST", f"/api/v1/simulations/{run_id}/cancel", status=202)

        s.store.finish_case = finish_then_cancel  # type: ignore[method-assign]
        assert await s.worker.process_next() == run_id
        detail = await s.ok("GET", f"/api/v1/simulations/{run_id}")
        run = detail["run"]
        assert (run["status"], run["passed"], run["cancelled"]) == ("CANCELLED", 1, 0)
        assert detail["transitions"][-1] == detail["transitions"][-1] | {
            "from_status": "RUNNING",
            "to_status": "CANCELLED",
            "reason": "cancelled on request",
        }
        # The judged case keeps its verdict and its outcome is still reported.
        assert detail["cases"][0]["status"] == "PASSED"
        [event] = await s.outbox("simulation.run_completed.v1")
        assert (event["payload"]["status"], event["payload"]["passed"]) == ("CANCELLED", 1)
        # Once the run is final a cancel request changes nothing.
        again = await s.ok("POST", f"/api/v1/simulations/{run_id}/cancel")
        assert again["run"]["status"] == "CANCELLED"


async def test_a_cancelled_run_whose_worker_died_is_cancelled_not_retried() -> None:
    async with simulation_stack() as s:
        await s.register_demo(HAPPY)
        run_id = await s.start_run("1.2.4", HAPPY)
        assert await s.store.claim_next_run("dead-worker", 0.2)
        out = await s.ok("POST", f"/api/v1/simulations/{run_id}/cancel", status=202)
        assert out["run"]["status"] == "PREPARING"
        await asyncio.sleep(0.3)
        assert await s.worker.recover() == [(run_id, "CANCELLED")]
        detail = await s.ok("GET", f"/api/v1/simulations/{run_id}")
        assert (detail["run"]["status"], detail["run"]["error"]) == ("CANCELLED", None)
        assert detail["cases"][0]["status"] == "CANCELLED"
        assert detail["transitions"][-1]["reason"] == "cancelled on request (the worker lease expired)"
        [event] = await s.outbox("simulation.run_completed.v1")
        assert event["payload"]["status"] == "CANCELLED"
        assert await s.worker.process_next() is None


async def test_expired_leases_are_recovered_or_failed() -> None:
    async with simulation_stack() as s:
        await s.register_demo(HAPPY, TIMEOUT)
        run_id = await s.start_run("1.2.4", HAPPY)
        # A worker claims the run, starts a case and dies (its lease expires).
        dead = await s.store.claim_next_run("dead-worker", 0.2)
        assert dead is not None and dead["attempts"] == 1
        async with transaction(s.pool) as conn:
            assert await s.store.transition(conn, run_id, JobStatus.RUNNING, "x", owner="dead-worker")
        [case] = await s.store.pending_cases(run_id)
        await s.store.start_case(case["id"], b"\x01" * 32, {"state": {}, "initial": {}})
        await asyncio.sleep(0.3)
        assert await s.worker.recover() == [(run_id, "QUEUED")]
        row = await s.store.one("SELECT status, token_hash FROM simulation_case WHERE id = %s", (case["id"],))
        assert row == {"status": "PENDING", "token_hash": None}
        # The dead worker cannot move the run any more.
        async with transaction(s.pool) as conn:
            assert await s.store.transition(conn, run_id, JobStatus.COMPLETED, owner="dead-worker") is None
        # Another worker finishes it from a fresh twin.
        assert await s.worker.process_next() == run_id
        detail = await s.ok("GET", f"/api/v1/simulations/{run_id}")
        assert detail["run"]["status"] == "COMPLETED" and detail["run"]["attempts"] == 2
        assert detail["run"]["passed"] == 1
        assert "worker lease expired" in [t["reason"] for t in detail["transitions"]]

        # After the attempt limit the run fails and announces it.
        other = await s.start_run("1.2.4", TIMEOUT)
        assert await s.store.claim_next_run("dead-worker", 0.1)
        await s.store.one("UPDATE simulation_run SET attempts = 3 WHERE id = %s RETURNING id", (other,))
        await asyncio.sleep(0.2)
        assert await s.worker.recover() == [(other, "FAILED")]
        detail = await s.ok("GET", f"/api/v1/simulations/{other}")
        assert detail["run"]["status"] == "FAILED"
        assert detail["run"]["error"] == "worker lease expired 3 times"
        assert detail["cases"][0]["status"] == "CANCELLED"
        events = await s.outbox("simulation.run_completed.v1")
        assert [(e["payload"]["run_id"], e["payload"]["status"]) for e in events] == [
            (run_id, "COMPLETED"),
            (other, "FAILED"),
        ]


async def test_agent_failures_decide_the_verdict() -> None:
    async with simulation_stack() as s:
        await s.register_demo(HAPPY)
        # An agent that cannot be reached: the case errors (infrastructure).
        s.cfg.agent_endpoints[AGENT] = AgentEndpoint(url="http://127.0.0.1:9")
        run_id = await s.start_run("1.2.4", HAPPY)
        assert await s.worker.process_next() == run_id
        detail = await s.ok("GET", f"/api/v1/simulations/{run_id}")
        [case] = detail["cases"]
        assert (detail["run"]["status"], case["status"]) == ("COMPLETED", "ERRORED")
        assert (detail["run"]["errored"], detail["run"]["failed"]) == (1, 0)
        full = await s.ok("GET", f"/api/v1/simulations/{run_id}/cases/{case['id']}")
        first, *expectations = full["case"]["results"]
        assert (first["status"], first["label"]) == ("ERROR", "AGENT_UNREACHABLE")
        assert full["case"]["reason"] == first["reason"]
        assert full["case"]["reason"].startswith("The agent could not be run:")
        # Its expectations are not judged against a twin the agent never used.
        assert len(expectations) == 7
        assert {(r["status"], r["reason"]) for r in expectations} == {
            ("SKIPPED", "The agent did not run, so this was not evaluated.")
        }
        assert full["case"]["verdict"]["labels"] == []
        assert full["case"]["agent_result"]["kind"] == "unreachable"
        assert full["case"]["outcome_status"] == "none"  # no trace, nothing to report

        # A version the agent deployment does not have is a configuration error.
        del s.cfg.agent_endpoints[AGENT]
        r = await s.call(
            "POST",
            "/api/v1/simulations",
            {"project_id": PROJECT, "agent": AGENT, "agent_version": "1.2.4", "scenarios": [HAPPY]},
        )
        assert (r.status_code, r.json()["error"]["code"]) == (400, "AGENT_NOT_RUNNABLE")


async def test_a_run_whose_agent_endpoint_disappears_fails() -> None:
    async with simulation_stack() as s:
        await s.register_demo(HAPPY)
        run_id = await s.start_run("1.2.4", HAPPY)
        endpoints = dict(s.cfg.agent_endpoints)
        s.cfg.agent_endpoints.clear()
        assert await s.worker.process_next() == run_id
        s.cfg.agent_endpoints.update(endpoints)
        detail = await s.ok("GET", f"/api/v1/simulations/{run_id}")
        assert detail["run"]["status"] == "FAILED"
        assert "No endpoint is configured" in detail["run"]["error"]
        assert detail["cases"][0]["status"] == "CANCELLED"
