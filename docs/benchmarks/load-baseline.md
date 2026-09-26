# Load-test baseline

What the running stack does under the load of spec §65, measured, with the
method so the numbers can be reproduced and compared. It is a baseline for
one machine, not a capacity plan: the point is to notice when a change makes
an ordinary path slower, loses data under load, or stops the consumers from
keeping up.

## Method

`make load-test` (after `make dev`) runs `scripts/load_test.py`, which starts
the k6 suite `tests/load/agenttwin.js` on the compose network (the `load`
service, `grafana/k6:1.3.0`) and then checks what k6 cannot see from outside.

The five paths of §65 run at the same time, each at a constant arrival rate
(requests start on schedule whether or not earlier ones have finished), for
60 seconds:

| Scenario | Rate | What one iteration does |
|---|---:|---|
| trace list | 10/s | `GET /traces?project_id=…&limit=50`, unfiltered or filtered (agent, status, source, environment); 30 % also fetch the next page |
| trace ingestion | 10/s | `POST /v1/traces` to the OTel collector: one run of the refund agent as the Python SDK records it (agent span, two model calls, two read-only tool calls; metadata only). About one trace a second, picked at random, is followed with `GET /traces/{id}` until the API shows it |
| release summary | 6/s | `GET /releases/{id}` and `GET /releases/{id}/gate` side by side, as the release page loads them |
| graph query | 6/s | 45 % the graph page (`GET /graph`, every agent's latest version, two hops), 45 % a tool's neighbourhood (`focus=<tool>&depth=4&limit=500`), 10 % change impact (`GET /change-sets/{id}/impact`: blast radius plus the scenarios the change needs, with the reasons) |
| simulation submission | 1/s | `POST /simulations`: refund-happy-path on 1.3.1, with an idempotency key |

The API rates add up to about 44 requests a second, under the 50/s one API
key may make (`RATE_LIMIT_RPS`), so a 429 is a finding, not noise. Ingestion
goes to the collector, which the API's rate limit does not cover; its rate
can be raised on its own (`--ingest-rate`).

Thresholds (the run fails otherwise): p95 under 300 ms for every ordinary
read and write (spec §57), under 1 s for change impact, under 5 s from export
to visible for a trace; no failed request, no 429, no dropped iteration, every
check passing.

After k6, the wrapper checks, through the API and the broker:

* every trace the collector accepted was stored (counted by the load's own
  session id), so nothing is lost silently under load;
* every simulation run the load queued ran to COMPLETED and its case passed,
  and when the worker finished the last one;
* the broker's queues and the outboxes are empty again and no event reached a
  dead-letter queue;
* what the services measured themselves (Prometheus): the p95 of every
  service's HTTP requests (health and metrics excluded), of database queries,
  and of trace ingestion in trace-service.

The load writes into the demo workspace, apart from the rest: traces of
environment `loadtest`, session `load-<run id>`, and simulation runs of
refund-happy-path. They show up in the lists; `make reset` starts clean.

## Machine and data

A shared cloud VM: 4 vCPU Intel Xeon @ 2.80 GHz, 15 GiB RAM, Docker 29.3.1,
Compose 5.1.1. The whole stack (16 containers: PostgreSQL, RabbitMQ, the
collector, Prometheus, Grafana, six services, two workers, the web app and the
demo agent and tools) and k6 share the four cores. At the start of the
baseline runs the demo project held 15,185 traces (80,414 spans), 407
simulation runs, 43 graph components and 103 dependency edges.

## Results

### Default rates — three runs (2026-09-26)

p50 / p95 / p99 in milliseconds, as k6 measured them (client side,
through the compose network).

| Request | Run 1 | Run 2 | Run 3 | Requests per run |
|---|---|---|---|---:|
| trace list | 9.2 / 26 / 43 | 8.5 / 27 / 41 | 8.8 / 34 / 49 | 601 |
| trace list, next page | 5.1 / 12 / 25 | 5.5 / 15 / 18 | 5.8 / 18 / 23 | 142–144 |
| OTLP export to the collector | 1.1 / 6.2 / 17 | 1.2 / 6.8 / 18 | 1.2 / 7.3 / 17 | 601 |
| release | 4.0 / 15 / 23 | 4.2 / 16 / 31 | 4.5 / 17 / 32 | 360–361 |
| release gate | 9.7 / 21 / 30 | 9.8 / 25 / 40 | 11 / 27 / 41 | 360–361 |
| graph page | 12 / 26 / 39 | 12 / 35 / 43 | 12 / 35 / 46 | 163–175 |
| tool neighbourhood, depth 4 | 14 / 29 / 44 | 15 / 34 / 41 | 16 / 41 / 58 | 156–160 |
| change impact | 29 / 52 / 60 | 37 / 62 / 70 | 33 / 72 / 74 | 30–40 |
| simulation submission | 39 / 70 / 76 | 41 / 77 / 96 | 41 / 81 / 102 | 60–61 |

Every run: 2,607–2,661 requests (43–44/s), none failed, none rate limited,
no dropped iteration; every threshold passed.

End to end, every run:

* **Ingestion:** 601 traces accepted, 601 stored. From export to visible in
  the API: p50 523–765 ms, p95 1,028–1,037 ms, max 1,046–1,276 ms. The
  collector batches for up to one second before it exports, so a trace waits
  for the batch half a second on average and a second at most; storing it
  takes the rest (trace-service's ingestion p95: 56–60 ms per batch).
* **Simulations:** 60–61 queued, all COMPLETED with their case passed; the
  worker finished the last one as the load stopped (it keeps up with one run
  a second).
* **Consumers:** the queues and outboxes were empty within 2–17 s of the end;
  nothing was dead-lettered.

Server side (Prometheus, p95 over the run): control-plane 32–39 ms (it adds
the proxied services' time), simulation-service 41–47 ms, graph-service
24–29 ms, trace-service 23–25 ms, runtime-gateway 5–7 ms; database queries
2.1–8.4 ms in every service.

The spec's target — p95 under ~300 ms for ordinary reads and writes at modest
concurrency (§57) — holds: the slowest ordinary path, simulation submission
(70–81 ms p95), is under a third of it, and reads are 12–41 ms. Blast-radius
traversal (four hops from a tool, up to 500 components) is interactive at
29–41 ms p95.

### A finding: the health checks used a core

The first ingestion probe (below) saturated the machine, and sampling the
containers' CPU showed the stack was busy even when idle: about 1.3 of the 4
cores with no load at all. The cause was the containers' own health checks,
measured one call at a time:

| Health check | Every | CPU per call | Share of a core |
|---|---:|---:|---:|
| `simulation-service healthcheck` (also the worker's) | 5 s | 1.42 s | ~28 % each |
| `evaluation-service healthcheck` (also the worker's) | 5 s | 1.52 s | ~30 % each |
| `rabbitmq-diagnostics check_port_connectivity` | 5 s | ~1 s | ~20 % |
| demo agent and tools (`python -c "import urllib.request …"`) | 3 s | ~0.3 s | ~9 % each |
| Go services (`/app/service healthcheck`), for comparison | 5 s | 0.04 s | < 1 % |

The Python probe imported the whole service (FastAPI, the database driver,
the evaluators) to make one local HTTP request; `urllib.request` alone brings
in `ssl` and `email`. The fix (the same commit as this page):

* `agenttwin_core.probe` does the request with a bare socket and imports
  nothing else; the services' command entry points (`agenttwin_*.cli`)
  answer `healthcheck` from it before importing the service. Tests pin that
  the probe imports none of `fastapi`, `psycopg`, `uvicorn`, `ssl`,
  `urllib.request` or the service, that each command probes its service's own
  port, and that every other command still runs the service.
* RabbitMQ's check (an Erlang VM per call) runs every 2 s while the broker
  starts (`start_interval`) and every 30 s after; the test broker likewise.
* The demo containers probe with a bare socket every 10 s. (The probe
  reads the whole response before closing: closing after the status line
  reset the connection under the server's body write and filled the demo
  agent's log with a traceback every 10 s.)

Idle CPU of the whole stack, measured from the containers' cgroup counters:
**about 133 % of a core before, 28 % after** (the Python services 22-23 %
each → 3 %, RabbitMQ 23 % → 4 %, demo 8-9 % → 1 %).

After the fix, the default rates once more (p50 / p95 / p99 ms): trace list
7.8 / 35 / 51, next page 4.7 / 9.7 / 14, OTLP export 1.0 / 2.6 / 4.9, release
3.2 / 9.0 / 14, gate 8.0 / 19 / 30, graph page 10 / 20 / 31, tool
neighbourhood 12 / 26 / 43, change impact 22 / 56 / 77, simulation submission
34 / 70 / 113. Same checks, same outcome: 601 of 601 traces stored (visible
p95 1,026 ms), 61 of 61 simulations completed, nothing dead-lettered.

### Ingestion pushed to 100 traces a second

`--ingest-rate 100`: ten times the default ingestion, the API rates
unchanged, 60 s (about 6,000 traces, 30,000 spans, and the consumers' work on
each). p95 in milliseconds (p99 in brackets):

| Request | Before the fix | After the fix |
|---|---:|---:|
| trace list | 136 (195) | 67 (106) |
| trace list, next page | 72 (106) | 22 (37) |
| OTLP export to the collector | 13 (60) | 4.1 (15) |
| release | 51 (90) | 19 (36) |
| release gate | 71 (116) | 36 (61) |
| graph page | 95 (139) | 45 (61) |
| tool neighbourhood, depth 4 | 136 (255) | 61 (87) |
| change impact | 187 (383) | 98 (108) |
| simulation submission | 286 (474) | 94 (134) |
| trace-service ingestion (server side, per batch) | 815 | 253 |
| export to visible | 1,380 (1,597) | 1,307 (1,404) |

Both runs lost nothing: 6,001 and 6,000 traces accepted and stored, 61
simulations completed, queues and outboxes empty within 25 s, nothing
dead-lettered, no request failed. Before the fix the machine was CPU-bound
at this rate (the containers together used about 3.7 of the 4 cores: the
evaluation worker, which mines every production trace for regressions, the
simulation worker, PostgreSQL, trace-service and graph-service most) and an
earlier run crossed the 300 ms threshold for simulation submission (327 ms
p95); after it, every ordinary path stays under 110 ms at the 95th
percentile.

Ingestion is not the bottleneck at this rate: the collector answers in
milliseconds and every trace is visible within about 1.5 s. Nothing here
calls for optimizing further (§65: no speculative optimization); the next
limit on this machine is CPU shared by everything on it.

## Reproducing and comparing

```bash
make dev
make load-test                                  # the default rates, 60 s
make load-test LOAD_ARGS="--duration 2m"        # longer
make load-test LOAD_ARGS="--ingest-rate 100"    # push ingestion alone
make load-test LOAD_ARGS="--json results.json"  # keep the k6 summary and the checks
```

Compare on the same machine, with the stack otherwise idle (stop the test
infrastructure of `make test` first:
`docker compose -f docker-compose.test.yml stop`). Differences of a few
milliseconds between runs are noise; a p95 that moves by 2× is not.

## What this does not cover

* One machine, one API key, one project. It says nothing about horizontal
  scaling, and the per-key rate limit caps what one client may ask anyway.
* The demo data set is small (about 15–20 thousand traces). Trace list
  queries use indexes and cursor pagination, but their cost on millions of
  traces is not measured here.
* LLM-backed evaluation: the demo judge and agent are deterministic, so the
  simulations measure AgentTwin's own overhead, not a provider's latency.
* The web UI's rendering (Playwright covers what it shows, not how fast).
