"""Evaluation metrics (spec §54): every way a semantic expectation meets the
judge is counted once, under a bounded label; a run's end counts its status,
its duration and its compared cases; unknown values never become labels."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from prometheus_client import CollectorRegistry
from test_semantic import SPEC, MemoryCache, Scripted, grade, verdict

from agenttwin_evaluation.judges import JudgeError
from agenttwin_evaluation.metrics import JUDGE_OUTCOMES, EvaluationMetrics
from agenttwin_evaluation.semantic import JudgeBudget, SemanticJudging


def sample(reg: CollectorRegistry, name: str, **labels: str) -> float:
    value = reg.get_sample_value(name, {**labels, "service": "evaluation-worker"})
    return 0.0 if value is None else value


@pytest.fixture
def reg() -> CollectorRegistry:
    return CollectorRegistry()


def metrics_on(reg: CollectorRegistry) -> EvaluationMetrics:
    return EvaluationMetrics.on(reg, "evaluation-worker")


def test_one_instance_per_registry(reg: CollectorRegistry) -> None:
    assert metrics_on(reg) is metrics_on(reg)
    assert metrics_on(CollectorRegistry()) is not metrics_on(reg)


def test_each_judge_outcome_is_counted_once(reg: CollectorRegistry) -> None:
    m = metrics_on(reg)
    cache = MemoryCache()
    judge = Scripted(
        verdict(0.9),
        JudgeError("rate_limited", "slow down"),
        JudgeError("malformed", "not json"),
    )
    judging = SemanticJudging(judge, budget=JudgeBudget(max_calls=3), cache=cache, metrics=m)
    grade(judging)  # a fresh verdict
    grade(judging)  # the same request again: served from the cache
    grade(judging, SPEC | {"rubric": "Second rubric."})  # 429 from the provider
    grade(judging, SPEC | {"rubric": "Third rubric."})  # malformed answer
    grade(judging, SPEC | {"rubric": "Fourth rubric."})  # three calls made: budget spent

    calls = "agenttwin_judge_calls_total"
    assert sample(reg, calls, provider="anthropic", outcome="verdict") == 1
    assert sample(reg, calls, provider="anthropic", outcome="cached") == 1
    assert sample(reg, calls, provider="anthropic", outcome="error_rate_limited") == 1
    assert sample(reg, calls, provider="anthropic", outcome="error_malformed") == 1
    assert sample(reg, calls, provider="anthropic", outcome="budget_spent") == 1
    # Latency is observed for the calls that were made, not for a cache hit or a skip.
    assert sample(reg, "agenttwin_judge_call_duration_seconds_count", provider="anthropic") == 3


def test_a_judge_that_hangs_is_counted_as_a_timeout(reg: CollectorRegistry) -> None:
    m = metrics_on(reg)
    grade(SemanticJudging(Scripted(5.0), timeout_s=0.01, metrics=m))
    assert sample(reg, "agenttwin_judge_calls_total", provider="anthropic", outcome="timeout") == 1
    assert sample(reg, "agenttwin_judge_call_duration_seconds_count", provider="anthropic") == 1


def test_judging_without_metrics_still_grades() -> None:
    assert grade(SemanticJudging(Scripted(verdict(0.9)))).result.status == "PASS"


def test_labels_stay_bounded(reg: CollectorRegistry) -> None:
    m = metrics_on(reg)
    m.judge_call("some-new-provider", "error_something_new", 0.5)
    m.judge_call("openai", "verdict", None)
    assert sample(reg, "agenttwin_judge_calls_total", provider="other", outcome="other") == 1
    assert sample(reg, "agenttwin_judge_calls_total", provider="openai", outcome="verdict") == 1
    assert sample(reg, "agenttwin_judge_call_duration_seconds_count", provider="openai") == 0
    # Every judge error kind has its own outcome label.
    kinds = ("unavailable", "rate_limited", "timeout", "rejected", "malformed", "refused")
    assert {f"error_{k}" for k in kinds} <= JUDGE_OUTCOMES


def test_a_run_counts_its_status_duration_and_cases(reg: CollectorRegistry) -> None:
    m = metrics_on(reg)
    created = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    finished = created + timedelta(seconds=42)
    m.run_ended({"status": "COMPLETED", "created_at": created, "finished_at": finished})
    m.run_ended({"status": "FAILED", "created_at": created, "finished_at": None})
    m.cases_compared({"NEW_CRITICAL_FAILURE": 2, "REGRESSED": 1, "UNCHANGED": 0, "SOMETHING": 3})

    assert sample(reg, "agenttwin_eval_runs_total", status="COMPLETED") == 1
    assert sample(reg, "agenttwin_eval_runs_total", status="FAILED") == 1
    assert sample(reg, "agenttwin_eval_run_duration_seconds_sum", status="COMPLETED") == 42
    # A run without a finish time counts, but has no duration to observe.
    assert sample(reg, "agenttwin_eval_run_duration_seconds_count", status="FAILED") == 0
    assert sample(reg, "agenttwin_eval_cases_total", classification="NEW_CRITICAL_FAILURE") == 2
    assert sample(reg, "agenttwin_eval_cases_total", classification="REGRESSED") == 1
    assert sample(reg, "agenttwin_eval_cases_total", classification="SOMETHING") == 0


def test_runs_in_progress_reset_statuses_that_emptied(reg: CollectorRegistry) -> None:
    m = metrics_on(reg)
    m.active({"QUEUED": 3, "RUNNING": 1})
    assert sample(reg, "agenttwin_eval_runs_in_progress", status="QUEUED") == 3
    m.active({"RUNNING": 2})
    # A status no row has any more reads 0, not its last value.
    assert sample(reg, "agenttwin_eval_runs_in_progress", status="QUEUED") == 0
    assert sample(reg, "agenttwin_eval_runs_in_progress", status="RUNNING") == 2
    assert sample(reg, "agenttwin_eval_runs_in_progress", status="EVALUATING") == 0
