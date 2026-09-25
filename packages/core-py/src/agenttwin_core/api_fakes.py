"""Payloads of the AgentTwin APIs that their contracts accept, for the fakes
that stand in for a service in tests: the Python SDK's stub, the demo seed's
fake API, and the simulation service's fakes of the control plane and the
trace service.

Each builder returns a complete response object; a test overrides the fields
it cares about. :class:`ExchangeChecker` holds every exchange a fake serves
to the contract of the service that owns the path (ADR-0021), so a fake
cannot answer what the service never would, and a client cannot send what the
service would reject, without a test failing.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx

from agenttwin_core.openapi_contract import Contract, ContractViolation, contract_path

__all__ = [
    "ExchangeChecker",
    "agent",
    "agent_result",
    "agent_version",
    "agent_version_detail",
    "case_detail",
    "case_summary",
    "error",
    "expectation_result",
    "expectation_spec",
    "manifest",
    "outcome",
    "project",
    "queued_case",
    "registered_version",
    "retrieval_step",
    "run",
    "run_detail",
    "scenario",
    "tool_step",
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


def sha256(c: str) -> str:
    """A fixed, valid SHA-256 digest (64 hex characters) for test data."""
    return (c * 64)[:64]


# ------------------------------------------------------------ control plane


def project(**over: Any) -> dict[str, Any]:
    """A project (``listProjects``, ``getProject``)."""
    return {
        "id": PROJECT,
        "organization_id": ORGANIZATION,
        "slug": "support",
        "name": "Customer Support",
        "description": "",
        "content_mode": "redacted",
        "store_prompt_text": False,
        "trace_retention_days": 30,
        "content_retention_days": 7,
        "artifact_retention_days": 30,
        "gate_policy": {},
        "created_by": ACTOR,
        "updated_by": ACTOR,
        "created_at": NOW,
        "updated_at": NOW,
    } | over


def agent(**over: Any) -> dict[str, Any]:
    return {
        "id": uuid(0xB101),
        "organization_id": ORGANIZATION,
        "project_id": PROJECT,
        "name": "support-refund-agent",
        "description": "",
        "created_by": ACTOR,
        "created_at": NOW,
        "version_count": 1,
        "latest_version": "1.2.4",
    } | over


def manifest(**over: Any) -> dict[str, Any]:
    """A normalized manifest, as a registered version carries it."""
    return {
        "name": "support-refund-agent",
        "version": "1.2.4",
        "model": {"provider": "scripted", "name": "scripted-planner-v1", "temperature": 0},
        "limits": {"max_steps": 30},
        "tools": [
            {"name": "lookup_order", "risk": "READ", "risk_declared": True, "definition_sha256": sha256("1")}
        ],
        "content_mode": "redacted",
    } | over


def agent_version(**over: Any) -> dict[str, Any]:
    """An agent version as the version list answers it (``listAgentVersions``)."""
    version = over.get("version", "1.2.4")
    return {
        "id": uuid(0xB201),
        "agent_id": uuid(0xB101),
        "agent_name": "support-refund-agent",
        "project_id": PROJECT,
        "version": version,
        "manifest": manifest(version=version),
        "manifest_sha256": sha256("a"),
        "prompt_sha256": sha256("c"),
        "model_provider": "scripted",
        "model_name": "scripted-planner-v1",
        "model_params": {"temperature": 0},
        "created_by": ACTOR,
        "created_at": NOW,
    } | over


def agent_version_detail(**over: Any) -> dict[str, Any]:
    """A version with the tool versions it is bound to (``getAgentVersion``,
    ``findAgentVersionInternal``)."""
    tools = [{"name": "lookup_order", "tool_version": 1, "risk": "READ", "definition_sha256": sha256("1")}]
    return agent_version(**over) | {"tools": over.get("tools", tools)}


def registered_version(*, created: bool = True, **over: Any) -> dict[str, Any]:
    """``registerManifest``: 201 when ``created``, 200 for identical content."""
    version = agent_version(**over)
    return {"agent": agent(latest_version=version["version"]), "version": version, "created": created}


# ------------------------------------------------------------ trace service


def outcome(**over: Any) -> dict[str, Any]:
    """A stored outcome (``recordOutcome``). ``contradiction`` follows the
    claimed and the verified status unless overridden."""
    body = {
        "status": "SUCCESS",
        "business_outcome": None,
        "verified": False,
        "verification_source": "external_callback",
        "claimed_status": None,
        "notes": None,
        "source": "api",
        "recorded_by": ACTOR,
        "recorded_at": NOW,
    } | over
    claimed = body["claimed_status"]
    return {
        "contradiction": bool(body["verified"] and claimed is not None and claimed != body["status"])
    } | body


# ------------------------------------------------------------ simulation service


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


def tool_step(seq: int, tool: str, arguments: Mapping[str, Any] | None = None, **over: Any) -> dict[str, Any]:
    """A tool call as the twin recorded it (a ``Step`` of ``getSimulationCase``).
    ``over`` sets fields of the record; ``latency_ms`` sets the step's."""
    latency = over.pop("latency_ms", 5.0)
    http = over.get("http_status", 200)
    record = {
        "seq": seq,
        "call_number": 1,
        "tool": tool,
        "arguments": dict(arguments or {}),
        "http_status": http,
        "status": "ok",
        "error_code": None,
        "response": {},
        "risk": "READ",
        "fault": None,
        "mutated": False,
        "expects_mutation": False,
        "replayed": False,
        "effect_key": None,
        "cross_tenant": None,
        "policy_violation": None,
        "redelivered": False,
        "changes": [],
        "delay_ms": 0,
    } | over
    return {
        "seq": seq,
        "kind": "tool_call",
        "tool": tool,
        "latency_ms": latency,
        "created_at": NOW,
        "record": record,
    }


def retrieval_step(seq: int, query: str, documents: list[str] | None = None) -> dict[str, Any]:
    record = {
        "seq": seq,
        "kind": "retrieval",
        "query": query,
        "limit": 3,
        "documents": [{"id": d, "trusted": True} for d in documents or []],
    }
    return {
        "seq": seq,
        "kind": "retrieval",
        "tool": None,
        "latency_ms": 1.0,
        "created_at": NOW,
        "record": record,
    }


def expectation_result(
    expectation_id: str,
    status: str = "PASS",
    *,
    type_: str = "toolCalled",
    critical: bool = False,
    **over: Any,
) -> dict[str, Any]:
    """One expectation result of a case (``ExpectationResult``)."""
    return {
        "status": status,
        "reason": over.pop("reason", f"{expectation_id}: {status.lower()}"),
        "evaluator": f"expectation.{type_}",
        "evaluator_version": "1.0.0",
        "score": {"PASS": 1.0, "FAIL": 0.0}.get(status),
        "label": None,
        "critical": critical,
        "expectation": {"id": expectation_id, "type": type_, "critical": critical},
        "evidence": [],
    } | over


def agent_result(**over: Any) -> dict[str, Any]:
    """What the simulation kept of the agent's answer (``AgentResult``)."""
    return {
        "kind": "ok",
        "http_status": 200,
        "elapsed_ms": 80.0,
        "output": "Done.",
        "status": "completed",
        "agent_version": "1.2.4",
        "model": "scripted-planner",
        "model_kind": "deterministic-fake",
        "steps": 3,
        "claimed_outcome": "SUCCESS",
        "business_outcome": None,
        "tool_calls": [],
    } | over


# The fields scenario.v1 requires per expectation type, with a plausible
# value, so a case's embedded scenario document is valid (ADR-0021).
_REQUIRED_FIELDS: dict[str, dict[str, Any]] = {
    "state": {"path": "orders"},
    "stateUnchanged": {"path": "orders"},
    "outputJsonPath": {"path": "$.status", "equals": "ok"},
    "toolCalled": {"tool": "lookup_order"},
    "toolNotCalled": {"tool": "delete_customer"},
    "toolArgs": {"tool": "refund_payment", "path": "amount", "lte": 100},
    "toolStatus": {"tool": "refund_payment", "status": 200},
    "approvalRequired": {"tool": "refund_payment"},
    "maxToolCalls": {"value": 10},
    "maxRetries": {"value": 1},
    "maxSteps": {"value": 10},
    "maxLatencyMs": {"value": 5000},
    "maxCostUsd": {"value": 1},
    "order": {"mustCall": ["get_refund_policy"], "before": ["refund_payment"]},
    "finalOutcome": {"equals": "SUCCESS"},
    "outputContains": {"value": "refund"},
    "outputNotContains": {"value": "password"},
    "outputRegex": {"pattern": "refund"},
    "outputJsonSchema": {"schema": {"type": "object"}},
    "semantic": {"rubric": "The reply explains what happened to the refund."},
}


def expectation_spec(result: Mapping[str, Any]) -> dict[str, Any]:
    """The scenario expectation an ``expectation_result`` is for."""
    meta = result["expectation"]
    spec = {"id": meta["id"], "type": meta["type"], "critical": meta["critical"]}
    return spec | _REQUIRED_FIELDS.get(str(meta["type"]), {})


def case_detail(
    position: int,
    name: str,
    status: str,
    *,
    steps: list[dict[str, Any]] | None = None,
    results: list[dict[str, Any]] | None = None,
    agent: dict[str, Any] | None = None,
    initial: dict[str, Any] | None = None,
    final: dict[str, Any] | None = None,
    **over: Any,
) -> dict[str, Any]:
    """``getSimulationCase``: a finished case with its steps and results. The
    verdict is computed from ``results`` as the simulation worker would."""
    results = results if results is not None else [expectation_result("e1")]
    counts = {k: sum(1 for r in results if r["status"] == k) for k in ("PASS", "FAIL", "ERROR", "SKIPPED")}
    evaluated = counts["PASS"] + counts["FAIL"] + counts["ERROR"]
    verdict = None
    if status in ("PASSED", "FAILED", "ERRORED"):
        verdict = {
            "status": status,
            "reason": over.get("reason") or f"{name} {status.lower()}",
            "passed": counts["PASS"],
            "failed": counts["FAIL"],
            "errored": counts["ERROR"],
            "skipped": counts["SKIPPED"],
            "critical_failures": sum(1 for r in results if r["critical"] and r["status"] == "FAIL"),
            "score": counts["PASS"] / evaluated if evaluated else None,
            "labels": sorted({r["label"] for r in results if r["status"] == "FAIL" and r["label"]}),
        }
    steps = steps if steps is not None else []
    summary = (
        case_summary(
            position,
            name,
            status,
            call_count=sum(1 for s in steps if s["kind"] == "tool_call"),
            reason=verdict["reason"] if verdict else None,
            latency_ms=over.pop("latency_ms", 120.0),
            labels=verdict["labels"] if verdict else [],
            score=verdict["score"] if verdict else None,
        )
        | over
    )
    return {
        "case": summary
        | {
            "verdict": verdict,
            "results": results,
            "state_diff": [],
            "agent_result": agent if agent is not None else agent_result(),
        },
        "scenario": {
            "document": {
                "apiVersion": "agenttwin.dev/v1",
                "kind": "Scenario",
                "metadata": {"name": name, "severity": summary["severity"]},
                "spec": {
                    "agent": "support-refund-agent",
                    "twin": "demo-co-support",
                    "input": {"message": "Please refund my order."},
                    "expectations": [expectation_spec(r) for r in results],
                },
            },
            "faults": [],
        },
        "twin": {"id": uuid(0xC101), "name": "demo-co-support", "version": 1, "spec_hash": sha256("c")},
        "steps": steps,
        "state": {
            "initial": initial if initial is not None else {},
            "final": final if final is not None else {},
        },
    }


def error(code: str, message: str, **details: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"code": code, "message": message, "request_id": "0" * 32}
    if details:
        body["details"] = details
    return {"error": body}


# The service that owns each path (longest prefix first). Paths no service
# documents (``/health/ready``) are not checked.
_ROUTES: tuple[tuple[str, str], ...] = (
    ("/api/v1/trace-stats", "trace-service"),
    ("/api/v1/traces", "trace-service"),
    ("/v1/traces", "trace-service"),
    ("/api/v1/twins", "simulation-service"),
    ("/api/v1/scenarios", "simulation-service"),
    ("/api/v1/simulations", "simulation-service"),
    ("/twin/v1", "simulation-service"),
    ("/internal/v1", "control-plane"),
    ("/api/v1", "control-plane"),
)
_DOCUMENTS: dict[str, Contract] = {}


def owner(path: str) -> str | None:
    """The service whose contract documents ``path``, if any."""
    for prefix, service in _ROUTES:
        if path == prefix or path.startswith(prefix + "/"):
            return service
    return None


class ExchangeChecker:
    """Checks the exchanges a fake serves against the contract of the service
    that owns the path. Violations are kept rather than raised: a fake's
    handler runs on a server thread, where an exception would not reach the
    test — the test asserts :attr:`violations` is empty."""

    def __init__(self) -> None:
        self.contracts: dict[str, Contract] = {}
        for service in dict.fromkeys(s for _, s in _ROUTES):
            parsed = _DOCUMENTS.get(service)
            if parsed is None:
                parsed = _DOCUMENTS[service] = Contract.load(contract_path(service))
            # A fresh checker per instance: coverage is per test.
            self.contracts[service] = Contract(parsed.document, parsed.name)
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
        request = httpx.Request(method, f"http://fake{target}", headers=dict(headers), content=body)
        self.check_exchange(request, httpx.Response(status, json=payload, request=request))

    def check_exchange(self, request: httpx.Request, response: httpx.Response) -> None:
        """One exchange of an ``httpx`` fake (a ``MockTransport`` handler)."""
        service = owner(urlsplit(str(request.url)).path)
        if service is None:
            return
        try:
            self.contracts[service].check_exchange(request, response)
        except ContractViolation as err:
            self.violations.append(str(err))

    def succeeded(self) -> set[str]:
        """The operations with at least one checked successful exchange."""
        return {
            op
            for contract in self.contracts.values()
            for op, statuses in contract.seen.items()
            if any(200 <= s < 300 for s in statuses)
        }
