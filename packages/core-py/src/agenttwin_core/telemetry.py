"""Internal observability (spec §54): Prometheus metrics with the same names
as the Go services (``agenttwin_*`` with a ``service`` label) and
OpenTelemetry tracing marked ``service.namespace=agenttwin``, which the
collector routes to RED metrics instead of customer trace ingestion.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from opentelemetry import propagate, trace
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    gc_collector,
    platform_collector,
    process_collector,
)

__all__ = ["Metrics", "metrics", "setup_metrics", "setup_tracing"]


@dataclass
class Metrics:
    """The shared metric families; every sample carries ``service``."""

    service: str
    registry: CollectorRegistry
    http_requests: Counter
    http_duration: Histogram
    events_consumed: Counter
    event_duration: Histogram
    outbox_backlog: Gauge
    outbox_failures: Counter
    db_duration: Histogram

    def observe_http(self, method: str, route: str, status: int, seconds: float) -> None:
        self.http_requests.labels(method, route, str(status), self.service).inc()
        self.http_duration.labels(method, route, self.service).observe(seconds)

    def observe_event(self, event_type: str, outcome: str, seconds: float) -> None:
        self.events_consumed.labels(event_type or "unknown", outcome, self.service).inc()
        self.event_duration.labels(event_type or "unknown", self.service).observe(seconds)

    def observe_db(self, outcome: str, seconds: float) -> None:
        self.db_duration.labels(outcome, self.service).observe(seconds)


_metrics: Metrics | None = None


def setup_metrics(service: str, version: str) -> Metrics:
    """Creates the process registry. Idempotent: later calls return the first."""
    global _metrics
    if _metrics is not None:
        return _metrics
    reg = CollectorRegistry()
    process_collector.ProcessCollector(registry=reg)
    platform_collector.PlatformCollector(registry=reg)
    gc_collector.GCCollector(registry=reg)
    _metrics = Metrics(
        service=service,
        registry=reg,
        http_requests=Counter(
            "agenttwin_http_requests_total",
            "HTTP requests by route and status.",
            ["method", "route", "status", "service"],
            registry=reg,
        ),
        http_duration=Histogram(
            "agenttwin_http_request_duration_seconds",
            "HTTP request latency.",
            ["method", "route", "service"],
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
            registry=reg,
        ),
        events_consumed=Counter(
            "agenttwin_events_consumed_total",
            "Consumed events by type and outcome.",
            ["type", "outcome", "service"],
            registry=reg,
        ),
        event_duration=Histogram(
            "agenttwin_event_handling_seconds", "Event handler duration.", ["type", "service"], registry=reg
        ),
        outbox_backlog=Gauge(
            "agenttwin_outbox_backlog", "Unpublished outbox rows.", ["service"], registry=reg
        ),
        outbox_failures=Counter(
            "agenttwin_outbox_publish_failures_total", "Outbox publish failures.", ["service"], registry=reg
        ),
        db_duration=Histogram(
            "agenttwin_db_query_duration_seconds",
            "Database query latency.",
            ["outcome", "service"],
            buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5),
            registry=reg,
        ),
    )
    Gauge(
        "agenttwin_build_info",
        "Version of the running binary (value is always 1).",
        ["service", "version"],
        registry=reg,
    ).labels(service, version).set(1)
    return _metrics


def metrics() -> Metrics | None:
    """The process metrics, or None before :func:`setup_metrics` (tests)."""
    return _metrics


def setup_tracing(service: str, version: str, otlp_endpoint: str | None, sample_ratio: float = 1.0) -> None:
    """Exports AgentTwin's own spans when an OTLP endpoint is configured."""
    propagate.set_global_textmap(CompositePropagator([TraceContextTextMapPropagator()]))
    if not otlp_endpoint:
        return
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    resource = Resource.create(
        {
            "service.name": service,
            "service.namespace": "agenttwin",
            "service.version": version,
            "agenttwin.internal": "true",
            "host.name": os.environ.get("HOSTNAME", ""),
        }
    )
    ratio = sample_ratio if sample_ratio > 0 else 1.0
    provider = TracerProvider(resource=resource, sampler=ParentBased(TraceIdRatioBased(ratio)))
    exporter = OTLPSpanExporter(endpoint=otlp_endpoint.rstrip("/") + "/v1/traces", timeout=5)
    provider.add_span_processor(BatchSpanProcessor(exporter, max_queue_size=2048))
    trace.set_tracer_provider(provider)
