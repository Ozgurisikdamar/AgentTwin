"""SDK configuration from keyword arguments and ``AGENTTWIN_*`` variables."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Literal

from agenttwin.redaction import RedactionConfig, Strategy

__all__ = ["Config", "ContentMode", "Source"]

ContentMode = Literal["off", "redacted", "full"]
Source = Literal["production", "simulation", "replay", "eval", "test"]

_CONTENT_MODES: tuple[ContentMode, ...] = ("off", "redacted", "full")
_SOURCES: tuple[Source, ...] = ("production", "simulation", "replay", "eval", "test")
_STRATEGIES: tuple[Strategy, ...] = ("mask", "hash", "drop")


@dataclass(frozen=True)
class Config:
    """Everything the SDK needs; every field has a safe default.

    Content capture is **off** by default (ADR-0008): only metadata, hashes
    and counts leave the process unless ``content_mode`` is set explicitly.
    """

    api_key: str | None = None
    #: OTLP/HTTP base URL (the collector); ``/v1/traces`` is appended.
    otlp_endpoint: str = "http://localhost:4318"
    #: AgentTwin API base URL, used for delayed outcome reporting.
    api_url: str | None = None
    project: str | None = None
    service_name: str | None = None
    environment: str = "development"
    source: Source = "production"
    release_id: str | None = None
    commit_sha: str | None = None
    content_mode: ContentMode = "off"
    redaction: RedactionConfig = field(default_factory=RedactionConfig)
    #: Head sampling ratio for new traces (parent decision is respected).
    sample_ratio: float = 1.0
    #: Bounded in-memory export queue; spans beyond it are dropped, never block.
    max_queue_size: int = 2048
    max_export_batch_size: int = 512
    schedule_delay_ms: int = 500
    export_timeout_s: float = 5.0
    #: Upper bound for any single content attribute (bytes, UTF-8).
    max_content_bytes: int = 8192
    enabled: bool = True

    def __post_init__(self) -> None:
        if self.content_mode not in _CONTENT_MODES:
            raise ValueError(f"content_mode must be one of {_CONTENT_MODES}, got {self.content_mode!r}")
        if self.source not in _SOURCES:
            raise ValueError(f"source must be one of {_SOURCES}, got {self.source!r}")
        if not 0.0 <= self.sample_ratio <= 1.0:
            raise ValueError("sample_ratio must be between 0 and 1")
        if self.max_queue_size < 1 or self.max_export_batch_size < 1:
            raise ValueError("queue and batch sizes must be positive")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **overrides: object) -> Config:
        """Build a configuration from ``AGENTTWIN_*`` variables; keyword
        arguments win over the environment."""
        e = os.environ if env is None else env

        def get(name: str) -> str | None:
            v = e.get(name)
            return v if v not in (None, "") else None

        values: dict[str, object] = {}
        if (v := get("AGENTTWIN_API_KEY")) is not None:
            values["api_key"] = v
        if (v := get("AGENTTWIN_OTLP_ENDPOINT") or get("OTEL_EXPORTER_OTLP_ENDPOINT")) is not None:
            values["otlp_endpoint"] = v
        if (v := get("AGENTTWIN_API_URL")) is not None:
            values["api_url"] = v
        for env_name, attr in (
            ("AGENTTWIN_PROJECT", "project"),
            ("AGENTTWIN_SERVICE_NAME", "service_name"),
            ("AGENTTWIN_ENVIRONMENT", "environment"),
            ("AGENTTWIN_RELEASE_ID", "release_id"),
            ("AGENTTWIN_COMMIT_SHA", "commit_sha"),
        ):
            if (v := get(env_name)) is not None:
                values[attr] = v
        if (v := get("AGENTTWIN_SOURCE")) is not None:
            values["source"] = v
        if (v := get("AGENTTWIN_CONTENT_MODE")) is not None:
            values["content_mode"] = v
        if (v := get("AGENTTWIN_SAMPLE_RATIO")) is not None:
            values["sample_ratio"] = float(v)
        if (v := get("AGENTTWIN_DISABLED")) is not None:
            values["enabled"] = v.lower() not in ("1", "true", "yes")
        strategy = get("AGENTTWIN_REDACTION_STRATEGY")
        custom = get("AGENTTWIN_REDACTION_PATTERNS")
        paths = get("AGENTTWIN_REDACTION_JSON_PATHS")
        if strategy or custom or paths:
            if strategy is not None and strategy not in _STRATEGIES:
                raise ValueError(f"AGENTTWIN_REDACTION_STRATEGY must be one of {_STRATEGIES}")
            values["redaction"] = RedactionConfig(
                strategy=strategy if strategy in _STRATEGIES else "mask",
                custom_patterns=tuple(p for p in (custom or "").split("\n") if p),
                json_paths=tuple(p.strip() for p in (paths or "").split(",") if p.strip()),
            )
        values.update(overrides)
        return replace(cls(), **values)  # type: ignore[arg-type]
