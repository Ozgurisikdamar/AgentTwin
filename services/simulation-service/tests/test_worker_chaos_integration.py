"""Chaos tests (spec §64) for the simulation worker: a bug in the middle of a
run fails it at once and says so; the database going away in the middle of
a run leaves it to be retried, not failed; a worker killed mid-run is
recovered by another (test_run_lifecycle_integration covers the lease)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from psycopg import OperationalError

from agenttwin_simulation.runner import Worker
from sim_testutil import simulation_stack

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

HAPPY = "refund-happy-path"


async def test_a_bug_in_the_middle_of_a_run_fails_it_and_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    async with simulation_stack() as s:
        await s.register_demo(HAPPY)
        run_id = await s.start_run("1.2.4", HAPPY)

        async def broken(*_: Any, **__: Any) -> Any:
            raise KeyError("expected_tools")

        monkeypatch.setattr(s.worker, "run_case", broken)
        assert await s.worker.process_next() == run_id
        detail = await s.ok("GET", f"/api/v1/simulations/{run_id}")
        # Clearly failed: not left running, not retried, not a verdict.
        assert detail["run"]["status"] == "FAILED"
        assert detail["run"]["error"] == "internal error (KeyError)"
        assert [c["status"] for c in detail["cases"]] == ["CANCELLED"]
        assert detail["run"]["passed"] == 0
        events = await s.outbox("simulation.run_completed.v1")
        assert [(e["payload"]["run_id"], e["payload"]["status"]) for e in events] == [(run_id, "FAILED")]


async def test_the_database_going_away_mid_run_leaves_the_run_to_be_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with simulation_stack(lease_seconds=0.5) as s:
        await s.register_demo(HAPPY)
        run_id = await s.start_run("1.2.4", HAPPY)
        real = Worker.run_case
        calls = 0

        async def outage_once(self: Worker, *args: Any, **kwargs: Any) -> Any:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OperationalError("consuming input failed: server closed the connection unexpectedly")
            return await real(self, *args, **kwargs)

        monkeypatch.setattr(Worker, "run_case", outage_once)
        assert await s.worker.process_next() == run_id
        # Not failed: an outage is not the run's fault. It keeps its state
        # until its lease expires and the janitor puts it back in the queue.
        detail = await s.ok("GET", f"/api/v1/simulations/{run_id}")
        assert detail["run"]["status"] == "RUNNING" and detail["run"]["error"] is None
        assert await s.outbox("simulation.run_completed.v1") == []

        for _ in range(100):
            if await s.worker.recover() == [(run_id, "QUEUED")]:
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("the run was never put back in the queue")
        assert await s.worker.process_next() == run_id
        detail = await s.ok("GET", f"/api/v1/simulations/{run_id}")
        assert (detail["run"]["status"], detail["run"]["attempts"], detail["run"]["passed"]) == (
            "COMPLETED",
            2,
            1,
        )
