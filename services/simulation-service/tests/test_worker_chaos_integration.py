"""Chaos tests (spec §64) for the simulation worker: a bug in the middle of a
run fails it at once and says so; the database going away in the middle of
a run leaves it to be retried, not failed; a worker killed mid-run is
recovered by another (test_run_lifecycle_integration covers the lease); an
agent that hangs or answers something that is not a run result fails its
case, bounded by the case timeout, and never passes; a tool twin that crashes
answers 500, keeps its state, and errors the case instead of letting it be
judged; an evaluator that crashes errors its expectation and the case, and
the others are still judged."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from psycopg import OperationalError

from agenttwin_core.evaluators import default_registry
from agenttwin_simulation import twin_http
from agenttwin_simulation.clients import AgentClient
from agenttwin_simulation.config import AgentEndpoint
from agenttwin_simulation.runner import Worker
from agenttwin_simulation.twin.engine import DeclarativeTwin
from sim_testutil import AGENT, running_cases, simulation_stack

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


def http_answer(status: str, body: bytes, content_type: str) -> bytes:
    head = f"HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\nContent-Length: {len(body)}\r\n"
    return head.encode() + b"Connection: close\r\n\r\n" + body


@asynccontextmanager
async def raw_agent(answer: bytes | None) -> AsyncIterator[str]:
    """An agent endpoint that answers every run with the given bytes, or, for
    None, reads the request and never answers (a hung agent)."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readuntil(b"\r\n\r\n")
            if answer is None:
                await reader.read()  # until the caller gives up and closes
            else:
                writer.write(answer)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize(
    ("answer", "label", "kind"),
    [
        pytest.param(None, "AGENT_TIMEOUT", "timeout", id="hangs"),
        pytest.param(
            http_answer("200 OK", b"<html>502 Bad Gateway</html>", "text/html"),
            "AGENT_ERROR",
            "invalid_response",
            id="html",
        ),
        pytest.param(
            http_answer("200 OK", b'{"output": "Refund issued", "steps"', "application/json"),
            "AGENT_ERROR",
            "invalid_response",
            id="truncated-json",
        ),
        pytest.param(
            http_answer("200 OK", b"[]", "application/json"), "AGENT_ERROR", "invalid_response", id="array"
        ),
        pytest.param(
            http_answer("500 Internal Server Error", b"boom", "text/plain"),
            "AGENT_ERROR",
            "http_error",
            id="500",
        ),
    ],
)
async def test_an_agent_that_hangs_or_answers_garbage_fails_its_case(
    answer: bytes | None, label: str, kind: str
) -> None:
    async with simulation_stack(case_timeout_s=0.5) as s, raw_agent(answer) as url:
        await s.register_demo(HAPPY)
        run_id = await s.start_run("1.2.4", HAPPY)
        # Not the contract-checking client: these answers break the contract on purpose.
        agents, s.worker.agents = s.worker.agents, AgentClient()
        s.cfg.agent_endpoints[AGENT] = AgentEndpoint(url=url, token_env="DEMO_AGENT_TOKEN")
        try:
            started = time.perf_counter()
            assert await s.worker.process_next() == run_id
            elapsed = time.perf_counter() - started
        finally:
            await s.worker.agents.close()
            s.worker.agents = agents
        # Bounded: a hung agent costs the case timeout, not the worker.
        assert elapsed < 5.0
        detail = await s.ok("GET", f"/api/v1/simulations/{run_id}")
        [case] = detail["cases"]
        assert (detail["run"]["status"], case["status"]) == ("COMPLETED", "FAILED")
        assert (detail["run"]["passed"], detail["run"]["failed"]) == (0, 1)
        full = await s.ok("GET", f"/api/v1/simulations/{run_id}/cases/{case['id']}")
        first = full["case"]["results"][0]
        assert (first["status"], first["label"], first["critical"]) == ("FAIL", label, True)
        assert full["case"]["agent_result"]["kind"] == kind
        assert full["case"]["verdict"]["status"] != "PASS"
        events = await s.outbox("simulation.run_completed.v1")
        assert [(e["payload"]["run_id"], e["payload"]["status"]) for e in events] == [(run_id, "COMPLETED")]


def crash_on(monkeypatch: pytest.MonkeyPatch, call: int, *more: tuple[int, Exception]) -> list[str]:
    """Makes the twin engine raise KeyError on its ``call``-th invocation (a
    bug in the twin), and the given exceptions on later ones; returns the
    tools called so far."""
    real = DeclarativeTwin.invoke
    seen: list[str] = []
    bugs: dict[int, Exception] = {call: KeyError("orders"), **dict(more)}

    async def invoke(self: DeclarativeTwin, tool: str, *args: Any, **kwargs: Any) -> Any:
        seen.append(tool)
        if len(seen) in bugs:
            raise bugs[len(seen)]
        return await real(self, tool, *args, **kwargs)

    monkeypatch.setattr(DeclarativeTwin, "invoke", invoke)
    return seen


async def test_a_twin_crash_answers_500_and_leaves_the_case_state_alone(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async with simulation_stack() as s:
        await s.register_demo(HAPPY)
        run_id = await s.start_run("1.2.4", HAPPY)
        case_id = (await running_cases(s, run_id, {HAPPY: "tok-A"}))[HAPPY]
        headers = {"Authorization": "Bearer tok-A"}
        refund = {"order_id": "ORD-1001", "amount": 40, "idempotency_key": "k1"}
        crash_on(monkeypatch, 2, (4, ValueError("amount")))
        r = await s.client.post("/twin/v1/tools/lookup_order", json={"order_id": "ORD-1001"}, headers=headers)
        assert r.status_code == 200
        before = await s.store.one(
            "SELECT twin_state, call_count, error FROM simulation_case WHERE id = %s", (case_id,)
        )
        r = await s.client.post("/twin/v1/tools/refund_payment", json=refund, headers=headers)
        # A visible failure with the request id to find it in the logs, never a result.
        assert (r.status_code, r.json()["error"]["code"]) == (500, "INTERNAL")
        assert r.headers["x-request-id"] and r.json()["error"]["request_id"] == r.headers["x-request-id"]
        [logged] = [rec for rec in caplog.records if rec.getMessage() == "unhandled error"]
        assert logged.__dict__["attrs"]["error"] == "KeyError" and logged.exc_info is not None
        after = await s.store.one(
            "SELECT twin_state, call_count, error FROM simulation_case WHERE id = %s", (case_id,)
        )
        assert before is not None and after is not None
        # Nothing of the failed call was kept; the case knows the twin failed.
        assert (after["twin_state"], after["call_count"]) == (before["twin_state"], before["call_count"])
        assert before["error"] is None
        assert after["error"] == "the tool twin failed (KeyError)"
        assert len(await s.store.case_steps(case_id)) == 1
        # The twin keeps working: the same call now applies once, numbered as
        # if the failed one never happened (it took no sequence number).
        r = await s.client.post("/twin/v1/tools/refund_payment", json=refund, headers=headers)
        assert r.status_code == 200 and r.json()["result"]["refund_id"] == "RF-0002"
        assert [step["seq"] for step in await s.store.case_steps(case_id)] == [1, 2]
        state = await s.store.one("SELECT twin_state, error FROM simulation_case WHERE id = %s", (case_id,))
        assert state is not None and state["twin_state"]["state"]["orders"]["ORD-1001"]["refund_count"] == 1
        assert state["error"] == "the tool twin failed (KeyError)"  # the first failure is kept
        # A second, different failure: the first one is what the case reports.
        r = await s.client.post("/twin/v1/tools/lookup_order", json={"order_id": "ORD-1001"}, headers=headers)
        assert r.status_code == 500
        state = await s.store.one("SELECT error FROM simulation_case WHERE id = %s", (case_id,))
        assert state is not None and state["error"] == "the tool twin failed (KeyError)"

        # The worker holding the run dies; another one runs the case again from
        # the start, and nothing of the earlier twin failure follows it.
        await s.store.one(
            "UPDATE simulation_run SET lease_expires_at = now() - interval '1 second' "
            "WHERE id = %s RETURNING id",
            (run_id,),
        )
        assert await s.worker.recover() == [(run_id, "QUEUED")]
        assert await s.worker.process_next() == run_id
        detail = await s.ok("GET", f"/api/v1/simulations/{run_id}")
        assert (detail["run"]["passed"], detail["cases"][0]["status"]) == (1, "PASSED")
        full = await s.ok("GET", f"/api/v1/simulations/{run_id}/cases/{case_id}")
        assert full["case"]["error"] is None


async def test_refusals_are_answers_not_twin_failures() -> None:
    async with simulation_stack(max_calls_per_case=1) as s:
        await s.register_demo(HAPPY)
        run_id = await s.start_run("1.2.4", HAPPY)
        case_id = (await running_cases(s, run_id, {HAPPY: "tok-A"}))[HAPPY]
        headers = {"Authorization": "Bearer tok-A"}
        order = {"order_id": "ORD-1001"}
        r = await s.client.post("/twin/v1/tools/lookup_order", json=order, headers=headers)
        assert r.status_code == 200
        r = await s.client.post("/twin/v1/tools/lookup_order", json=order, headers=headers)
        assert (r.status_code, r.json()["error"]["code"]) == (409, "CALL_LIMIT_EXCEEDED")
        r = await s.client.get("/twin/v1/kb/search", params={"q": "refund"}, headers=headers)
        assert (r.status_code, r.json()["error"]["code"]) == (409, "CALL_LIMIT_EXCEEDED")
        state = await s.store.one("SELECT error FROM simulation_case WHERE id = %s", (case_id,))
        assert state is not None and state["error"] is None


async def test_a_twin_crash_during_a_retrieval_is_a_twin_failure_too(monkeypatch: pytest.MonkeyPatch) -> None:
    async with simulation_stack() as s:
        await s.register_demo(HAPPY)
        run_id = await s.start_run("1.2.4", HAPPY)
        case_id = (await running_cases(s, run_id, {HAPPY: "tok-A"}))[HAPPY]

        def broken(*_: Any, **__: Any) -> Any:
            raise IndexError("documents")

        monkeypatch.setattr(twin_http, "search", broken)
        r = await s.client.get(
            "/twin/v1/kb/search", params={"q": "refund"}, headers={"Authorization": "Bearer tok-A"}
        )
        assert (r.status_code, r.json()["error"]["code"]) == (500, "INTERNAL")
        state = await s.store.one("SELECT error, twin_state FROM simulation_case WHERE id = %s", (case_id,))
        assert state is not None and state["error"] == "the tool twin failed (IndexError)"
        assert state["twin_state"]["seq"] == 0 and await s.store.case_steps(case_id) == []


async def test_a_twin_crash_errors_the_case_instead_of_judging_it(monkeypatch: pytest.MonkeyPatch) -> None:
    async with simulation_stack() as s:
        await s.register_demo(HAPPY)
        run_id = await s.start_run("1.2.4", HAPPY)
        seen = crash_on(monkeypatch, 2)
        assert await s.worker.process_next() == run_id
        assert len(seen) >= 2  # the agent reached the crash and carried on
        detail = await s.ok("GET", f"/api/v1/simulations/{run_id}")
        [case] = detail["cases"]
        assert (detail["run"]["status"], case["status"]) == ("COMPLETED", "ERRORED")
        assert (detail["run"]["passed"], detail["run"]["errored"]) == (0, 1)
        full = await s.ok("GET", f"/api/v1/simulations/{run_id}/cases/{case['id']}")
        first, *rest = full["case"]["results"]
        assert (first["status"], first["label"], first["critical"]) == ("ERROR", "TWIN_ERROR", True)
        assert "the tool twin failed (KeyError)" in first["reason"]
        assert full["case"]["reason"] == first["reason"]
        assert full["case"]["error"] == "the tool twin failed (KeyError)"
        expectations = [r for r in rest if r["evaluator"] != "simulation.agent_run"]
        assert expectations and {(r["status"], r["reason"]) for r in expectations} == {
            ("SKIPPED", "The tool twin failed during the case, so this was not evaluated.")
        }

        # The same run again, without the bug: nothing of the failure is left.
        monkeypatch.undo()
        again = await s.start_run("1.2.4", HAPPY)
        assert await s.worker.process_next() == again
        detail = await s.ok("GET", f"/api/v1/simulations/{again}")
        assert (detail["run"]["passed"], detail["cases"][0]["status"]) == (1, "PASSED")
        [case] = detail["cases"]
        full = await s.ok("GET", f"/api/v1/simulations/{again}/cases/{case['id']}")
        assert full["case"]["error"] is None


async def test_the_database_going_away_during_a_case_abandons_it_and_the_agent_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with simulation_stack(lease_seconds=0.5) as s:
        await s.register_demo(HAPPY)
        run_id = await s.start_run("1.2.4", HAPPY)
        agent_call = asyncio.Event()
        cancelled = asyncio.Event()
        real_run = s.worker.agents.run

        async def slow_agent(*args: Any, **kwargs: Any) -> Any:
            agent_call.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return await real_run(*args, **kwargs)

        real_cancel_requested = s.store.cancel_requested

        async def outage(run: str) -> bool:
            if not agent_call.is_set():
                return await real_cancel_requested(run)
            raise OperationalError("consuming input failed: server closed the connection unexpectedly")

        monkeypatch.setattr(s.worker.agents, "run", slow_agent)
        monkeypatch.setattr(s.store, "cancel_requested", outage)
        assert await s.worker.process_next() == run_id
        assert agent_call.is_set()
        await asyncio.wait_for(cancelled.wait(), 5)  # not left running for nobody
        monkeypatch.undo()
        detail = await s.ok("GET", f"/api/v1/simulations/{run_id}")
        # Left to its lease: no verdict was made from a case the worker could not watch.
        assert detail["run"]["status"] == "RUNNING" and detail["cases"][0]["status"] == "RUNNING"
        for _ in range(100):
            if await s.worker.recover() == [(run_id, "QUEUED")]:
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("the run was never put back in the queue")
        assert await s.worker.process_next() == run_id
        detail = await s.ok("GET", f"/api/v1/simulations/{run_id}")
        assert (detail["run"]["status"], detail["run"]["passed"]) == ("COMPLETED", 1)


async def test_a_twin_failure_is_recorded_through_a_database_blip_or_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async with simulation_stack() as s:
        await s.register_demo(HAPPY)
        run_id = await s.start_run("1.2.4", HAPPY)
        case_id = (await running_cases(s, run_id, {HAPPY: "tok-A"}))[HAPPY]
        headers = {"Authorization": "Bearer tok-A"}
        order = {"order_id": "ORD-1001"}
        blip = OperationalError("consuming input failed: server closed the connection unexpectedly")
        monkeypatch.setattr(twin_http, "RECORD_RETRY_S", 0.01)
        real_lock, real_record = s.store.lock_case_by_token, s.store.record_twin_failure
        locks = records = 0

        async def lock_once_down(*args: Any) -> Any:
            nonlocal locks
            locks += 1
            if locks == 1:
                raise blip
            return await real_lock(*args)

        async def record_after_one_try(*args: Any) -> bool:
            nonlocal records
            records += 1
            if records == 1:
                raise blip
            return await real_record(*args)

        monkeypatch.setattr(s.store, "lock_case_by_token", lock_once_down)
        monkeypatch.setattr(s.store, "record_twin_failure", record_after_one_try)
        # The twin's database drops the call: 503 to retry, and the case knows.
        r = await s.client.post("/twin/v1/tools/lookup_order", json=order, headers=headers)
        assert (r.status_code, r.json()["error"]["code"], r.headers["retry-after"]) == (
            503,
            "UNAVAILABLE",
            "2",
        )
        assert records == 2
        state = await s.store.one("SELECT error FROM simulation_case WHERE id = %s", (case_id,))
        assert state is not None and state["error"] == "the tool twin's database was unavailable"

        # Recording stays impossible: the agent still gets its answer, and the
        # failure is logged instead of lost silently.
        records = 0

        async def always_down(*_: Any) -> bool:
            nonlocal records
            records += 1
            raise blip

        monkeypatch.setattr(s.store, "record_twin_failure", always_down)
        crash_on(monkeypatch, 1)
        r = await s.client.post("/twin/v1/tools/lookup_order", json=order, headers=headers)
        assert r.status_code == 500 and records == twin_http.RECORD_ATTEMPTS
        [logged] = [
            rec for rec in caplog.records if rec.getMessage() == "could not record a twin failure on its case"
        ]
        assert logged.__dict__["attrs"] == {
            "reason": "the tool twin failed (KeyError)",
            "error": "OperationalError",
        }


async def test_an_evaluator_that_crashes_errors_the_case_and_judges_the_rest() -> None:
    async with simulation_stack() as s:
        await s.register_demo(HAPPY)
        run_id = await s.start_run("1.2.4", HAPPY)
        registry = default_registry()
        name, version, _ = registry._checks["toolCalled"]

        def broken(*_: Any) -> Any:
            raise ZeroDivisionError("a bug in the evaluator")

        registry._checks["toolCalled"] = (name, version, broken)
        s.worker.registry = registry
        assert await s.worker.process_next() == run_id
        detail = await s.ok("GET", f"/api/v1/simulations/{run_id}")
        [case] = detail["cases"]
        assert (detail["run"]["status"], case["status"]) == ("COMPLETED", "ERRORED")
        assert (detail["run"]["passed"], detail["run"]["errored"]) == (0, 1)
        full = await s.ok("GET", f"/api/v1/simulations/{run_id}/cases/{case['id']}")
        results = full["case"]["results"]
        crashed = [r for r in results if r["label"] == "EVALUATOR_ERROR"]
        assert crashed and {(r["status"], r["reason"]) for r in crashed} == {
            ("ERROR", "The evaluator crashed (ZeroDivisionError).")
        }
        # The other expectations were still judged: the crash hides nothing
        # (the semantic one is the evaluation service's, not the simulation's).
        others = {(r["evaluator"], r["status"]) for r in results if r["label"] != "EVALUATOR_ERROR"}
        assert others == {
            ("expectation.noDuplicateSideEffect", "PASS"),
            ("expectation.noPolicyViolation", "PASS"),
            ("expectation.order", "PASS"),
            ("expectation.outcomeVerified", "PASS"),
            ("expectation.state", "PASS"),
            ("expectation.semantic", "SKIPPED"),
        }
        assert full["case"]["reason"].startswith("An expectation could not be evaluated")
