# SDK instrumentation overhead

The SDK must not slow an agent down noticeably and must never make an agent
wait for the network (spec §13). This page records measured numbers for both
SDKs; it does not claim zero overhead.

## Python SDK

### Method

`packages/sdk-python/tests/test_overhead.py` (part of the normal test suite):

* 2,000 calls of a trivial tool function, first uninstrumented (baseline),
  then wrapped in a tool span inside an agent run.
* The exporter sleeps 50 ms per batch (a slow collector). The batch span
  processor exports on a background thread, so the measured calls include
  span creation, attribute capture, hashing and redaction, but never the
  export itself.
* One warm-up pass per content mode, then the measured pass.
* `content=off` records metadata and argument hashes only (the default).
  `content=redacted` additionally serializes the arguments and result and runs
  the secret/PII redactor on them.

Run it with `uv run pytest -s packages/sdk-python/tests/test_overhead.py`.

### Results

Machine: 4 vCPU Intel Xeon @ 2.10 GHz (shared cloud VM), Python 3.12.11,
OpenTelemetry SDK 1.44.0, AgentTwin SDK 0.1.0. Three consecutive runs,
2026-09-24; all values in microseconds per call.

| Measurement | Run | p50 | p99 | mean |
|---|---|---:|---:|---:|
| Uninstrumented call | 1 | 0.19 | 0.47 | 0.22 |
| | 2 | 0.20 | 0.73 | 0.23 |
| | 3 | 0.20 | 0.73 | 0.22 |
| Tool span, `content=off` | 1 | 42.7 | 196.1 | 57.6 |
| | 2 | 43.1 | 110.3 | 57.0 |
| | 3 | 43.7 | 132.0 | 59.4 |
| Tool span, `content=redacted` | 1 | 90.1 | 180.3 | 98.7 |
| | 2 | 91.7 | 261.7 | 104.6 |
| | 3 | 91.9 | 168.4 | 101.1 |

### Reading the numbers

* A tool span costs about **43 µs** at the median with content capture off and
  about **91 µs** with redacted content. An agent step that calls a model
  (hundreds of milliseconds) or a network tool (milliseconds) spends well
  under 0.1 % of its time in instrumentation.
* p50 is stable across runs (±2 %); p99 varies between 110 and 260 µs because
  the machine is shared. The test asserts generous bounds (p50 < 500 µs,
  p99 < 5 ms with content off) so it does not flake on CI runners.
* The slow exporter did not appear in any sample: export happens on the
  background thread. When the export queue is full the SDK drops spans and
  counts them (`client.stats.not_exported`, see `test_resilience.py`) instead
  of blocking the agent.
* Redaction is the main cost of `content=redacted`; it is paid only by projects
  that opt into content capture (ADR-0008).

## TypeScript SDK

### Method

`packages/sdk-typescript/test/overhead.test.ts` (part of the normal test
suite; `AGENTTWIN_BENCH_OUT=<file>` writes the numbers to a file):

* 2,000 calls of a trivial tool function, uninstrumented (baseline), then as
  a tool call inside an agent run, `content=off` and `content=redacted`, each
  after a warm-up pass.
* The spans go through the real exporter to a local HTTP collector that
  answers every request after 50 ms (a slow collector).
* Two shapes of agent: a synchronous loop of tool calls, and an async one
  that awaits every step (and yields to the event loop every 100 steps, as
  I/O would), so the exports run in between the steps on the same thread.
* Node.js has one application thread: the export does not run beside the
  agent as it does in Python but between its steps. What the export costs
  that thread is measured separately: encoding a full batch of 512 spans as
  OTLP JSON. A span never exports inside the application's `end()`; a full
  batch starts on the next turn of the event loop (a test pins this).

Run it with `pnpm --filter @agenttwin/sdk exec vitest run test/overhead.test.ts`.

### Results

Machine: 4 vCPU Intel Xeon @ 2.80 GHz (shared cloud VM), Node.js 22.22.2,
AgentTwin SDK 0.1.0. Three consecutive runs, 2026-09-26; microseconds per call.

| Measurement | Run | p50 | p99 | mean |
|---|---|---:|---:|---:|
| Uninstrumented call | 1 | 0.22 | 1.94 | 0.36 |
| | 2 | 0.23 | 2.74 | 0.40 |
| | 3 | 0.23 | 2.19 | 0.37 |
| Tool span, `content=off` | 1 | 15.3 | 108.2 | 21.4 |
| | 2 | 14.9 | 91.3 | 19.4 |
| | 3 | 15.1 | 133.4 | 21.6 |
| Tool span, `content=off`, awaited | 1 | 7.8 | 57.6 | 10.2 |
| | 2 | 8.1 | 79.2 | 10.6 |
| | 3 | 8.3 | 50.4 | 14.9 |
| Tool span, `content=redacted` | 1 | 31.5 | 116.0 | 39.2 |
| | 2 | 29.7 | 110.7 | 37.2 |
| | 3 | 31.6 | 139.0 | 41.2 |
| Tool span, `content=redacted`, awaited | 1 | 30.3 | 92.1 | 34.6 |
| | 2 | 31.4 | 100.6 | 37.0 |
| | 3 | 30.7 | 92.2 | 34.7 |
| Encoding a batch of 512 spans (export) | 1 | 3,362 | 8,632 | 3,894 |
| | 2 | 3,052 | 5,049 | 3,249 |
| | 3 | 3,199 | 6,482 | 3,492 |

### Reading the numbers

* A tool span costs about **15 µs** at the median with content capture off
  and about **30 µs** with redacted content, on the calling path. The awaited
  shape is a little cheaper at the median (the code is fully warmed up by
  then); its p99 stays under 0.1 ms although the exports ran in between.
* The export adds about **6 µs per span** of work on the same thread (a full
  batch encodes in about 3 ms), outside any instrumented call. An agent step
  that calls a model (hundreds of milliseconds) or a network tool
  (milliseconds) spends well under 0.1 % of its time in instrumentation.
* The 50 ms collector never showed in a step's latency: exports are
  asynchronous requests, one in flight at a time.
* This benchmark produces about 100,000 spans a second, far more than one
  50 ms request at a time carries (about 10,000 a second): the bounded queue
  shed the excess, 2,372–3,396 of 8,004 spans per content mode, and counted
  it (`client.stats.dropped`); the test checks that every span is either
  exported or counted as dropped, and none failed. A real agent emits a few
  spans per model call and is nowhere near that rate.
* The test asserts the same generous bounds as the Python one (p50 < 500 µs,
  p99 < 5 ms with content off; p50 < 1.5 ms redacted) so it does not flake on
  shared runners.
