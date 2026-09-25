"""Simulation metrics (spec §54), registered on the process registry next to
the shared ``agenttwin_*`` families. Labels are bounded: tool names never
become labels (they come from user-authored twins)."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from agenttwin_simulation.twin.faults import FAULT_TYPES

__all__ = ["SimulationMetrics"]

_FAULTS = frozenset(FAULT_TYPES)


class SimulationMetrics:
    _instances: dict[int, SimulationMetrics] = {}

    @classmethod
    def on(cls, registry: CollectorRegistry, service: str) -> SimulationMetrics:
        """One instance per registry (a registry refuses duplicate families,
        and tests run both roles in one process)."""
        existing = cls._instances.get(id(registry))
        if existing is None:
            existing = cls._instances[id(registry)] = cls(registry, service)
        return existing

    def __init__(self, registry: CollectorRegistry, service: str) -> None:
        self.service = service
        self.twin_calls = Counter(
            "agenttwin_twin_calls_total",
            "Calls served by tool twins, by call status and injected fault.",
            ["status", "fault", "service"],
            registry=registry,
        )
        self.cases = Counter(
            "agenttwin_simulation_cases_total",
            "Finished simulation cases by verdict.",
            ["status", "service"],
            registry=registry,
        )
        self.case_duration = Histogram(
            "agenttwin_simulation_case_duration_seconds",
            "Wall time of one simulation case (agent run and evaluation).",
            ["service"],
            buckets=(0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300),
            registry=registry,
        )
        self.runs = Counter(
            "agenttwin_simulation_runs_total",
            "Simulation runs that reached a terminal status.",
            ["status", "service"],
            registry=registry,
        )
        self.queue_depth = Gauge(
            "agenttwin_simulation_queue_depth",
            "Simulation runs waiting for a worker.",
            ["service"],
            registry=registry,
        )
        self.outcomes = Counter(
            "agenttwin_simulation_outcomes_total",
            "Verified outcomes reported to the trace service, by result.",
            ["result", "service"],
            registry=registry,
        )

    def twin_call(self, status: str, fault: str | None) -> None:
        label = fault if fault in _FAULTS else "none"
        self.twin_calls.labels(status, label, self.service).inc()

    def case(self, status: str, seconds: float) -> None:
        self.cases.labels(status, self.service).inc()
        self.case_duration.labels(self.service).observe(seconds)

    def run(self, status: str) -> None:
        self.runs.labels(status, self.service).inc()

    def outcome(self, result: str) -> None:
        self.outcomes.labels(result, self.service).inc()
