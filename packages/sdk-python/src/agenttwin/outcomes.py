"""Delayed outcome reporting over the AgentTwin REST API.

Some outcomes are only known after the agent run ended (a refund settles, a
customer replies). ``report_outcome`` attaches such an outcome to the trace
through ``POST /api/v1/traces/{trace_id}/outcome`` using the project API key
(scope ``traces:write``). It uses only the standard library.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agenttwin.api import APIError, Client
from agenttwin.config import Config
from agenttwin.tracing import OUTCOME_STATUSES, VERIFICATION_SOURCES, OutcomeStatus, VerificationSource

__all__ = ["OutcomeReportError", "report_outcome"]


class OutcomeReportError(APIError):
    """The API rejected or could not accept the outcome."""


def report_outcome(
    trace_id: str,
    status: OutcomeStatus,
    *,
    config: Config | None = None,
    business_outcome: str | None = None,
    verified: bool = False,
    verification_source: VerificationSource = "external_callback",
    claimed_status: OutcomeStatus | None = None,
    expected_state: Mapping[str, Any] | None = None,
    actual_state: Mapping[str, Any] | None = None,
    notes: str | None = None,
    timeout_s: float = 5.0,
) -> dict[str, Any]:
    """Record an outcome for ``trace_id``; returns the stored outcome.

    Raises OutcomeReportError on API errors and ValueError on invalid input.
    """
    cfg = config or Config.from_env()
    if not cfg.api_url or not cfg.api_key:
        raise ValueError("report_outcome needs api_url and api_key (AGENTTWIN_API_URL / AGENTTWIN_API_KEY)")
    if status not in OUTCOME_STATUSES or (
        claimed_status is not None and claimed_status not in OUTCOME_STATUSES
    ):
        raise ValueError(f"status must be one of {sorted(OUTCOME_STATUSES)}")
    if verification_source not in VERIFICATION_SOURCES:
        raise ValueError(f"verification_source must be one of {sorted(VERIFICATION_SOURCES)}")
    if len(trace_id) != 32 or any(c not in "0123456789abcdef" for c in trace_id.lower()):
        raise ValueError("trace_id must be 32 hex characters")
    body: dict[str, Any] = {
        "status": status,
        "verified": verified,
        "verification_source": verification_source,
    }
    for key, value in (
        ("business_outcome", business_outcome),
        ("claimed_status", claimed_status),
        ("expected_state", expected_state),
        ("actual_state", actual_state),
        ("notes", notes),
    ):
        if value is not None:
            body[key] = value
    client = Client(cfg.api_url, cfg.api_key, timeout_s=timeout_s)
    try:
        return client.record_outcome(trace_id, body)
    except APIError as err:
        raise OutcomeReportError(err.status, err.code, err.message, err.details) from None
