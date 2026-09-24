"""Instrumentation overhead benchmark (spec §13: measure it, do not claim
zero). The exporter is deliberately slow; the numbers show that ordinary
instrumented steps never wait for the network. Results are printed (run with
``-s``) and recorded in docs/benchmarks/sdk-overhead.md."""

from __future__ import annotations

import statistics
import time
from collections.abc import Sequence
from typing import Any

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from agenttwin import AgentTwin, Config

N = 2000


class SlowExporter(SpanExporter):
    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        time.sleep(0.05)  # 50 ms per batch: a slow collector
        return SpanExportResult.SUCCESS


def bare_tool(order_id: str) -> dict[str, Any]:
    return {"order_id": order_id, "status": "delivered"}


def measure(client: AgentTwin | None) -> list[float]:
    samples: list[float] = []
    if client is None:
        for i in range(N):
            t0 = time.perf_counter()
            bare_tool(f"ORD-{i}")
            samples.append((time.perf_counter() - t0) * 1e6)
        return samples
    with client.agent_run("bench-agent", "1") as run:
        for i in range(N):
            t0 = time.perf_counter()
            with run.tool_call("lookup_order", args={"order_id": f"ORD-{i}"}, risk="READ") as call:
                call.set_result(bare_tool(f"ORD-{i}"))
            samples.append((time.perf_counter() - t0) * 1e6)
    return samples


def summarize(samples: list[float]) -> dict[str, float]:
    q = statistics.quantiles(samples, n=100)
    return {"p50_us": q[49], "p99_us": q[98], "mean_us": statistics.fmean(samples)}


def test_instrumentation_overhead_is_small_and_network_independent() -> None:
    baseline = summarize(measure(None))
    results = {}
    for mode in ("off", "redacted"):
        client = AgentTwin(Config(content_mode=mode, max_queue_size=4096), exporter=SlowExporter())  # type: ignore[arg-type]
        measure(client)  # warm up
        results[mode] = summarize(measure(client))
        client.shutdown()
    print(f"\nbaseline (uninstrumented call): {baseline}")
    for mode, r in results.items():
        print(f"tool span, content={mode}: {r}")
    # Generous bounds so shared CI machines do not flake; typical values are
    # an order of magnitude lower (see docs/benchmarks/sdk-overhead.md).
    assert results["off"]["p50_us"] < 500
    assert results["off"]["p99_us"] < 5000
    assert results["redacted"]["p50_us"] < 1500
