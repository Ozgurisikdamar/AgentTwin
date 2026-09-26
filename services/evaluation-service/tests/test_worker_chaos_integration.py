"""Chaos tests (spec §64) for the evaluation worker. A bug while evaluating
fails the run at once with the reason (retrying a deterministic failure
spends judge calls and delays the gate for nothing); the database going away
while evaluating leaves the run to its lease, and it is evaluated again once
the database is back. Either way no run is left in between, and nothing that
was not compared becomes a verdict."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from psycopg import OperationalError

from agenttwin_evaluation import worker as worker_module
from agenttwin_evaluation.store import Store
from eval_testutil import PROJECT, Stack, evaluation_with_simulation, run_simulations
from sim_testutil import Stack as SimStack

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

SCENARIO = "refund-happy-path"


async def start(ev: Stack) -> str:
    out = await ev.ok(
        "POST",
        "/api/v1/eval-runs",
        {
            "project_id": PROJECT,
            "agent": "support-refund-agent",
            "baseline_version": "1.2.4",
            "candidate_version": "1.3.0",
            "scenarios": [SCENARIO],
            "seed": 7,
        },
        status=202,
    )
    return str(out["run"]["id"])


async def simulated(ev: Stack, sim: SimStack, run_id: str) -> None:
    assert await ev.worker.process_next() == run_id
    await run_simulations(sim)


async def detail(ev: Stack, run_id: str) -> dict[str, Any]:
    out: dict[str, Any] = await ev.ok("GET", f"/api/v1/eval-runs/{run_id}")
    return out


async def test_a_bug_while_evaluating_fails_the_run_at_once_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with evaluation_with_simulation() as (ev, sim):
        run_id = await start(ev)
        await simulated(ev, sim, run_id)

        def broken(*_: Any, **__: Any) -> Any:
            raise ZeroDivisionError("division by zero")

        monkeypatch.setattr(worker_module, "summarize", broken)
        await ev.worker.check_waiting()
        run = (await detail(ev, run_id))["run"]
        assert run["status"] == "FAILED" and run["error"] == "internal error (ZeroDivisionError)"
        assert run["attempts"] == 1, "a deterministic failure is not retried"
        events = await ev.outbox("evaluation.run_completed.v1")
        assert [(e["payload"]["eval_run_id"], e["payload"]["status"]) for e in events] == [(run_id, "FAILED")]
        # Nothing half-written: a failed run has no case results.
        assert (await detail(ev, run_id)).get("cases") in (None, [])


async def test_the_database_going_away_while_evaluating_leaves_the_run_to_be_evaluated_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with evaluation_with_simulation(lease_seconds=0.5) as (ev, sim):
        run_id = await start(ev)
        await simulated(ev, sim, run_id)
        real = Store.save_case_results
        calls = 0

        async def outage_once(self: Store, *args: Any, **kwargs: Any) -> Any:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OperationalError("consuming input failed: server closed the connection unexpectedly")
            return await real(self, *args, **kwargs)

        monkeypatch.setattr(Store, "save_case_results", outage_once)
        await ev.worker.check_waiting()
        run = (await detail(ev, run_id))["run"]
        # Not failed and not completed: it waits for its lease to expire.
        assert run["status"] == "EVALUATING" and run["error"] is None
        assert await ev.outbox("evaluation.run_completed.v1") == []

        for _ in range(100):
            if (run_id, "QUEUED") in await ev.worker.recover_expired():
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("the run was never put back in the queue")
        # The same pair of simulations (they ended): evaluated again, completed.
        assert await ev.worker.process_next() == run_id
        await ev.worker.check_waiting()
        run = (await detail(ev, run_id))["run"]
        assert (run["status"], run["attempts"], run["case_count"]) == ("COMPLETED", 2, 1)
        events = await ev.outbox("evaluation.run_completed.v1")
        assert [(e["payload"]["eval_run_id"], e["payload"]["status"]) for e in events] == [
            (run_id, "COMPLETED")
        ]
