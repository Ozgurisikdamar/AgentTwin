# Python SDK instrumentation overhead

The SDK must not slow an agent down noticeably and must never make an agent
wait for the network (spec §13). This page records measured numbers; it does
not claim zero overhead.

## Method

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

## Results

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

## Reading the numbers

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
