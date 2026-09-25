"""simulation-service configuration: agent endpoints come from the operator
only, their tokens from named environment variables, and every problem is
reported at once."""

from __future__ import annotations

import json
import re

import pytest

from agenttwin_core.config import ConfigError, Loader
from agenttwin_core.embeddings import EmbeddingSettings
from agenttwin_simulation.config import AgentEndpoint, load_config, parse_agent_endpoints


def test_defaults_point_at_the_compose_services() -> None:
    loader = Loader({})
    cfg = load_config(loader)
    loader.raise_for_errors()
    assert cfg.control_plane_url == "http://control-plane:8080"
    assert cfg.trace_service_url == "http://trace-service:8081"
    assert cfg.twin_public_url == "http://simulation-service:8084/twin/v1"
    assert (cfg.agent_endpoints, cfg.agent_tokens) == ({}, {})
    assert (cfg.worker_concurrency, cfg.lease_seconds, cfg.poll_interval_s) == (2, 60.0, 2.0)
    assert (cfg.max_calls_per_case, cfg.max_fault_delay_ms, cfg.max_run_attempts) == (200, 30_000, 3)
    # Scenario matching embeds locally unless a hosted model is configured.
    assert cfg.embedding == EmbeddingSettings()


def test_a_hosted_embedding_model_is_configured() -> None:
    loader = Loader(
        {
            "EMBEDDING_PROVIDER": "openai_compatible",
            "EMBEDDING_MODEL": "text-embedding-3-small",
            "EMBEDDING_DIMENSIONS": "512",
            "EMBEDDING_API_KEY": "sk-test",
            "EMBEDDING_SEND_DIMENSIONS": "true",
        }
    )
    cfg = load_config(loader)
    loader.raise_for_errors()
    assert (cfg.embedding.provider, cfg.embedding.model, cfg.embedding.dims) == (
        "openai_compatible",
        "text-embedding-3-small",
        512,
    )
    assert cfg.embedding.send_dimensions and cfg.embedding.base_url == "https://api.openai.com/v1"
    loader = Loader({"EMBEDDING_PROVIDER": "openai_compatible"})
    load_config(loader)
    with pytest.raises(ConfigError, match="EMBEDDING_MODEL"):
        loader.raise_for_errors()


def test_endpoints_and_their_tokens() -> None:
    loader = Loader(
        {
            "SIMULATION_AGENT_ENDPOINTS": json.dumps(
                {
                    "support-refund-agent": {
                        "url": "http://demo-agent:8090/",
                        "token_env": "DEMO_AGENT_TOKEN",
                    },
                    "faq-bot": "https://faq.internal",
                }
            ),
            "DEMO_AGENT_TOKEN": "secret-token",
            "SIMULATION_TWIN_PUBLIC_URL": "http://sim:8084/twin/v1/",
            "SIMULATION_WORKER_CONCURRENCY": "4",
            "SIMULATION_CASE_TIMEOUT": "90s",
        }
    )
    cfg = load_config(loader)
    loader.raise_for_errors()
    assert cfg.agent_endpoints == {
        "support-refund-agent": AgentEndpoint(url="http://demo-agent:8090", token_env="DEMO_AGENT_TOKEN"),
        "faq-bot": AgentEndpoint(url="https://faq.internal"),
    }
    assert cfg.agent_tokens == {"support-refund-agent": "secret-token"}
    assert cfg.twin_public_url == "http://sim:8084/twin/v1"
    assert (cfg.worker_concurrency, cfg.case_timeout_s) == (4, 90.0)


def test_problems_are_reported_together() -> None:
    loader = Loader(
        {
            "SIMULATION_AGENT_ENDPOINTS": '{"agent": {"url": "http://a", "token_env": "MISSING_TOKEN"}}',
            "SIMULATION_TWIN_PUBLIC_URL": "ftp://nope",
            "SIMULATION_WORKER_CONCURRENCY": "0",
        }
    )
    load_config(loader)
    with pytest.raises(ConfigError) as err:
        loader.raise_for_errors()
    message = str(err.value)
    for key in ("MISSING_TOKEN", "SIMULATION_TWIN_PUBLIC_URL", "SIMULATION_WORKER_CONCURRENCY"):
        assert key in message, message


@pytest.mark.parametrize(
    ("raw", "problem"),
    [
        ("[]", "must be a JSON object"),
        ('{"Bad Name": "http://a"}', "invalid agent name"),
        ('{"a": 42}', "endpoint must be an object"),
        ('{"a": {"url": "file:///etc/passwd"}}', "absolute http(s) URL"),
        ('{"a": {"url": "http://"}}', "absolute http(s) URL"),
        ('{"a": {"url": "http://a", "token_env": "lower"}}', "environment variable name"),
    ],
)
def test_invalid_endpoints_are_refused(raw: str, problem: str) -> None:
    with pytest.raises(ValueError, match=re.escape(problem)):
        parse_agent_endpoints(raw)
    loader = Loader({"SIMULATION_AGENT_ENDPOINTS": raw})
    load_config(loader)
    with pytest.raises(ConfigError, match="SIMULATION_AGENT_ENDPOINTS"):
        loader.raise_for_errors()


def test_empty_endpoints_mean_no_runnable_agent() -> None:
    assert parse_agent_endpoints("  ") == {}
