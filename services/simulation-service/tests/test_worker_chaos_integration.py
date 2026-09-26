"""Chaos tests (spec §64) for the simulation worker: a bug in the middle of a
run fails it at once and says so; the database going away in the middle of
a run leaves it to be retried, not failed; a worker killed mid-run is
recovered by another (test_run_lifecycle_integration covers the lease); an
agent that hangs or answers something that is not a run result fails its
case, bounded by the case timeout, and never passes."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from psycopg import OperationalError

from agenttwin_simulation.clients import AgentClient
from agenttwin_simulation.config import AgentEndpoint
from agenttwin_simulation.runner import Worker
from sim_testutil import AGENT, simulation_stack

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
