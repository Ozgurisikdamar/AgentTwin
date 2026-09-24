"""The simulation worker (spec §23, §86, ADR-0006).

The database is the queue: a worker claims a QUEUED run with ``FOR UPDATE
SKIP LOCKED``, holds a renewable lease while it executes the run's cases one
by one, and moves the run through PREPARING → RUNNING → EVALUATING →
COMPLETED with guarded transitions. ``simulation.run_requested.v1`` events
only wake workers up early; a lost event costs one poll interval, never a run.

For every case the worker prepares an isolated twin state, gives the agent a
capability token for the twin endpoint, calls the agent through the adapter
contract, revokes the token, evaluates the scenario's expectations against
what the twin recorded and stores the verdict with its evidence. A worker
that dies loses its lease; the janitor puts the run back in the queue and its
in-flight case restarts from a fresh twin.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import os
import secrets
import socket
import time
from collections.abc import Mapping
from typing import Any, Literal

from agenttwin_core.db import Conn, transaction
from agenttwin_core.evaluators import EvaluationResult, Registry, case_verdict, evaluate_all, skip_all
from agenttwin_core.events import Envelope, claim_event, write_outbox
from agenttwin_core.ids import new_id
from agenttwin_core.jobs import JobStatus
from agenttwin_core.logx import Log
from agenttwin_simulation.cases import (
    agent_request,
    evaluation_context,
    initial_state,
    outcome_body,
    synthetic_result,
    valid_trace_id,
)
from agenttwin_simulation.clients import AgentCall, AgentClient, TraceServiceClient
from agenttwin_simulation.config import AgentEndpoint, SimulationConfig
from agenttwin_simulation.metrics import SimulationMetrics
from agenttwin_simulation.store import SCHEMA, Row, Store
from agenttwin_simulation.twin.adapters import AdapterRegistry
from agenttwin_simulation.twin.definition import load_twin
from agenttwin_simulation.twin.engine import CaseState, diff_state
from agenttwin_simulation.twin_http import token_hash

__all__ = [
    "CONSUMER",
    "PRODUCER",
    "QUEUE",
    "Worker",
    "agent_result_json",
    "completed_event",
    "run_findings",
    "verdict_json",
]

PRODUCER = "simulation-service"
CONSUMER = "simulation-service.worker"
QUEUE = "simulation-service.events"
MAX_OUTPUT_CHARS = 20_000
CaseEnd = Literal["done", "cancelled", "lost"]


def verdict_json(v: Any) -> dict[str, Any]:
    return {
        "status": v.status,
        "reason": v.reason,
        "passed": v.passed,
        "failed": v.failed,
        "errored": v.errored,
        "skipped": v.skipped,
        "critical_failures": v.critical_failures,
        "score": v.score,
        "labels": list(v.labels),
    }


def completed_event(run: Mapping[str, Any]) -> Envelope:
    pinning = run.get("pinning") or {}
    return Envelope.new(
        "simulation.run_completed.v1",
        PRODUCER,
        str(run["organization_id"]),
        str(run["project_id"]),
        str(pinning.get("correlation_id") or run["id"]),
        {
            "run_id": str(run["id"]),
            "eval_run_id": run.get("eval_run_id"),
            "side": run.get("side") or "SINGLE",
            "status": run["status"],
            "case_count": int(run["case_count"]),
            "error": run.get("error"),
            "agent": run["agent_name"],
            "agent_version": run["agent_version"],
            "passed": int(run.get("passed") or 0),
            "failed": int(run.get("failed") or 0),
            "errored": int(run.get("errored") or 0),
            "cancelled": int(run.get("cancelled") or 0),
            "critical_failures": int(run.get("critical_failures") or 0),
        },
    )


def _truncate(value: Any, limit: int) -> Any:
    return value[:limit] if isinstance(value, str) else value


def agent_result_json(call: AgentCall) -> dict[str, Any]:
    """What is kept of the agent's answer (bounded; never the credentials)."""
    out: dict[str, Any] = {"kind": call.kind, "http_status": call.status_code, "elapsed_ms": call.elapsed_ms}
    if call.error:
        out["error"] = call.error
    body = call.body or {}
    for key in (
        "output",
        "trace_id",
        "status",
        "agent_version",
        "model",
        "model_kind",
        "steps",
        "claimed_outcome",
        "business_outcome",
    ):
        if key in body:
            out[key] = _truncate(body[key], MAX_OUTPUT_CHARS if key == "output" else 200)
    calls = body.get("tool_calls")
    if isinstance(calls, list):
        out["tool_calls"] = calls[:100]  # the agent's own account, for comparison with the twin's records
    problem = body.get("error")
    if not call.ok and isinstance(problem, Mapping):
        out["agent_error"] = {
            "code": str(problem.get("code") or "")[:100],
            "message": str(problem.get("message") or "")[:500],
        }
    return out


def run_findings(call: AgentCall, state: CaseState, max_calls: int) -> list[EvaluationResult]:
    """Results about the run itself, next to the scenario's expectations."""
    found: list[EvaluationResult] = []
    if call.kind == "timeout":
        found.append(
            synthetic_result(
                "FAIL", "AGENT_TIMEOUT", f"The agent did not finish the scenario in time ({call.error})."
            )
        )
    elif call.kind == "unreachable":
        found.append(
            synthetic_result("ERROR", "AGENT_UNREACHABLE", f"The agent could not be run: {call.error}.")
        )
    elif call.kind in ("http_error", "invalid_response"):
        code = call.status_code or 0
        agent_code = str(((call.body or {}).get("error") or {}).get("code") or "")
        if code in (401, 403):
            found.append(
                synthetic_result(
                    "ERROR",
                    "AGENT_AUTH",
                    "The agent rejected the simulation's credentials (check token_env in "
                    "SIMULATION_AGENT_ENDPOINTS).",
                )
            )
        elif code == 404 and agent_code == "UNKNOWN_VERSION":
            found.append(
                synthetic_result(
                    "ERROR",
                    "AGENT_VERSION_UNAVAILABLE",
                    f"The agent deployment cannot run this version: {call.error}.",
                )
            )
        elif code in (400, 413, 415, 422):
            found.append(
                synthetic_result(
                    "ERROR", "AGENT_REJECTED_REQUEST", f"The agent rejected the run request: {call.error}."
                )
            )
        else:
            found.append(
                synthetic_result(
                    "FAIL", "AGENT_ERROR", f"The agent failed during the scenario: {call.error}."
                )
            )
    if state.seq >= max_calls:
        found.append(
            synthetic_result(
                "FAIL",
                "CALL_LIMIT_EXCEEDED",
                f"The agent used all {max_calls} twin calls allowed per case; later calls were refused.",
            )
        )
    return found


class Worker:
    def __init__(
        self,
        *,
        store: Store,
        cfg: SimulationConfig,
        agents: AgentClient,
        traces: TraceServiceClient,
        registry: Registry,
        log: Log,
        adapters: AdapterRegistry | None = None,
        metrics: SimulationMetrics | None = None,
        owner: str | None = None,
    ) -> None:
        self.store = store
        self.cfg = cfg
        self.agents = agents
        self.traces = traces
        self.registry = registry
        self.log = log
        self.adapters = adapters or AdapterRegistry()
        self.metrics = metrics
        self.owner = owner or f"{socket.gethostname()}:{os.getpid()}:{new_id()[-12:]}"
        self.wake = asyncio.Event()

    # ------------------------------------------------------------ loops

    @staticmethod
    async def _pause(stop: asyncio.Event, seconds: float, wake: asyncio.Event | None = None) -> None:
        waiters = [asyncio.ensure_future(stop.wait())]
        if wake is not None:
            waiters.append(asyncio.ensure_future(wake.wait()))
        try:
            await asyncio.wait(waiters, timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for w in waiters:
                w.cancel()

    async def claim_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                run_id = await self.process_next()
            except Exception as err:  # noqa: BLE001 - the loop must survive any failure
                self.log.warn("claiming a simulation run failed", error=str(err))
                run_id = None
            if run_id is None:
                await self._pause(stop, self.cfg.poll_interval_s, self.wake)
                self.wake.clear()

    async def janitor_loop(self, stop: asyncio.Event) -> None:
        interval = max(1.0, min(15.0, self.cfg.lease_seconds / 4))
        while not stop.is_set():
            try:
                await self.recover()
                if self.metrics is not None:
                    self.metrics.queue_depth.labels(self.metrics.service).set(await self.store.queue_depth())
            except Exception as err:  # noqa: BLE001 - the janitor must survive any failure
                self.log.warn("simulation janitor failed", error=str(err))
            await self._pause(stop, interval)

    async def outcome_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                n = await self.post_outcomes()
            except Exception as err:  # noqa: BLE001 - the poster must survive any failure
                self.log.warn("posting verified outcomes failed", error=str(err))
                n = 0
            if n == 0:
                await self._pause(stop, min(2.0, self.cfg.outcome_retry_s))

    async def on_event(self, env: Envelope) -> None:
        """``simulation.run_requested.v1`` wakes the claim loops up early."""
        if env.type != "simulation.run_requested.v1":
            return
        async with transaction(self.store.pool) as conn:
            await claim_event(conn, CONSUMER, env.id)
        self.wake.set()

    # ------------------------------------------------------------ runs

    async def process_next(self) -> str | None:
        run = await self.store.claim_next_run(self.owner, self.cfg.lease_seconds)
        if run is None:
            return None
        await self.execute(run)
        return str(run["id"])

    async def _renew(self, run_id: str, lost: asyncio.Event) -> None:
        interval = max(0.5, self.cfg.lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            try:
                ok = await self.store.renew_lease(run_id, self.owner, self.cfg.lease_seconds)
            except Exception as err:  # noqa: BLE001 - retried at the next interval
                self.log.warn("lease renewal failed", run_id=run_id, error=str(err))
                continue
            if not ok:
                self.log.warn("lost the lease of a simulation run", run_id=run_id)
                lost.set()
                return

    async def execute(self, run: Row) -> None:
        run_id = str(run["id"])
        lost = asyncio.Event()
        renewer = asyncio.create_task(self._renew(run_id, lost))
        try:
            await self._execute(run, lost)
        except Exception as err:
            self.log.exception("simulation run failed", run_id=run_id, error=str(err))
            await self._finish_run(run_id, JobStatus.FAILED, f"internal error ({type(err).__name__})")
        finally:
            renewer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await renewer

    async def _execute(self, run: Row, lost: asyncio.Event) -> None:
        run_id = str(run["id"])
        endpoint = self.cfg.agent_endpoints.get(run["agent_name"])
        if endpoint is None:
            await self._finish_run(
                run_id,
                JobStatus.FAILED,
                f"No endpoint is configured for agent {run['agent_name']!r} (SIMULATION_AGENT_ENDPOINTS).",
            )
            return
        async with transaction(self.store.pool) as conn:
            if (
                await self.store.transition(
                    conn, run_id, JobStatus.RUNNING, "executing cases", owner=self.owner
                )
                is None
            ):
                return  # cancelled or the lease moved on
        self.log.info(
            "simulation run started", run_id=run_id, agent=run["agent_name"], version=run["agent_version"]
        )
        for case in await self.store.pending_cases(run_id):
            if lost.is_set():
                return
            if await self.store.cancel_requested(run_id):
                await self._finish_run(run_id, JobStatus.CANCELLED, "cancelled on request")
                return
            end = await self.run_case(run, case, endpoint, lost)
            if end == "lost":
                return
            if end == "cancelled":
                await self._finish_run(run_id, JobStatus.CANCELLED, "cancelled on request")
                return
        await self._finish_run(run_id, JobStatus.COMPLETED)

    async def _finish_run(self, run_id: str, to: JobStatus, reason: str | None = None) -> None:
        async with transaction(self.store.pool) as conn:
            if to is JobStatus.COMPLETED and await self.store.lock_cancel_requested(conn, run_id):
                # A cancellation acknowledged before the run ended is honoured
                # even if its last case finished meanwhile; the row lock orders
                # this check against request_cancel.
                to, reason = JobStatus.CANCELLED, "cancelled on request"
            if to is JobStatus.COMPLETED:
                if (
                    await self.store.transition(
                        conn, run_id, JobStatus.EVALUATING, "all cases executed", owner=self.owner
                    )
                    is None
                ):
                    return
                counted = await self.store.refresh_counts(conn, run_id)
                if counted is not None:
                    reason = (
                        f"{counted['passed']} passed, {counted['failed']} failed, "
                        f"{counted['errored']} errored ({counted['critical_failures']} critical failures)"
                    )
            else:
                await self.store.cancel_pending_cases(
                    conn,
                    run_id,
                    "The run was cancelled." if to is JobStatus.CANCELLED else f"The run failed: {reason}",
                )
                await self.store.refresh_counts(conn, run_id)
            row = await self.store.transition(
                conn,
                run_id,
                to,
                reason,
                owner=self.owner,
                extra={"error": reason} if to is JobStatus.FAILED else None,
            )
            if row is not None:
                await write_outbox(conn, SCHEMA, completed_event(row))
        if row is not None:
            self.log.info("simulation run finished", run_id=run_id, status=str(to), summary=reason)
            if self.metrics is not None:
                self.metrics.run(str(to))

    async def _on_recovered_failure(self, conn: Conn, run: Row) -> None:
        await write_outbox(conn, SCHEMA, completed_event(run))

    async def recover(self) -> list[tuple[str, str]]:
        recovered = await self.store.recover_expired(self.cfg.max_run_attempts, self._on_recovered_failure)
        for run_id, status in recovered:
            self.log.warn("recovered a simulation run with an expired lease", run_id=run_id, status=status)
            if status in ("QUEUED",):
                self.wake.set()
            elif self.metrics is not None:
                self.metrics.run(status)
        return recovered

    # ------------------------------------------------------------ cases

    async def _watch(self, task: asyncio.Task[AgentCall], run_id: str, lost: asyncio.Event) -> CaseEnd | None:
        while True:
            done, _ = await asyncio.wait({task}, timeout=1.0)
            if done:
                return None
            reason: CaseEnd | None = None
            if lost.is_set():
                reason = "lost"
            elif await self.store.cancel_requested(run_id):
                reason = "cancelled"
            if reason is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                return reason

    async def run_case(self, run: Row, case: Row, endpoint: AgentEndpoint, lost: asyncio.Event) -> CaseEnd:
        started = time.perf_counter()
        case_id, run_id = str(case["id"]), str(run["id"])
        doc = case["scenario_document"]
        spec = doc["spec"]
        definition = load_twin(case["twin_document"], adapters=self.adapters.names())
        before = initial_state(definition, doc)
        token = secrets.token_urlsafe(32)
        await self.store.start_case(case_id, token_hash(token), CaseState.fresh(before).to_json())
        body = agent_request(doc=doc, run=run, case=case, twin_url=self.cfg.twin_public_url, token=token)
        timeout = float(spec.get("timeoutSeconds") or self.cfg.case_timeout_s)
        task = asyncio.create_task(
            self.agents.run(endpoint.url, self.cfg.agent_tokens.get(run["agent_name"]), body, timeout)
        )
        interrupted = await self._watch(task, run_id, lost)
        # Revoke the case's twin credential first: nothing may change the
        # twin state after this point (late or replayed agent calls included).
        closed = await self.store.close_case(case_id)
        if interrupted is None and await self.store.cancel_requested(run_id):
            # The agent finished, but after the run was cancelled: the twin
            # refused its calls from then on, so its result is not a verdict.
            # (Checked after the credential is revoked: a request made later
            # cannot have touched this case.)
            interrupted = "cancelled"
        if interrupted == "lost":
            return "lost"
        if interrupted == "cancelled":
            async with transaction(self.store.pool) as conn:
                await self.store.finish_case(
                    conn,
                    case_id,
                    {"status": "CANCELLED", "reason": "The run was cancelled during this case."},
                )
                await self.store.refresh_counts(conn, run_id)
            return "cancelled"
        call = task.result()
        raw_state = (closed or {}).get("twin_state")
        twin = CaseState.from_json(raw_state) if raw_state else CaseState.fresh(before)
        after = twin.state
        steps = await self.store.case_steps(case_id)
        agent_result = agent_result_json(call)
        ctx = evaluation_context(
            doc=doc,
            definition=definition,
            agent_result=agent_result if call.ok else None,
            steps=steps,
            state_before=before,
            state_after=after,
            tenant=case["tenant"],
            latency_ms=call.elapsed_ms,
        )
        findings = run_findings(call, twin, self.cfg.max_calls_per_case)
        blocked = next((f for f in findings if f.status == "ERROR"), None)
        if blocked is not None:
            # The agent never ran the scenario (unreachable, misconfigured,
            # rejected the request): judging the untouched twin state would
            # turn an infrastructure problem into a pass or a regression.
            expectations = skip_all(spec["expectations"], "The agent did not run, so this was not evaluated.")
        else:
            expectations = await evaluate_all(self.registry, spec["expectations"], ctx, log=self.log)
        results = [*findings, *expectations]
        verdict = case_verdict(results)
        if blocked is not None:
            verdict = dataclasses.replace(verdict, reason=blocked.reason)
        trace_id = valid_trace_id(agent_result.get("trace_id"))
        async with transaction(self.store.pool) as conn:
            await self.store.finish_case(
                conn,
                case_id,
                {
                    "status": verdict.status,
                    "agent_result": agent_result,
                    "trace_id": trace_id,
                    "verdict": verdict_json(verdict),
                    "results": [r.to_json() for r in results],
                    "state_diff": diff_state(before, after),
                    "reason": verdict.reason[:2000],
                    "error": call.error,
                    "latency_ms": call.elapsed_ms,
                    "outcome_status": "pending" if trace_id else "none",
                },
            )
            await self.store.refresh_counts(conn, run_id)
        elapsed = time.perf_counter() - started
        self.log.info(
            "simulation case finished",
            run_id=run_id,
            case_id=case_id,
            scenario=case["scenario_name"],
            status=verdict.status,
            labels=list(verdict.labels),
            tool_calls=len(ctx.tool_calls),
            duration_ms=round(elapsed * 1000, 1),
        )
        if self.metrics is not None:
            self.metrics.case(verdict.status, elapsed)
        return "done"

    # ------------------------------------------------------------ outcomes

    async def post_outcomes(self) -> int:
        """Reports finished cases' verified outcomes to the trace service. A
        trace that is not ingested yet is retried with a growing delay."""
        due = await self.store.due_outcomes(20)
        for row in due:
            result, detail = await self.traces.record_outcome(
                str(row["organization_id"]), str(row["project_id"]), str(row["trace_id"]), outcome_body(row)
            )
            attempts = int(row["outcome_attempts"]) + 1
            if result == "posted":
                await self.store.mark_outcome(row["id"], "posted")
            elif result == "retry" and attempts < self.cfg.outcome_max_attempts:
                await self.store.mark_outcome(
                    row["id"], "pending", retry_in_s=min(self.cfg.outcome_retry_s * attempts, 60.0)
                )
            else:
                await self.store.mark_outcome(row["id"], "failed")
                self.log.warn(
                    "could not report a verified outcome",
                    case_id=row["id"],
                    trace_id=row["trace_id"],
                    attempts=attempts,
                    reason=detail,
                )
            if self.metrics is not None:
                self.metrics.outcome(result)
        return len(due)
