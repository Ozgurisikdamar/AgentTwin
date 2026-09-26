"""The load test of spec §65 against the running stack (``make dev``).

    make load-test                                  # 60 s at the default rates
    uv run python scripts/load_test.py --duration 30s --scale 0.5 --json out.json

It runs the k6 suite of ``tests/load/agenttwin.js`` on the compose network
(the ``load`` service): the trace list, the trace ingestion path (OTLP to
the collector, then followed until the API shows the trace), a release
summary, graph queries and simulation submissions, all at once. Then it
checks what k6 cannot see from outside:

* every trace the collector accepted was stored (no silent loss under load);
* every simulation run the load queued ran to COMPLETED and passed, and how
  long the worker took to drain them;
* the event consumers kept up: the broker's queues and the outboxes are
  empty again, and nothing reached a dead-letter queue;
* what the services themselves measured (Prometheus): the 95th percentile
  per service, of trace ingestion and of database queries.

The load writes into the demo workspace, visibly apart: traces of the
environment ``loadtest`` (session ``load-<run id>``), and simulation runs of
refund-happy-path on 1.3.1.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "sdk-python" / "src"))

# The drill's helpers for the running stack.
from chaos_drill import (
    API_URL,
    OPENER,
    PROJECT,
    PROMETHEUS_URL,
    ROOT,
    DrillFailed,
    dead_letters,
    prometheus,
    queue_depths,
    until,
)

from agenttwin.api import Client  # the SDK from this checkout

# The requests of the suite, in report order, with what they are.
PATHS = [
    ("traces_list", "GET /traces (first page, sometimes filtered)"),
    ("traces_list_next", "GET /traces?cursor= (the next page)"),
    ("otlp_export", "POST /v1/traces to the collector (an agent run, 5 spans)"),
    ("release_detail", "GET /releases/{id}"),
    ("release_gate", "GET /releases/{id}/gate"),
    ("graph_view", "GET /graph (the graph page)"),
    ("graph_focus", "GET /graph?focus=<tool>&depth=4&limit=500"),
    ("change_impact", "GET /change-sets/{id}/impact (blast radius + scenarios)"),
    ("simulation_submit", "POST /simulations"),
]
TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}


class LoadFailed(Exception):
    pass


def seconds(duration: str) -> float:
    m = re.fullmatch(r"(\d+)(s|m)", duration)
    if not m:
        raise argparse.ArgumentTypeError("a duration like 60s or 2m")
    return float(m[1]) * (60 if m[2] == "m" else 1)


def run_k6(api_key: str, run_id: str, duration: str, scale: float, ingest_rate: int | None, out: Path) -> int:
    env = {**os.environ, "AGENTTWIN_API_KEY": api_key}
    extra = ["-e", f"LOAD_INGEST_RATE={ingest_rate}"] if ingest_rate else []
    return subprocess.run(  # noqa: S603 - fixed arguments
        [  # noqa: S607 - docker from PATH, as make dev uses it
            "docker", "compose", "run", "--rm", "-T",
            "-v", f"{out}:/out",
            # The key's value comes from the environment, never the command line.
            "-e", "AGENTTWIN_API_KEY",
            "-e", f"LOAD_RUN_ID={run_id}",
            "-e", f"LOAD_DURATION={duration}",
            "-e", f"LOAD_SCALE={scale}",
            *extra,
            "load", "run", "--quiet", "/load/agenttwin.js",
        ],
        cwd=ROOT,
        env=env,
        check=False,
    ).returncode  # fmt: skip


def all_pages(client: Client, path: str, query: dict[str, str]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor = None
    while True:
        page = client.request("GET", path, query={**query, **({"cursor": cursor} if cursor else {})})
        items.extend(page["items"])
        cursor = page.get("next_cursor")
        if not cursor:
            return items


def when(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def quantile(expr: str, at: float) -> dict[str, float]:
    """A PromQL expression by its labels (joined), as of ``at``."""
    url = f"{PROMETHEUS_URL}/api/v1/query?" + urllib.parse.urlencode({"query": expr, "time": f"{at:.3f}"})
    with OPENER.open(url, timeout=10) as resp:
        result = json.loads(resp.read())["data"]["result"]
    out = {}
    for r in result:
        label = "/".join(v for k, v in sorted(r["metric"].items()))
        value = float(r["value"][1])
        if value == value:  # not NaN: a series with no observations in the window
            out[label or "all"] = value
    return out


def ms(v: float) -> str:
    return f"{v:.0f}" if v >= 10 else f"{v:.1f}"


# ------------------------------------------------------------------ checks


def check_traces(client: Client, project_id: str, run_id: str, accepted: int) -> dict[str, Any]:
    """Every trace the collector accepted is stored, once."""
    query = {"project_id": project_id, "session_id": f"load-{run_id}", "limit": "200"}
    started = time.monotonic()
    stored = until(
        f"all {accepted} accepted traces are stored",
        lambda: (n := len(all_pages(client, "/api/v1/traces", query))) == accepted and n,
        120 + accepted / 50,
        3,
    )
    return {"accepted": accepted, "stored": stored, "settled_s": time.monotonic() - started}


def check_simulations(
    client: Client, project_id: str, since: datetime, queued: int, k6_end: datetime
) -> dict[str, Any]:
    """Every run the load queued runs to COMPLETED, and passes."""
    query = {"project_id": project_id, "agent": "support-refund-agent", "limit": "200"}

    def runs() -> list[dict[str, Any]]:
        return [r for r in all_pages(client, "/api/v1/simulations", query) if when(r["created_at"]) >= since]

    done = until(
        "every queued simulation run finishes",
        lambda: (rs := runs()) and len(rs) >= queued and all(r["status"] in TERMINAL for r in rs) and rs,
        1800,
        5,
    )
    statuses: dict[str, int] = {}
    for r in done:
        statuses[r["status"]] = statuses.get(r["status"], 0) + 1
    if len(done) != queued:
        raise LoadFailed(f"{len(done)} simulation runs since the start, but k6 queued {queued}")
    bad = [r["id"] for r in done if r["status"] != "COMPLETED" or r.get("passed") != 1]
    if bad:
        raise LoadFailed(f"{len(bad)} simulation run(s) did not complete with their case passed: {bad[:5]}")
    last = max(when(r["finished_at"]) for r in done)
    return {
        "queued": queued,
        "statuses": statuses,
        "drained_after_s": max(0.0, (last - k6_end).total_seconds()),
    }


def check_consumers(dlq_before: int) -> dict[str, Any]:
    """The consumers kept up: queues and outboxes empty again, nothing parked."""
    started = time.monotonic()
    until(
        "the broker's queues are empty again",
        lambda: all(n == 0 for q, n in queue_depths().items() if not q.endswith(".dlq")),
        300,
        3,
    )
    until("the outboxes are empty again", lambda: prometheus("sum(agenttwin_outbox_backlog)") == 0, 120, 5)
    dlq_after = dead_letters()
    if dlq_after > dlq_before:
        raise LoadFailed(f"{dlq_after - dlq_before} event(s) reached a dead-letter queue under load")
    return {"settled_s": time.monotonic() - started, "dead_lettered": 0}


def server_side(window_s: float, at: float) -> dict[str, Any]:
    """What the services measured during the load (Prometheus, p95 in ms)."""
    w = f"{int(window_s)}s"
    skip = 'route!~".*(healthz|readyz|metrics).*"'
    return {
        "http_p95_ms_by_service": {
            k: v * 1000
            for k, v in quantile(
                f"histogram_quantile(0.95, sum by (le, service) "
                f"(increase(agenttwin_http_request_duration_seconds_bucket{{{skip}}}[{w}])))",
                at,
            ).items()
        },
        "trace_ingest_p95_ms": quantile(
            "histogram_quantile(0.95, sum by (le) "
            f"(increase(agenttwin_trace_ingest_duration_seconds_bucket[{w}])))",
            at,
        ).get("all", float("nan"))
        * 1000,
        "db_query_p95_ms_by_service": {
            k: v * 1000
            for k, v in quantile(
                f"histogram_quantile(0.95, sum by (le, service) "
                f"(increase(agenttwin_db_query_duration_seconds_bucket[{w}])))",
                at,
            ).items()
        },
    }


# ------------------------------------------------------------------ report


def report(summary: dict[str, Any], result: dict[str, Any]) -> str:
    m = summary["metrics"]
    lines = [
        "| Request | Count | p50 ms | p95 ms | p99 ms | max ms | Threshold |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for name, what in PATHS:
        d = m.get(f"http_req_duration{{name:{name}}}")
        if d is None:
            continue
        v = d["values"]
        count = m.get(f"http_reqs{{name:{name}}}", {}).get("values", {}).get("count", "—")
        th = ", ".join(f"{t} {'ok' if r['ok'] else 'FAILED'}" for t, r in d.get("thresholds", {}).items())
        lines.append(
            f"| `{name}` {what} | {count} | {ms(v['med'])} | {ms(v['p(95)'])} "
            f"| {ms(v['p(99)'])} | {ms(v['max'])} | {th} |"
        )
    vis = m["trace_visible_ms"]["values"]
    t, s, c = result["traces"], result["simulations"], result["consumers"]
    lines += [
        "",
        f"* Requests: {m['http_reqs']['values']['count']:.0f} in "
        f"{result['duration_s']:.0f} s ({m['http_reqs']['values']['rate']:.1f}/s), "
        f"failed {m['http_req_failed']['values']['rate'] * 100:.2f} %, "
        f"rate limited {m['rate_limited']['values']['count']:.0f}.",
        f"* Trace ingestion: {t['accepted']} accepted, {t['stored']} stored; from export to visible in the "
        f"API p50 {ms(vis['med'])} ms, p95 {ms(vis['p(95)'])} ms, max {ms(vis['max'])} ms "
        f"({m['http_reqs{name:trace_poll}']['values']['count']:.0f} polls).",
        f"* Simulations: {s['queued']} queued, {s['statuses']}; the worker finished the last one "
        f"{s['drained_after_s']:.0f} s after the load stopped.",
        f"* Consumers: queues and outboxes empty {c['settled_s']:.0f} s after the checks began; "
        "nothing dead-lettered.",
    ]
    srv = result["server"]
    lines += [
        "",
        "Server side (Prometheus, p95 over the load window):",
        "",
        "| Service | HTTP p95 ms | DB query p95 ms |",
        "|---|---:|---:|",
    ]
    for svc in sorted(set(srv["http_p95_ms_by_service"]) | set(srv["db_query_p95_ms_by_service"])):
        http_v = srv["http_p95_ms_by_service"].get(svc)
        db_v = srv["db_query_p95_ms_by_service"].get(svc)
        cells = ["—" if v is None else ms(v) for v in (http_v, db_v)]
        lines.append(f"| {svc} | {cells[0]} | {cells[1]} |")
    lines += ["", f"Trace ingestion in trace-service, p95: {ms(srv['trace_ingest_p95_ms'])} ms."]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--duration", default="60s", help="how long k6 applies the load (default 60s)")
    parser.add_argument("--scale", type=float, default=1.0, help="multiplies every rate (default 1)")
    parser.add_argument(
        "--ingest-rate", type=int, help="traces per second sent to the collector (default 10 x scale)"
    )
    parser.add_argument("--json", type=Path, help="also write the results (k6 summary included) here")
    args = parser.parse_args(argv)
    window_s = seconds(args.duration)
    api_key = os.environ.get("AGENTTWIN_DEMO_API_KEY", "")
    if not api_key:
        print("AGENTTWIN_DEMO_API_KEY is not set (make load-test loads .env)", file=sys.stderr)
        return 2
    client = Client(API_URL, api_key, timeout_s=30)
    client.wait_ready(timeout_s=30)
    project_id = client.project_id(PROJECT)
    run_id = secrets.token_hex(8)
    dlq_before = dead_letters()
    since = datetime.now(UTC)
    rates = f"scale {args.scale}" + (f", {args.ingest_rate} traces/s" if args.ingest_rate else "")
    print(f"load test {run_id}: {args.duration} at {rates}", flush=True)
    with tempfile.TemporaryDirectory(prefix="agenttwin-load-") as tmp:
        out = Path(tmp)
        out.chmod(0o777)  # k6 runs as its own user and writes the summary here
        code = run_k6(api_key, run_id, args.duration, args.scale, args.ingest_rate, out)
        k6_end = datetime.now(UTC)
        k6_end_mono = time.time()
        summary_file = out / "summary.json"
        if not summary_file.exists():
            print(f"k6 wrote no summary (exit {code})", file=sys.stderr)
            return 1
        summary = json.loads(summary_file.read_text())
    m = summary["metrics"]
    failed = [
        f"{name}: {t}"
        for name, metric in m.items()
        for t, r in metric.get("thresholds", {}).items()
        if not r["ok"]
    ]
    result: dict[str, Any] = {
        "run_id": run_id,
        "duration_s": window_s,
        "scale": args.scale,
        "ingest_rate": args.ingest_rate,
    }
    try:
        accepted = int(m["traces_accepted"]["values"]["count"])
        result["traces"] = check_traces(client, project_id, run_id, accepted)
        result["simulations"] = check_simulations(
            client, project_id, since, int(m["simulations_queued"]["values"]["count"]), k6_end
        )
        result["consumers"] = check_consumers(dlq_before)
    except (LoadFailed, DrillFailed) as err:
        print(f"load test failed: {err}", file=sys.stderr)
        return 1
    # The last scrape of the window is at most 15 s after it ended.
    time.sleep(max(0.0, k6_end_mono + 20 - time.time()))
    result["server"] = server_side(window_s + 20, k6_end_mono + 20)
    print(report(summary, result))
    if args.json:
        args.json.write_text(json.dumps({"result": result, "k6": summary}, indent=1) + "\n")
    if failed or code != 0:
        print(f"k6 thresholds failed (exit {code}): {'; '.join(failed) or 'see above'}", file=sys.stderr)
        return 1
    print("load test passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
