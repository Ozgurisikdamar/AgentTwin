"""Payloads of the simulation API that its contract accepts, for the fakes
that stand in for the service in client tests (the Python SDK, the demo
seed).

Each builder returns a complete response object; a test overrides the fields
it cares about. :class:`ExchangeChecker` holds every exchange a fake serves
to the contract (ADR-0021), so a fake cannot answer what the service never
would, and a client cannot send what the service would reject, without a
test failing.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx

from agenttwin_core.openapi_contract import Contract, ContractViolation, contract_path

__all__ = [
    "ExchangeChecker",
    "case_summary",
    "error",
    "queued_case",
    "run",
    "run_detail",
    "scenario",
    "twin",
    "twin_summary",
    "uuid",
]

NOW = "2026-01-01T00:00:00Z"
ACTOR = "apikey:test"
ORGANIZATION = "0190f3b4-0000-7000-8000-00000000a000"
PROJECT = "0190f3b4-0000-7000-8000-00000000b000"


def uuid(n: int) -> str:
    """A fixed, valid (canonical, lower-case) UUID for test data."""
    return f"0190f3b4-0000-7000-8000-{n:012x}"


def twin_summary(**over: Any) -> dict[str, Any]:
    return {
        "id": uuid(0xC001),
        "organization_id": ORGANIZATION,
        "project_id": PROJECT,
        "name": "demo-co-support",
        "version": 1,
        "spec_hash": "a" * 64,
        "tool_count": 1,
        "description": None,
        "created_by": ACTOR,
        "created_at": NOW,
    } | over


def twin(**over: Any) -> dict[str, Any]:
    """A twin with its tools (``registerTwin``)."""
    tools = [
        {
            "name": "lookup_order",
            "description": "Looks up an order.",
            "risk": "READ",
            "handler": "read",
            "mutates": False,
            "idempotency": None,
            "tenant_scoped": True,
        }
    ]
    return twin_summary(tool_count=len(tools)) | {"tools": tools} | over


def scenario(**over: Any) -> dict[str, Any]:
    return {
        "id": uuid(0xD001),
        "organization_id": ORGANIZATION,
        "project_id": PROJECT,
        "name": "refund-happy-path",
        "agent": "support-refund-agent",
        "twin": "demo-co-support",
        "severity": "critical",
        "tags": [],
        "source": "manual",
        "latest_version": 1,
        "archived": False,
        "created_by": ACTOR,
        "created_at": NOW,
        "updated_at": NOW,
        "version_id": uuid(0xD101),
        "spec_hash": "b" * 64,
    } | over


_FINAL = ("COMPLETED", "FAILED", "CANCELLED")


def run(**over: Any) -> dict[str, Any]:
    """A run with its pinning (``startSimulation``, ``getSimulation``). The
    counts are consistent with ``status`` unless overridden."""
    status = over.get("status", "QUEUED")
    final = status in _FINAL
    agent, version = over.get("agent_name", "support-refund-agent"), over.get("agent_version", "1.2.4")
    return {
        "id": uuid(0xE001),
        "organization_id": ORGANIZATION,
        "project_id": PROJECT,
        "agent_name": agent,
        "agent_version": version,
        "agent_version_id": None,
        "side": "SINGLE",
        "eval_run_id": None,
        "release_id": None,
        "status": status,
        "requested_by": ACTOR,
        "cancel_requested": False,
        "attempts": 0 if status == "QUEUED" else 1,
        "case_count": 0,
        "passed": 0,
        "failed": 0,
        "errored": 0,
        "cancelled": 0,
        "critical_failures": 0,
        "error": None,
        "created_at": NOW,
        "started_at": None if status == "QUEUED" else NOW,
        "finished_at": NOW if final else None,
        "updated_at": NOW,
        "finished_cases": 0,
        "pinning": {
            "correlation_id": "sim-test",
            "seed": 42,
            "agent": {
                "name": agent,
                "version": version,
                "version_id": None,
                "manifest_sha256": None,
                "prompt_sha256": None,
                "model_provider": None,
                "model_name": None,
                "commit_sha": None,
            },
            "scenarios": [],
            "evaluators": {"expectation.toolCalled": "1.0.0"},
            "engine": "twin-engine/1.0.0",
            "selection": {"scenarios": None, "tags": None},
        },
    } | over


def queued_case(position: int, name: str, **over: Any) -> dict[str, Any]:
    """A case as ``startSimulation`` lists it."""
    return {
        "id": uuid(0xF000 + position),
        "position": position,
        "scenario_id": uuid(0xD000 + position),
        "scenario_name": name,
        "severity": "critical",
        "seed": 42 + position,
        "tenant": None,
    } | over


def case_summary(position: int, name: str, status: str, **over: Any) -> dict[str, Any]:
    """A case of a run (``getSimulation``)."""
    final = status in ("PASSED", "FAILED", "ERRORED", "CANCELLED")
    return {
        "id": uuid(0xF000 + position),
        "run_id": uuid(0xE001),
        "position": position,
        "scenario_id": uuid(0xD000 + position),
        "scenario_version_id": uuid(0xD100 + position),
        "scenario_name": name,
        "severity": "critical",
        "twin_definition_id": uuid(0xC101),
        "status": status,
        "seed": 42 + position,
        "tenant": None,
        "call_count": 0,
        "trace_id": None,
        "reason": None,
        "error": None,
        "latency_ms": None,
        "outcome_status": "none",
        "started_at": NOW if status != "PENDING" else None,
        "finished_at": NOW if final else None,
        "labels": [],
        "score": 1.0 if status == "PASSED" else (0.0 if status == "FAILED" else None),
    } | over


def run_detail(run_: Mapping[str, Any], cases: list[dict[str, Any]]) -> dict[str, Any]:
    """``getSimulation``: the run, its cases and no transitions."""
    return {"run": dict(run_), "cases": cases, "transitions": []}


def error(code: str, message: str, **details: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"code": code, "message": message, "request_id": "0" * 32}
    if details:
        body["details"] = details
    return {"error": body}


class ExchangeChecker:
    """Checks the exchanges a fake serves on the simulation API against its
    contract. Violations are kept rather than raised: a fake's handler runs
    on a server thread, where an exception would not reach the test — the
    test asserts :attr:`violations` is empty."""

    PREFIXES = ("/api/v1/twins", "/api/v1/scenarios", "/api/v1/simulations")

    def __init__(self) -> None:
        self.contract = Contract.load(contract_path("simulation-service"))
        self.violations: list[str] = []

    def check(
        self,
        method: str,
        target: str,
        headers: Mapping[str, str],
        body: bytes,
        status: int,
        payload: Any,
    ) -> None:
        """One exchange: ``target`` is the request target (path and query).
        The answer is always checked; the request when it was accepted."""
        if not urlsplit(target).path.startswith(self.PREFIXES):
            return
        request = httpx.Request(method, f"http://fake{target}", headers=dict(headers), content=body)
        response = httpx.Response(status, json=payload, request=request)
        try:
            self.contract.check_exchange(request, response)
        except ContractViolation as err:
            self.violations.append(str(err))

    def succeeded(self) -> set[str]:
        """The operations with at least one checked successful exchange."""
        return {op for op, statuses in self.contract.seen.items() if any(200 <= s < 300 for s in statuses)}
