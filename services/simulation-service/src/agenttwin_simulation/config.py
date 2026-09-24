"""simulation-service configuration (environment variables)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from agenttwin_core.config import Loader

__all__ = ["AgentEndpoint", "SimulationConfig", "load_config", "parse_agent_endpoints"]

_AGENT_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


@dataclass(frozen=True)
class AgentEndpoint:
    """Where an agent under test is reached (operator configuration only:
    run requests cannot supply URLs, so simulations cannot be pointed at
    arbitrary hosts)."""

    url: str
    token_env: str | None = None


@dataclass(frozen=True)
class SimulationConfig:
    control_plane_url: str
    trace_service_url: str
    # How agents reach this service's twin endpoint (e.g. http://simulation-service:8084/twin/v1).
    twin_public_url: str
    agent_endpoints: dict[str, AgentEndpoint] = field(default_factory=dict)
    agent_tokens: dict[str, str] = field(default_factory=dict)
    worker_concurrency: int = 2
    lease_seconds: float = 60.0
    poll_interval_s: float = 2.0
    case_timeout_s: float = 120.0
    max_calls_per_case: int = 200
    max_fault_delay_ms: int = 30_000
    outcome_retry_s: float = 3.0
    outcome_max_attempts: int = 40
    max_run_attempts: int = 3


def parse_agent_endpoints(raw: str) -> dict[str, AgentEndpoint]:
    """``{"<agent>": {"url": "http://host:port", "token_env": "ENV_NAME"}}``."""
    if not raw.strip():
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("must be a JSON object mapping agent names to endpoints")
    out: dict[str, AgentEndpoint] = {}
    for name, spec in data.items():
        if not isinstance(name, str) or not _AGENT_NAME.match(name):
            raise ValueError(f"invalid agent name {name!r}")
        if isinstance(spec, str):
            spec = {"url": spec}
        if not isinstance(spec, dict):
            raise ValueError(f"{name}: endpoint must be an object")
        url = str(spec.get("url") or "")
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError(f"{name}: url must be an absolute http(s) URL")
        token_env = spec.get("token_env")
        if token_env is not None and (not isinstance(token_env, str) or not _ENV_NAME.match(token_env)):
            raise ValueError(f"{name}: token_env must be an environment variable name")
        out[name] = AgentEndpoint(url=url.rstrip("/"), token_env=token_env)
    return out


def load_config(loader: Loader) -> SimulationConfig:
    none: dict[str, AgentEndpoint] = {}
    endpoints = loader.parsed("SIMULATION_AGENT_ENDPOINTS", none, parse_agent_endpoints)
    tokens: dict[str, str] = {}
    for name, ep in endpoints.items():
        if ep.token_env:
            value = loader.string(ep.token_env)
            loader.check(bool(value), ep.token_env, f"is required by SIMULATION_AGENT_ENDPOINTS ({name})")
            tokens[name] = value
    twin_url = loader.string("SIMULATION_TWIN_PUBLIC_URL", "http://simulation-service:8084/twin/v1").rstrip(
        "/"
    )
    loader.check(
        urlparse(twin_url).scheme in ("http", "https"), "SIMULATION_TWIN_PUBLIC_URL", "must be an http(s) URL"
    )
    return SimulationConfig(
        control_plane_url=loader.string("CONTROL_PLANE_URL", "http://control-plane:8080").rstrip("/"),
        trace_service_url=loader.string("TRACE_SERVICE_URL", "http://trace-service:8081").rstrip("/"),
        twin_public_url=twin_url,
        agent_endpoints=endpoints,
        agent_tokens=tokens,
        worker_concurrency=loader.integer("SIMULATION_WORKER_CONCURRENCY", 2, 1, 32),
        lease_seconds=loader.duration("SIMULATION_LEASE_DURATION", 60.0),
        poll_interval_s=loader.duration("SIMULATION_POLL_INTERVAL", 2.0),
        case_timeout_s=loader.duration("SIMULATION_CASE_TIMEOUT", 120.0),
        max_calls_per_case=loader.integer("SIMULATION_MAX_CALLS_PER_CASE", 200, 1, 10_000),
        max_fault_delay_ms=loader.integer("SIMULATION_MAX_FAULT_DELAY_MS", 30_000, 0, 600_000),
        outcome_retry_s=loader.duration("SIMULATION_OUTCOME_RETRY", 3.0),
        outcome_max_attempts=loader.integer("SIMULATION_OUTCOME_MAX_ATTEMPTS", 40, 1, 1000),
    )
