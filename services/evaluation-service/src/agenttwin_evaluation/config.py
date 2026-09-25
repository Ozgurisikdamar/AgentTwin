"""evaluation-service configuration (environment variables)."""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

from agenttwin_core.config import Loader
from agenttwin_core.embeddings import EmbeddingSettings, load_embedding_settings
from agenttwin_evaluation.judges import JudgeSettings

__all__ = ["EvaluationConfig", "load_config"]


@dataclass(frozen=True)
class EvaluationConfig:
    simulation_service_url: str
    trace_service_url: str
    judge: JudgeSettings = field(default_factory=JudgeSettings)
    # What one evaluation run may spend on judging (ADR-0022); unset cost = calls only.
    judge_budget_usd: float | None = 5.0
    judge_max_calls: int = 200
    judge_call_timeout_s: float = 120.0
    worker_concurrency: int = 2
    lease_seconds: float = 60.0
    poll_interval_s: float = 2.0
    # How long a run waits for its two simulations before it fails.
    max_wait_s: float = 3600.0
    max_run_attempts: int = 3
    # Parallel requests to the simulation and trace services while collecting cases.
    fetch_concurrency: int = 8
    # Regression mining (ADR-0032): a failure joins the group of its nearest
    # neighbour when their features are at least this similar (cosine).
    regression_similarity_threshold: float = 0.85
    embedding: EmbeddingSettings = field(default_factory=EmbeddingSettings)


def _url(loader: Loader, key: str, default: str) -> str:
    value = loader.string(key, default).rstrip("/")
    loader.check(urlparse(value).scheme in ("http", "https"), key, "must be an http(s) URL")
    return value


def _optional_price(loader: Loader, key: str) -> float | None:
    raw = loader.string(key)
    if not raw:
        return None
    return loader.number(key, 0.0, 0.0, 10_000.0)


def load_config(loader: Loader) -> EvaluationConfig:
    provider = loader.one_of("JUDGE_PROVIDER", "fake", "fake", "anthropic", "openai")
    model = loader.string("JUDGE_MODEL")
    api_key = loader.string("JUDGE_API_KEY")
    if provider != "fake":
        loader.check(bool(model), "JUDGE_MODEL", f"is required for the {provider} judge")
        loader.check(bool(api_key), "JUDGE_API_KEY", f"is required for the {provider} judge")
    base_url = loader.string("JUDGE_BASE_URL").rstrip("/")
    if base_url:
        loader.check(
            urlparse(base_url).scheme in ("http", "https"), "JUDGE_BASE_URL", "must be an http(s) URL"
        )
    seed = loader.integer("JUDGE_SEED", 7, -1, 2**31 - 1)
    judge = JudgeSettings(
        provider=provider,
        model=model,
        api_key=api_key,
        base_url=base_url,
        timeout_s=loader.duration("JUDGE_TIMEOUT", 30.0),
        max_retries=loader.integer("JUDGE_MAX_RETRIES", 2, 0, 10),
        temperature=0.0,
        seed=None if seed < 0 else seed,
        max_output_tokens=loader.integer("JUDGE_MAX_OUTPUT_TOKENS", 1024, 256, 8192),
        input_usd_per_mtok=_optional_price(loader, "JUDGE_INPUT_USD_PER_MTOK"),
        output_usd_per_mtok=_optional_price(loader, "JUDGE_OUTPUT_USD_PER_MTOK"),
    )
    budget_raw = loader.string("EVALUATION_JUDGE_BUDGET_USD", "5")
    budget = (
        None if budget_raw in ("", "none") else loader.number("EVALUATION_JUDGE_BUDGET_USD", 5.0, 0.0, 1e6)
    )
    return EvaluationConfig(
        simulation_service_url=_url(loader, "SIMULATION_SERVICE_URL", "http://simulation-service:8084"),
        trace_service_url=_url(loader, "TRACE_SERVICE_URL", "http://trace-service:8081"),
        judge=judge,
        judge_budget_usd=budget,
        judge_max_calls=loader.integer("EVALUATION_JUDGE_MAX_CALLS", 200, 0, 100_000),
        judge_call_timeout_s=loader.duration("EVALUATION_JUDGE_CALL_TIMEOUT", 120.0),
        worker_concurrency=loader.integer("EVALUATION_WORKER_CONCURRENCY", 2, 1, 32),
        lease_seconds=loader.duration("EVALUATION_LEASE_DURATION", 60.0),
        poll_interval_s=loader.duration("EVALUATION_POLL_INTERVAL", 2.0),
        max_wait_s=loader.duration("EVALUATION_MAX_WAIT", 3600.0),
        max_run_attempts=loader.integer("EVALUATION_MAX_RUN_ATTEMPTS", 3, 1, 20),
        fetch_concurrency=loader.integer("EVALUATION_FETCH_CONCURRENCY", 8, 1, 64),
        regression_similarity_threshold=loader.number("REGRESSION_SIMILARITY_THRESHOLD", 0.85, 0.0, 1.0),
        embedding=load_embedding_settings(loader),
    )
