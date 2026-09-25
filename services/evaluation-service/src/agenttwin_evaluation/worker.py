"""The evaluation worker (spec §27): runs a candidate version against its
baseline on one pinned suite and compares them case by case.

QUEUED      a worker claims the run (lease) …
PREPARING   … and asks the simulation service for the pair of runs
            (ADR-0023; a repeat answers the same pair, so a retried run never
            starts a second one). A refusal fails the run with its reason.
RUNNING     no worker holds the run while its simulations execute: it is
            checked when due — every poll interval, at once when a
            ``simulation.run_completed.v1`` arrives — until both are final or
            the deadline passes (the simulations are then cancelled).
EVALUATING  a worker takes the lease again, reads every case of both sides,
            grades the semantic expectations with the judge (budget, cache),
            compares each case, stores the results and the summary, and
            announces ``evaluation.run_completed.v1`` in the same transaction.

A worker that dies holding a lease leaves it to expire; the janitor puts the
run back in the queue (at most ``max_run_attempts`` times): everything a step
does is repeatable.
"""

from __future__ import annotations

import asyncio
import os
import socket
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from agenttwin_core.db import Conn, transaction
from agenttwin_core.evaluators import ToolCall
from agenttwin_core.events import Envelope, write_outbox
from agenttwin_core.ids import new_id
from agenttwin_core.jobs import JobStatus
from agenttwin_core.logx import Log
from agenttwin_evaluation.clients import PairRefused, SimulationClient, TraceClient, TraceUsage, UpstreamError
from agenttwin_evaluation.common import PRODUCER
from agenttwin_evaluation.comparison import DONE, CaseComparison, Side, compare_case, summarize
from agenttwin_evaluation.config import EvaluationConfig
from agenttwin_evaluation.judges import JudgeProvider, judge_identity
from agenttwin_evaluation.semantic import JudgeBudget, Judged, SemanticJudging
from agenttwin_evaluation.store import SCHEMA, JudgmentCache, Row, Store

__all__ = ["QUEUE", "EvalWorker", "judged_side"]

QUEUE = "evaluation-service.events"
FINAL = frozenset({"COMPLETED", "FAILED", "CANCELLED"})


class _LeaseLost(Exception):
    """The worker no longer holds the run; what it computed is discarded."""


def _default_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{new_id()[-8:]}"


async def judged_side(
    detail: Mapping[str, Any],
    judging: SemanticJudging,
    usage: TraceUsage | None,
) -> tuple[Side, dict[str, Any]]:
    """One side of a case from the simulation's case detail, its semantic
    expectations graded, and the snapshot that is stored with the result (the
    detail without the scenario document, the merged results and the
    verdicts) so the case can be shown and re-graded without the simulation
    service."""
    case = detail.get("case") or {}
    judged: list[Judged] = []
    if case.get("status") in DONE:
        spec = ((detail.get("scenario") or {}).get("document") or {}).get("spec") or {}
        message = str((spec.get("input") or {}).get("message") or "")
        agent = case.get("agent_result") or {}
        answer = agent.get("output") if isinstance(agent.get("output"), str) else None
        calls = [
            ToolCall.from_record(s["record"], s.get("latency_ms"))
            for s in detail.get("steps") or ()
            if s.get("kind") == "tool_call" and isinstance(s.get("record"), Mapping)
        ]
        for index, expectation in enumerate(spec.get("expectations") or ()):
            if isinstance(expectation, Mapping) and expectation.get("type") == "semantic":
                judged.append(
                    await judging.evaluate(
                        expectation, index, customer_message=message, answer=answer, calls=calls
                    )
                )
    tokens = usage.get("tokens") if usage is not None else None
    cost = usage.get("cost_usd") if usage is not None else None
    side = Side.from_case_detail(detail, judged=[j.result for j in judged], tokens=tokens, cost_usd=cost)
    snapshot = {
        "detail": {k: v for k, v in detail.items() if k != "scenario"},
        "judged": [j.result.to_json() for j in judged],
        "verdicts": [
            {
                "expectation_id": str(j.result.expectation.get("id")),
                "cached": j.cached,
                "judge": dict(j.judge),
                "verdict": j.verdict.to_json() if j.verdict is not None else None,
            }
            for j in judged
        ],
        "results": [r.to_json() for r in side.results],
        "tokens": tokens,
        "cost_usd": cost,
    }
    return side, snapshot


def completed_event(run: Mapping[str, Any], status: JobStatus, error: str | None) -> Envelope:
    return Envelope.new(
        "evaluation.run_completed.v1",
        PRODUCER,
        str(run["organization_id"]),
        str(run["project_id"]),
        str(run["id"]),
        {"eval_run_id": str(run["id"]), "release_evaluation_id": None, "status": str(status), "error": error},
    )


@dataclass
class EvalWorker:
    store: Store
    cfg: EvaluationConfig
    simulation: SimulationClient
    traces: TraceClient
    judge: JudgeProvider
    log: Log
    owner: str = field(default_factory=_default_owner)

    # ------------------------------------------------------------ steps

    async def process_next(self) -> str | None:
        """Claims one queued run and starts its simulations."""
        run = await self.store.claim_next_eval_run(self.owner, self.cfg.lease_seconds)
        if run is None:
            return None
        await self.prepare(run)
        return str(run["id"])

    async def prepare(self, run: Row) -> None:
        run_id, org, project = str(run["id"]), str(run["organization_id"]), str(run["project_id"])
        selection = run["selection"] or {}
        body = {
            "project_id": project,
            "eval_run_id": run_id,
            "agent": run["agent_name"],
            "baseline_version": run["baseline_version"],
            "candidate_version": run["candidate_version"],
            "scenarios": selection.get("scenarios"),
            "tags": selection.get("tags"),
            "seed": run["seed"],
            "release_id": run["release_id"],
            "requested_by": run["requested_by"],
        }
        if run["cancel_requested"] and run["attempts"] <= 1:
            await self.finish(
                run, JobStatus.CANCELLED, None, "cancelled before its simulations started", held=True
            )
            return
        try:
            pair = await self.simulation.start_pair(org, project, body)
        except PairRefused as err:
            await self.finish(
                run, JobStatus.FAILED, f"The simulations could not start: {err.describe()}", held=True
            )
            return
        except UpstreamError as err:
            # The lease expires and the janitor puts the run back in the queue.
            self.log.warn("simulation pair not started", eval_run_id=run_id, error=str(err))
            return
        baseline, candidate = str(pair["baseline"]["id"]), str(pair["candidate"]["id"])
        if run["cancel_requested"]:
            # An earlier attempt may have started the pair: stop it.
            await self._cancel_simulations(org, project, (baseline, candidate))
            await self.finish(
                run, JobStatus.CANCELLED, None, "cancelled before its simulations started", held=True
            )
            return
        now = datetime.now(UTC)
        pinning = {
            "seed": pair["seed"],
            "cases": pair["cases"],
            "baseline": pair["baseline"].get("pinning") or {},
            "candidate": pair["candidate"].get("pinning") or {},
        }
        async with transaction(self.store.pool) as conn:
            moved = await self.store.transition(
                conn,
                run_id,
                JobStatus.RUNNING,
                f"simulations {baseline} and {candidate} started",
                owner=self.owner,
                extra={
                    "baseline_run_id": baseline,
                    "candidate_run_id": candidate,
                    "pinning": pinning,
                    "case_count": len(pair["cases"]),
                    "next_check_at": now,
                    "wait_deadline": now + timedelta(seconds=self.cfg.max_wait_s),
                },
            )
        if moved is None:
            self.log.warn("eval run moved on while preparing", eval_run_id=run_id)
        else:
            self.log.info("simulations started", eval_run_id=run_id, cases=len(pair["cases"]))

    async def check_waiting(self) -> list[str]:
        """Checks the waiting runs that are due; evaluates those whose two
        simulations ended. Returns the ids of the runs it checked."""
        rows = await self.store.due_waiting(self.cfg.poll_interval_s)
        for run in rows:
            try:
                await self.check(run)
            except Exception as err:  # noqa: BLE001 - one run's failure must not stop the others
                self.log.error("eval run check failed", eval_run_id=str(run["id"]), error=repr(err))
        return [str(r["id"]) for r in rows]

    async def check(self, run: Row) -> None:
        org, project = str(run["organization_id"]), str(run["project_id"])
        sims = (str(run["baseline_run_id"]), str(run["candidate_run_id"]))
        if run["cancel_requested"]:
            await self._cancel_simulations(org, project, sims)
            await self.finish(run, JobStatus.CANCELLED, None, "cancelled while its simulations ran")
            return
        try:
            statuses = [str((await self.simulation.run(org, project, s))["run"]["status"]) for s in sims]
        except UpstreamError as err:
            self.log.warn("simulation status unavailable", eval_run_id=str(run["id"]), error=str(err))
            statuses = []
        if statuses and all(s in FINAL for s in statuses):
            await self.evaluate(run, statuses)
            return
        deadline = run["wait_deadline"]
        if deadline is not None and datetime.now(UTC) >= deadline:
            await self._cancel_simulations(org, project, sims)
            await self.finish(
                run,
                JobStatus.FAILED,
                f"The simulations did not finish within {self.cfg.max_wait_s:g} s; they were cancelled.",
            )

    async def evaluate(self, run: Row, statuses: Sequence[str]) -> None:
        run_id = str(run["id"])
        async with transaction(self.store.pool) as conn:
            taken = await self.store.transition(
                conn,
                run_id,
                JobStatus.EVALUATING,
                f"simulations ended ({statuses[0]} / {statuses[1]})",
                lease=(self.owner, self.cfg.lease_seconds),
            )
        if taken is None:
            return  # another worker took it
        if "CANCELLED" in statuses:
            side = "baseline" if statuses[0] == "CANCELLED" else "candidate"
            await self.finish(taken, JobStatus.FAILED, f"The {side} simulation was cancelled.", held=True)
            return
        renewing = asyncio.create_task(self._keep_lease(run_id))
        try:
            await self._evaluate(taken)
        except UpstreamError as err:
            # The lease expires and the run is retried from the queue.
            self.log.warn("evaluation interrupted", eval_run_id=run_id, error=str(err))
        finally:
            renewing.cancel()
            with suppress(asyncio.CancelledError):
                await renewing

    async def _keep_lease(self, run_id: str) -> None:
        while True:
            await asyncio.sleep(max(0.05, self.cfg.lease_seconds / 3))
            if not await self.store.renew_lease(run_id, self.owner, self.cfg.lease_seconds):
                return

    async def _evaluate(self, run: Row) -> None:
        run_id, org, project = str(run["id"]), str(run["organization_id"]), str(run["project_id"])
        baseline_run, candidate_run = str(run["baseline_run_id"]), str(run["candidate_run_id"])
        usage_b = await self.traces.usage_of_run(org, project, baseline_run)
        usage_c = await self.traces.usage_of_run(org, project, candidate_run)
        latest = await self.store.latest_calibrations(org, project, judge_identity(self.judge))
        judging = SemanticJudging(
            judge=self.judge,
            budget=JudgeBudget(max_cost_usd=self.cfg.judge_budget_usd, max_calls=self.cfg.judge_max_calls),
            cache=JudgmentCache(self.store),
            calibrated=frozenset(name for name, row in latest.items() if row["calibrated"]),
            timeout_s=self.cfg.judge_call_timeout_s,
        )
        limit = asyncio.Semaphore(self.cfg.fetch_concurrency)

        from agenttwin_evaluation.reviews import needs_review

        async def one(pc: Mapping[str, Any]) -> tuple[CaseComparison, dict[str, Any]]:
            async with limit:
                bd = await self.simulation.case(org, project, baseline_run, str(pc["baseline_case_id"]))
                cd = await self.simulation.case(org, project, candidate_run, str(pc["candidate_case_id"]))
                b, bsnap = await judged_side(bd, judging, usage_b.get(str(bd["case"].get("trace_id"))))
                c, csnap = await judged_side(cd, judging, usage_c.get(str(cd["case"].get("trace_id"))))
            meta = ((bd.get("scenario") or {}).get("document") or {}).get("metadata") or {}
            tags = sorted({str(t) for t in meta.get("tags") or ()})
            comparison = compare_case(str(pc["scenario_name"]), str(pc["severity"]), tags, b, c)
            return comparison, {
                "position": int(pc["position"]),
                "scenario_name": str(pc["scenario_name"]),
                "severity": str(pc["severity"]),
                "tags": tags,
                "classification": comparison.classification,
                "baseline_case_id": b.case_id,
                "candidate_case_id": c.case_id,
                "sides": {"baseline": bsnap, "candidate": csnap},
                "comparison": comparison.to_json(),
                "needs_review": [
                    f"{side}:{eid}"
                    for side, snap in (("BASELINE", bsnap), ("CANDIDATE", csnap))
                    for eid in needs_review(snap["results"], snap["verdicts"])
                ],
            }

        cases = sorted((run["pinning"] or {}).get("cases") or (), key=lambda c: int(c["position"]))
        results = await asyncio.gather(*(one(pc) for pc in cases))
        comparisons = [c for c, _ in results]
        summary = summarize(comparisons)
        counts = summary["counts"]
        try:
            async with transaction(self.store.pool) as conn:
                await self._complete(conn, run_id, [row for _, row in results], summary, judging)
        except _LeaseLost:
            self.log.warn("eval run lease lost before completion; results discarded", eval_run_id=run_id)
            return
        self.log.info("eval run completed", eval_run_id=run_id, **{k.lower(): v for k, v in counts.items()})

    async def _complete(
        self,
        conn: Conn,
        run_id: str,
        rows: Sequence[Mapping[str, Any]],
        summary: Mapping[str, Any],
        judging: SemanticJudging,
    ) -> None:
        counts = summary["counts"]
        await self.store.save_case_results(conn, run_id, rows)
        done = await self.store.transition(
            conn,
            run_id,
            JobStatus.COMPLETED,
            "compared",
            owner=self.owner,
            extra={
                "summary": summary,
                "judge": judging.identity(),
                "budget": judging.budget.to_json(),
                "case_count": len(rows),
                "new_critical_failures": counts["NEW_CRITICAL_FAILURE"],
                "regressed": counts["REGRESSED"],
                "improved": counts["IMPROVED"],
                "unchanged": counts["UNCHANGED"],
                "incomplete": counts["INCOMPLETE"],
            },
        )
        if done is None:
            raise _LeaseLost(run_id)
        await write_outbox(conn, SCHEMA, completed_event(done, JobStatus.COMPLETED, None))

    # ------------------------------------------------------------ endings

    async def finish(
        self, run: Row, status: JobStatus, error: str | None, reason: str | None = None, *, held: bool = False
    ) -> None:
        """Ends the run (``held``: only while this worker holds its lease)."""
        async with transaction(self.store.pool) as conn:
            await self._finish_in(conn, run, status, error, reason, owner=self.owner if held else None)

    async def _finish_in(
        self,
        conn: Conn,
        run: Row,
        status: JobStatus,
        error: str | None,
        reason: str | None = None,
        *,
        owner: str | None = None,
    ) -> Row | None:
        done = await self.store.transition(
            conn,
            str(run["id"]),
            status,
            reason or error,
            owner=owner,
            extra={"error": error} if error else None,
        )
        if done is not None:
            await write_outbox(conn, SCHEMA, completed_event(done, status, error))
            self.log.info("eval run ended", eval_run_id=str(run["id"]), status=str(status), error=error)
        return done

    async def _cancel_simulations(self, org: str, project: str, runs: Sequence[str]) -> None:
        for sim in runs:
            try:
                await self.simulation.cancel(org, project, sim)
            except UpstreamError as err:
                self.log.warn("simulation not cancelled", simulation_run_id=sim, error=str(err))

    async def recover_expired(self) -> list[tuple[str, str]]:
        """Runs whose worker lost its lease go back to the queue, or fail after
        ``max_run_attempts``."""
        out: list[tuple[str, str]] = []
        async with transaction(self.store.pool) as conn:
            for run in await self.store.expired_leases(conn):
                if int(run["attempts"]) >= self.cfg.max_run_attempts:
                    error = f"Gave up after {run['attempts']} attempts (the worker lost its lease each time)."
                    await self._finish_in(conn, run, JobStatus.FAILED, error)
                    out.append((str(run["id"]), "FAILED"))
                elif await self.store.transition(conn, str(run["id"]), JobStatus.QUEUED, "lease expired"):
                    out.append((str(run["id"]), "QUEUED"))
        return out

    async def calibrate_next(self) -> str | None:
        """Claims one queued judge calibration and runs it."""
        from agenttwin_evaluation.calibrations import run_calibration

        row = await self.store.claim_calibration(self.owner, max(self.cfg.lease_seconds, 600.0))
        if row is None:
            return None
        done = await run_calibration(self.store, self.judge, row, self.owner)
        if done is not None:
            self.log.info("judge calibrated", calibration_id=str(row["id"]), status=done["status"])
        return str(row["id"])

    # ------------------------------------------------------------ loops

    async def calibration_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                busy = await self.calibrate_next()
            except Exception as err:  # noqa: BLE001 - keep calibrating
                self.log.error("judge calibration failed", error=repr(err))
                busy = None
            if busy is None:
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), self.cfg.poll_interval_s)

    async def on_event(self, env: Envelope) -> None:
        """``simulation.run_completed.v1`` makes the waiting run due at once;
        the other events of the queue are for later phases."""
        if env.type != "simulation.run_completed.v1":
            return
        payload = env.payload
        async with transaction(self.store.pool) as conn:
            await self.store.wake(
                conn, eval_run_id=payload.get("eval_run_id"), simulation_run_id=str(payload["run_id"])
            )

    async def claim_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                busy = await self.process_next()
            except Exception as err:  # noqa: BLE001 - keep claiming
                self.log.error("eval run preparation failed", error=repr(err))
                busy = None
            if busy is None:
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), self.cfg.poll_interval_s)

    async def wait_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.check_waiting()
            except Exception as err:  # noqa: BLE001 - keep checking
                self.log.error("eval run checks failed", error=repr(err))
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), min(1.0, self.cfg.poll_interval_s))

    async def janitor_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                for run_id, status in await self.recover_expired():
                    self.log.warn("eval run recovered", eval_run_id=run_id, status=status)
            except Exception as err:  # noqa: BLE001 - keep recovering
                self.log.error("eval run recovery failed", error=repr(err))
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), max(1.0, self.cfg.lease_seconds / 2))
