"""Telemetry must never hurt the host agent (spec §13): exporter failures,
unreachable collectors and stalled networks are absorbed, the application
path never blocks, and the in-memory queue is bounded."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Sequence
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from agenttwin import AgentTwin, Config


class ExplodingExporter(SpanExporter):
    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        raise ConnectionError("collector is on fire")

    def shutdown(self) -> None:
        raise RuntimeError("and shutdown fails too")


class StalledExporter(SpanExporter):
    """Blocks every export until released (a hung network connection)."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.exported = 0

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        self.release.wait(timeout=10)
        self.exported += len(spans)
        return SpanExportResult.SUCCESS


def emit_runs(client: AgentTwin, runs: int, tools_per_run: int) -> None:
    for i in range(runs):
        with client.agent_run("agent", "1") as run:
            for j in range(tools_per_run):
                with run.tool_call("lookup_order", args={"order_id": f"ORD-{i}-{j}"}, risk="READ") as t:
                    t.set_result({"ok": True})


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_exporter_exceptions_never_reach_the_host() -> None:
    client = AgentTwin(Config(schedule_delay_ms=10), exporter=ExplodingExporter())
    emit_runs(client, runs=5, tools_per_run=3)
    assert client.flush() in (True, False)
    client.shutdown()  # exporter shutdown raises; must be absorbed
    assert client.stats.ended == 20
    assert client.stats.failed == 20
    assert client.stats.exported == 0


def test_unreachable_collector_does_not_block_the_agent() -> None:
    cfg = Config(
        otlp_endpoint=f"http://127.0.0.1:{free_port()}",
        api_key="atk_x",
        export_timeout_s=0.5,
        schedule_delay_ms=10,
    )
    client = AgentTwin(cfg)
    start = time.perf_counter()
    emit_runs(client, runs=40, tools_per_run=4)
    elapsed = time.perf_counter() - start
    # 200 spans; the application path never waits for the network.
    assert elapsed < 1.0, f"instrumented code took {elapsed:.3f}s with the collector down"
    client.shutdown()
    assert client.stats.exported == 0
    assert client.stats.ended == 200


def test_queue_is_bounded_when_the_network_stalls() -> None:
    exporter = StalledExporter()
    client = AgentTwin(
        Config(max_queue_size=50, max_export_batch_size=10, schedule_delay_ms=5), exporter=exporter
    )
    start = time.perf_counter()
    emit_runs(client, runs=250, tools_per_run=7)  # 2000 spans
    elapsed = time.perf_counter() - start
    assert elapsed < 2.0, f"producing spans blocked for {elapsed:.3f}s while the exporter was stalled"
    exporter.release.set()
    client.flush()
    client.shutdown()
    assert client.stats.ended == 2000
    # Everything beyond the queue (plus the batch in flight) was dropped,
    # never buffered without bound.
    assert exporter.exported <= 50 + 10, exporter.exported
    assert client.stats.not_exported >= 2000 - 60


class _Capture(BaseHTTPRequestHandler):
    requests: list[dict[str, Any]] = []

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        _Capture.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
        self.send_response(200)
        self.send_header("Content-Type", "application/x-protobuf")
        self.end_headers()

    def log_message(self, fmt: str, *args: Any) -> None:
        pass


def test_wire_format_is_otlp_http_protobuf_with_api_key() -> None:
    _Capture.requests = []
    server = HTTPServer(("127.0.0.1", 0), _Capture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        client = AgentTwin(
            Config(
                otlp_endpoint=f"http://127.0.0.1:{port}",
                api_key="atk_demo0000_secret-value-123",
                schedule_delay_ms=10,
            )
        )
        emit_runs(client, runs=2, tools_per_run=1)
        client.shutdown()
    finally:
        server.shutdown()
    assert _Capture.requests, "nothing was exported"
    req = _Capture.requests[0]
    assert req["path"] == "/v1/traces"
    headers = {k.lower(): v for k, v in req["headers"].items()}
    assert headers["x-agenttwin-api-key"] == "atk_demo0000_secret-value-123"
    assert headers["content-type"] == "application/x-protobuf"
    names: list[str] = []
    for r in _Capture.requests:
        msg = ExportTraceServiceRequest()
        msg.ParseFromString(r["body"])
        for rs in msg.resource_spans:
            res = {a.key: a.value for a in rs.resource.attributes}
            assert res["agenttwin.sdk.name"].string_value == "agenttwin-python"
            for ss in rs.scope_spans:
                names.extend(s.name for s in ss.spans)
    assert sorted(names) == sorted(["invoke_agent agent", "execute_tool lookup_order"] * 2)
