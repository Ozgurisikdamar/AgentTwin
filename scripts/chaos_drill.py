"""A chaos drill against the running stack (spec §64): the failures that
``make test-chaos`` injects in front of test instances, done to the real
containers of ``make dev``, with what an operator would watch.

    make chaos-drill                    # all drills, one after another
    uv run python scripts/chaos_drill.py rabbitmq postgres worker

Each drill breaks one thing, checks that the stack says so (a status, a
metric) and fails safe while it is broken, restores it, and checks that the
stack recovers on its own with nothing lost and nothing passed that should
not have been:

rabbitmq   the broker stops while a simulation runs: the run completes (it
           needs the database, not the broker), its events wait in the
           outbox (agenttwin_outbox_backlog) and are all delivered once the
           broker is back; no event reaches a dead-letter queue.
postgres   the database stops: the API answers 503 UNAVAILABLE with
           Retry-After within seconds instead of hanging or answering 500,
           and serves again once the database is back, without a restart.
worker     the simulation worker is killed in the middle of a run: the run
           is not left RUNNING; its lease brings it back and it completes
           (or fails, saying why) — never a silent pass.

It needs the stack of ``make dev`` (the demo workspace seeded) and the
demo API key in the environment (``make chaos-drill`` loads .env). It
changes nothing but container states, which it always restores.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "sdk-python" / "src"))

from agenttwin.api import APIError, Client  # noqa: E402 - the SDK from this checkout

API_URL = f"http://127.0.0.1:{os.environ.get('CONTROL_PLANE_HOST_PORT', '8080')}"
PROMETHEUS_URL = f"http://127.0.0.1:{os.environ.get('PROMETHEUS_HOST_PORT', '9090')}"
PROJECT = os.environ.get("AGENTTWIN_PROJECT", "support")
AGENT, VERSION = "support-refund-agent", "1.3.1"
# Local calls never go through an outbound proxy.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class DrillFailed(Exception):
    pass


def step(msg: str) -> None:
    print(f"  · {msg}", flush=True)


def compose(*args: str) -> str:
    out = subprocess.run(  # noqa: S603 - fixed arguments
        ["docker", "compose", *args],  # noqa: S607 - docker from PATH, as make dev uses it
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout


def until(what: str, check: Callable[[], Any], timeout_s: float, interval_s: float = 1.0) -> Any:
    """Polls ``check`` until it returns something truthy; the value is returned."""
    deadline = time.monotonic() + timeout_s
    last: Any = None
    while time.monotonic() < deadline:
        try:
            last = check()
        except (APIError, OSError, ValueError) as err:
            last = err
        if last and not isinstance(last, Exception):
            return last
        time.sleep(interval_s)
    raise DrillFailed(f"{what}: not within {timeout_s:.0f}s (last: {last!r})")


def prometheus(query: str) -> float:
    url = f"{PROMETHEUS_URL}/api/v1/query?" + urllib.parse.urlencode({"query": query})
    with _OPENER.open(url, timeout=5) as resp:
        body = json.loads(resp.read())
    result = body["data"]["result"]
    return sum(float(r["value"][1]) for r in result)


def dead_letters() -> int:
    """Messages in the dead-letter queues, from the broker itself."""
    out = compose("exec", "-T", "rabbitmq", "rabbitmqctl", "-q", "list_queues", "name", "messages")
    return sum(
        int(n) for q, n in (line.split() for line in out.splitlines() if line.strip()) if q.endswith(".dlq")
    )


def raw_get(path: str, api_key: str) -> tuple[int, dict[str, str], float]:
    """Status, headers and seconds of one API call, whatever its status."""
    req = urllib.request.Request(API_URL + path, headers={"X-AgentTwin-Api-Key": api_key})  # noqa: S310
    started = time.monotonic()
    try:
        with _OPENER.open(req, timeout=30) as resp:
            return resp.status, dict(resp.headers), time.monotonic() - started
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers), time.monotonic() - started


def healthy(services: list[str]) -> bool:
    out = compose("ps", "--format", "{{.Service}} {{.Health}}", *services)
    states = dict(line.split(maxsplit=1) for line in out.splitlines() if line.strip())
    return all(states.get(s) == "healthy" for s in services)


APP_SERVICES = [
    "control-plane",
    "trace-service",
    "graph-service",
    "runtime-gateway",
    "simulation-service",
    "simulation-worker",
    "evaluation-service",
    "evaluation-worker",
]


# ------------------------------------------------------------------ drills


def drill_rabbitmq(client: Client, project_id: str) -> None:
    dlq_before = dead_letters()
    compose("stop", "rabbitmq")
    try:
        step("rabbitmq stopped; starting a simulation run")
        run = client.start_simulation(project_id, AGENT, VERSION, scenarios=["refund-happy-path"])
        run_id = run["run"]["id"]
        done = client.wait_for_simulation(run_id, timeout_s=180)
        if done["run"]["status"] != "COMPLETED":
            raise DrillFailed(f"the run did not complete without the broker: {done['run']['status']}")
        step(f"run {run_id} completed without the broker")
        backlog = until(
            "the outbox backlog shows the waiting events",
            lambda: prometheus("sum(agenttwin_outbox_backlog)"),
            60,
            3,
        )
        step(f"agenttwin_outbox_backlog = {backlog:.0f} (events waiting for the broker)")
    finally:
        compose("start", "rabbitmq")
    step("rabbitmq started")
    until("the outbox drains", lambda: prometheus("sum(agenttwin_outbox_backlog)") == 0, 180, 3)
    step("agenttwin_outbox_backlog = 0: every waiting event was published")
    until("every service is healthy again", lambda: healthy(APP_SERVICES), 120, 3)
    dlq_after = dead_letters()
    if dlq_after > dlq_before:
        raise DrillFailed(f"{dlq_after - dlq_before} event(s) reached a dead-letter queue during the outage")
    step("no event reached a dead-letter queue")


def drill_postgres(client: Client, project_id: str, api_key: str) -> None:
    del project_id
    compose("stop", "postgres")
    try:
        step("postgres stopped")
        status, headers, took = until(
            "the API answers 503", lambda: (r := raw_get("/api/v1/projects", api_key))[0] == 503 and r, 30, 1
        )
        if took > 8:
            raise DrillFailed(f"the 503 took {took:.1f}s: callers must not hang on an outage")
        if headers.get("Retry-After") is None:
            raise DrillFailed("the 503 carries no Retry-After")
        step(f"GET /api/v1/projects -> {status} in {took:.1f}s, Retry-After: {headers['Retry-After']}")
    finally:
        compose("start", "postgres")
    step("postgres started")
    until("the API serves again", lambda: raw_get("/api/v1/projects", api_key)[0] == 200, 90, 2)
    step("GET /api/v1/projects -> 200 again, no service restarted")
    until("every service is healthy again", lambda: healthy(APP_SERVICES), 180, 3)
    client.projects()


def drill_worker(client: Client, project_id: str) -> None:
    run = client.start_simulation(project_id, AGENT, VERSION)
    run_id = run["run"]["id"]
    until(
        "the run is being worked on",
        lambda: any(c["status"] == "RUNNING" for c in client.simulation(run_id)["cases"]),
        120,
        0.5,
    )
    compose("kill", "simulation-worker")
    step(f"simulation-worker killed while run {run_id} was running")
    compose("start", "simulation-worker")
    step("simulation-worker started again; waiting for the lease to bring the run back")
    done = client.wait_for_simulation(run_id, timeout_s=600, interval_s=3)
    status, attempts = done["run"]["status"], done["run"]["attempts"]
    if status == "COMPLETED":
        if attempts < 2:
            raise DrillFailed("the run completed in one attempt: the kill did not interrupt it")
        r = done["run"]
        step(f"run completed after {attempts} attempts: {r['passed']} passed, {r['failed']} failed")
    elif status == "FAILED":
        if not done["run"].get("error"):
            raise DrillFailed("the run failed without saying why")
        step(f"run failed, saying why: {done['run']['error']}")
    else:
        raise DrillFailed(f"the run ended {status}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "drills", nargs="*", choices=["rabbitmq", "postgres", "worker"], help="default: all, in this order"
    )
    args = parser.parse_args(argv)
    drills = args.drills or ["rabbitmq", "postgres", "worker"]
    api_key = os.environ.get("AGENTTWIN_DEMO_API_KEY", "")
    if not api_key:
        print("AGENTTWIN_DEMO_API_KEY is not set (make chaos-drill loads .env)", file=sys.stderr)
        return 2
    client = Client(API_URL, api_key, timeout_s=30)
    client.wait_ready(timeout_s=30)
    project_id = client.project_id(PROJECT)
    runs: dict[str, Callable[[], None]] = {
        "rabbitmq": lambda: drill_rabbitmq(client, project_id),
        "postgres": lambda: drill_postgres(client, project_id, api_key),
        "worker": lambda: drill_worker(client, project_id),
    }
    failed = []
    for name in drills:
        print(f"drill: {name}", flush=True)
        started = time.monotonic()
        try:
            runs[name]()
        except (DrillFailed, APIError, subprocess.CalledProcessError) as err:
            print(f"  FAILED: {err}", file=sys.stderr, flush=True)
            failed.append(name)
            continue
        print(f"  ok ({time.monotonic() - started:.0f}s)", flush=True)
    if failed:
        print("chaos drill failed:", ", ".join(failed), file=sys.stderr)
        return 1
    print(f"chaos drill passed: {', '.join(drills)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
