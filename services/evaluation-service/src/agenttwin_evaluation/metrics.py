"""Evaluation metrics (spec §54), registered on the process registry next to
the shared ``agenttwin_*`` families. Labels are bounded: statuses,
classifications and judge outcomes are fixed sets, providers are the three
adapters, and run or scenario names never become labels."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

__all__ = ["JUDGE_OUTCOMES", "EvaluationMetrics"]

# How a semantic expectation met the judge: a fresh verdict, a cached one,
# not asked (the run's budget was spent), or an error of a known kind.
JUDGE_OUTCOMES = frozenset(
    {
        "verdict",
        "cached",
        "budget_spent",
        "timeout",
        "error_unavailable",
        "error_rate_limited",
        "error_timeout",
        "error_rejected",
        "error_malformed",
        "error_refused",
    }
)
_PROVIDERS = frozenset({"deterministic-fake", "anthropic", "openai"})
_CLASSIFICATIONS = frozenset({"NEW_CRITICAL_FAILURE", "REGRESSED", "INCOMPLETE", "IMPROVED", "UNCHANGED"})
ACTIVE_STATUSES = ("QUEUED", "PREPARING", "RUNNING", "EVALUATING")


class EvaluationMetrics:
    _instances: dict[int, EvaluationMetrics] = {}

    @classmethod
    def on(cls, registry: CollectorRegistry, service: str) -> EvaluationMetrics:
        """One instance per registry (a registry refuses duplicate families)."""
        existing = cls._instances.get(id(registry))
        if existing is None:
            existing = cls._instances[id(registry)] = cls(registry, service)
        return existing

    def __init__(self, registry: CollectorRegistry, service: str) -> None:
        self.service = service
        self.registry = registry
        self.runs = Counter(
            "agenttwin_eval_runs_total",
            "Evaluation runs that reached a terminal status.",
            ["status", "service"],
            registry=registry,
        )
        self.run_duration = Histogram(
            "agenttwin_eval_run_duration_seconds",
            "Wall time of an evaluation run, from request to its terminal status.",
            ["status", "service"],
            buckets=(1, 5, 10, 30, 60, 120, 300, 600, 1200, 1800, 3600),
            registry=registry,
        )
        self.cases = Counter(
            "agenttwin_eval_cases_total",
            "Compared cases of completed evaluation runs, by classification.",
            ["classification", "service"],
            registry=registry,
        )
        self.in_progress = Gauge(
            "agenttwin_eval_runs_in_progress",
            "Evaluation runs not yet finished, by status (QUEUED is the queue).",
            ["status", "service"],
            registry=registry,
        )
        self.judge_calls = Counter(
            "agenttwin_judge_calls_total",
            "Semantic expectations sent to the judge, by provider and outcome.",
            ["provider", "outcome", "service"],
            registry=registry,
        )
        self.judge_duration = Histogram(
            "agenttwin_judge_call_duration_seconds",
            "Latency of a judge call that was made (cache hits excluded).",
            ["provider", "service"],
            buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120),
            registry=registry,
        )

    def run_ended(self, run: Mapping[str, Any]) -> None:
        """A run reached ``run['status']``; its duration is request to finish."""
        status = str(run.get("status") or "UNKNOWN")
        self.runs.labels(status, self.service).inc()
        created, finished = run.get("created_at"), run.get("finished_at")
        if isinstance(created, datetime) and isinstance(finished, datetime):
            seconds = max(0.0, (finished - created).total_seconds())
            self.run_duration.labels(status, self.service).observe(seconds)

    def cases_compared(self, counts: Mapping[str, int]) -> None:
        for classification, n in counts.items():
            if classification in _CLASSIFICATIONS and n > 0:
                self.cases.labels(classification, self.service).inc(n)

    def active(self, by_status: Mapping[str, int]) -> None:
        for status in ACTIVE_STATUSES:
            self.in_progress.labels(status, self.service).set(by_status.get(status, 0))

    def judge_call(self, provider: str, outcome: str, seconds: float | None = None) -> None:
        p = provider if provider in _PROVIDERS else "other"
        o = outcome if outcome in JUDGE_OUTCOMES else "other"
        self.judge_calls.labels(p, o, self.service).inc()
        if seconds is not None:
            self.judge_duration.labels(p, self.service).observe(seconds)
