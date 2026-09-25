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

from collections.abc import Mapping, Sequence
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
    "catalog_entry",
    "change_impact",
    "change_item",
    "change_set",
    "error",
    "expectation_result",
    "expectation_spec",
    "gate_decision",
    "gate_decision_summary",
    "gate_policy",
    "gate_summary",
    "impact_scenario",
    "imported_catalog",
    "manifest",
    "outcome",
    "project",
    "promoted_regression",
    "queued_case",
    "registered_version",
    "regression",
    "regression_detail",
    "regression_draft",
    "release",
    "release_gate",
    "retrieval_step",
    "run",
    "run_detail",
    "scenario",
    "tool_step",
    "trace",
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


# ------------------------------------------------------------------ releases


def gate_policy(**over: Any) -> dict[str, Any]:
    """A project's gate policy with every default resolved (``ResolvedGatePolicy``)."""
    return {
        "always_run_tags": ["critical", "security"],
        "max_production_samples": 100,
        "include_known_regressions": True,
        "max_depth": 4,
        "max_gate_cost_usd": 20,
        "latency_regression_pct": 25,
        "latency_regression_min_ms": 250,
        "cost_regression_pct": 15,
        "semantic_regression_drop": 0.1,
        "judge_min_agreement": 0.8,
        "allow_reviewer_override": True,
        "warn_fails_ci": False,
    } | over


_EXIT_CODES = {"PASS": 0, "WARN": 2, "BLOCK": 3}


def gate_decision(outcome: str = "BLOCK", **over: Any) -> dict[str, Any]:
    """A gate decision (``GateDecision``); a BLOCK has one rule that fired."""
    rules = over.pop("rules", None)
    if rules is None:
        rules = (
            []
            if outcome == "PASS"
            else [
                {
                    "rule": "critical_failure" if outcome == "BLOCK" else "insufficient_coverage",
                    "outcome": outcome,
                    "title": "A critical scenario fails" if outcome == "BLOCK" else "Coverage is short",
                    "statement": "A critical scenario must not fail on the candidate.",
                    "evidence": [{"scenario": "refund-timeout-after-mutation", "summary": "It fails."}],
                }
            ]
        )
    return {
        "outcome": outcome,
        "incomplete": False,
        "rules_version": "gate-rules/v1",
        "summary": f"Gate {outcome}.",
        "rules": rules,
        "counts": {
            "required": 1,
            "evaluated": 1,
            "passed": 0 if outcome == "BLOCK" else 1,
            "failed": 1 if outcome == "BLOCK" else 0,
            "incomplete": 0,
            "new_critical_failures": 1 if outcome == "BLOCK" else 0,
            "regressed": 0,
            "improved": 0,
            "known_regressions": 0,
        },
        "coverage": [{"name": "Required scenarios passed", "covered": 1, "total": 1, "missing": []}],
        "risk_index": {"value": 30 if outcome == "BLOCK" else 0, "formula": "sum of factors", "factors": []},
        "exit_code": _EXIT_CODES[outcome],
        "ci_fails": outcome == "BLOCK",
    } | over


def gate_decision_summary(decision: Mapping[str, Any], **over: Any) -> dict[str, Any]:
    """What a decision says at a glance (``GateDecisionSummary``)."""
    counts = decision["counts"]
    return {
        "scenarios": counts["required"],
        "evaluated": counts["evaluated"],
        "new_critical_failures": counts["new_critical_failures"],
        "regressed": counts["regressed"],
        "failed": counts["failed"],
        "rules": [r["rule"] for r in decision["rules"]],
        "exit_code": decision["exit_code"],
        "ci_fails": decision["ci_fails"],
        "eval_run_status": "COMPLETED",
        "cost": None,
        "cost_delta_usd": None,
        "latency_p95_delta_ms": None,
    } | over


def release_gate(outcome: str | None = None, **over: Any) -> dict[str, Any]:
    """``getReleaseGate``: evaluating (``outcome`` None) or decided."""
    decision = gate_decision(outcome) if outcome else None
    return {
        "release_id": uuid(0xD101),
        "release_evaluation_id": uuid(0xD201),
        "revision": 1,
        "status": "DECIDED" if decision else "EVALUATING",
        "requested_by": ACTOR,
        "requested_at": NOW,
        "decided_at": NOW if decision else None,
        "eval_run_id": uuid(0xA003),
        "policy": gate_policy(),
        "suite": [],
        "impact": change_impact(),
        "decision": decision,
        "summary": gate_decision_summary(decision) if decision else None,
        "evidence_sha256": sha256("e") if decision else None,
        "evidence_verified": True if decision else None,
        "override": None,
        "effective_outcome": outcome or "PENDING",
        "exit_code": decision["exit_code"] if decision else None,
        "ci_fails": decision["ci_fails"] if decision else None,
    } | over


def gate_summary(gate: Mapping[str, Any]) -> dict[str, Any]:
    """A gate as release lists show it (``GateSummary``)."""
    decision = gate["decision"]
    return {
        "revision": gate["revision"],
        "status": gate["status"],
        "outcome": decision["outcome"] if decision else None,
        "effective_outcome": gate["effective_outcome"],
        "incomplete": decision["incomplete"] if decision else None,
        "risk_index": decision["risk_index"]["value"] if decision else None,
        "summary": gate["summary"],
        "requested_by": gate["requested_by"],
        "requested_at": gate["requested_at"],
        "decided_at": gate["decided_at"],
        "eval_run_id": gate["eval_run_id"],
        "overridden": gate["override"] is not None,
    }


def release(gate: Mapping[str, Any] | None = None, **over: Any) -> dict[str, Any]:
    """A release (``Release``), with the summary of its latest ``gate``."""
    base, candidate = over.pop("baseline_version", "1.2.4"), over.pop("candidate_version", "1.3.0")
    return {
        "id": uuid(0xD101),
        "project_id": PROJECT,
        "agent": {"id": uuid(0xB101), "name": "support-refund-agent"},
        "change_set_id": uuid(0xC101),
        "baseline": {"id": uuid(0xB201), "version": base},
        "candidate": {"id": uuid(0xB202), "version": candidate},
        "title": "",
        "commit_sha": None,
        "ci_url": None,
        "created_by": ACTOR,
        "created_at": NOW,
        "changes": {
            "items": 1,
            "breaking": 0,
            "seeds": 1,
            "kinds": {"prompt": 1},
            "confidence": {"exact": 1},
        },
        "gate": gate_summary(gate) if gate else None,
    } | over


# ------------------------------------------------------------ trace service


def trace(*, finalized: bool = True, **over: Any) -> dict[str, Any]:
    """A trace (``listTraces``). A finalized trace carries its complete
    summary; one that is not has ``{}`` and its usage is not settled."""
    tokens_in, tokens_out = over.get("input_tokens", 1200), over.get("output_tokens", 300)
    cost = over.get("cost_usd", 0.0042)
    summary: dict[str, Any] = (
        {
            "agent": "support-refund-agent",
            "agent_version": "1.2.4",
            "outcome": "SUCCESS",
            "outcome_verified": True,
            "tools": ["lookup_order"],
            "errors": [],
            "violations": [],
            "policy_decisions": [],
            "step_count": 4,
            "retry_count": 0,
            "cost_usd": cost if cost is not None else 0,
            "cost_known": cost is not None,
            "duration_ms": 850.0,
            "model": "scripted-planner-v1",
            "prompt_hash": None,
            "tool_sequence_sketch": "lookup_order",
        }
        if finalized
        else {}
    )
    return (
        {
            "project_id": PROJECT,
            "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
            "organization_id": ORGANIZATION,
            "environment": "simulation",
            "agent_name": "support-refund-agent",
            "agent_version": "1.2.4",
            "session_id": None,
            "release_id": None,
            "commit_sha": None,
            "source": "simulation",
            "simulation_run_id": None,
            "scenario_id": None,
            "root_span_id": None,
            "root_name": "agent.run",
            "status": "OK",
            "started_at": NOW,
            "ended_at": NOW,
            "duration_ms": 850.0,
            "span_count": 6,
            "model_call_count": 2,
            "tool_call_count": 1,
            "error_count": 0,
            "retry_count": 0,
            "input_tokens": tokens_in,
            "output_tokens": tokens_out,
            "cost_usd": cost,
            "models": ["scripted-planner-v1"],
            "tools": ["lookup_order"],
            "policy_decisions": [],
            "signals": [],
            "prompt_hash": None,
            "semconv_version": "1.0.0",
            "sdk_name": "agenttwin-python",
            "sdk_version": "0.1.0",
            "content_mode": "redacted",
            "content_dropped": False,
            "truncated": False,
            "outcome_status": None,
            "outcome_verified": None,
            "human_reviewed": False,
            "flagged": False,
            "summary": summary,
            "finalized": finalized,
            "content_purged": False,
            "expires_at": NOW,
        }
        | {k: v for k, v in over.items() if k not in ("input_tokens", "output_tokens", "cost_usd")}
        | {
            "input_tokens": tokens_in,
            "output_tokens": tokens_out,
            "cost_usd": cost,
        }
    )


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


def catalog_entry(name: str, **over: Any) -> dict[str, Any]:
    """An imported tool (``CatalogEntry``)."""
    return {
        "name": name,
        "operation": name,
        "description": "",
        "risk": "READ",
        "risk_source": "inferred",
        "mutating": False,
        "input_schema": {"type": "object"},
        "registry": "created",
    } | over


def imported_catalog(
    *, created: bool = True, entries: Sequence[str] = ("get_refund",), **over: Any
) -> dict[str, Any]:
    """``importOpenApi`` / ``importMcp``: a catalog revision (201 when
    ``created``, 200 for an import equal to the latest revision)."""
    items = [catalog_entry(n) for n in entries]
    return {
        "id": uuid(0xC001),
        "project_id": PROJECT,
        "source": "OPENAPI",
        "name": "payments-api",
        "revision": 1,
        "service": None,
        "title": "Demo Co Payments API",
        "api_version": "2.3.0",
        "spec_version": "3.1.0",
        "summary": {
            "tools": max(len(items), 1),
            "created": len(items) if created else 0,
            "updated": 0,
            "unchanged": 0 if created else len(items),
            "kept": 0,
            "skipped": 0,
            "warnings": 0,
            "mutating": 0,
            "risks": {"READ": len(items)},
        },
        "content_sha256": sha256("d"),
        "document_sha256": sha256("e"),
        "created_by": ACTOR,
        "created_at": NOW,
        "servers": [],
        "options": {},
        "entries": items,
        "skipped": [],
        "warnings": [],
        "created": created,
    } | over


def change_item(kind: str, subject: str, **over: Any) -> dict[str, Any]:
    """A change of a change set (``ChangeItem``)."""
    return {
        "kind": kind,
        "subject": subject,
        "change": "modified",
        "summary": f"{kind} {subject} changed",
        "confidence": "exact",
        "breaking": False,
        "detail": {},
    } | over


def change_set(*, created: bool = True, **over: Any) -> dict[str, Any]:
    """``createChangeSet`` (``created``: 201, else 200) and ``getChangeSet``
    (drop ``created``)."""
    base, candidate = over.pop("base_version", "1.2.4"), over.pop("candidate_version", "1.3.0")
    items = over.pop("items", [change_item("prompt", sha256("c")[:12])])
    return {
        "id": uuid(0xC101),
        "project_id": PROJECT,
        "agent_id": uuid(0xB101),
        "agent_name": "support-refund-agent",
        "base": {"id": uuid(0xB201), "version": base},
        "candidate": {"id": uuid(0xB202), "version": candidate},
        "title": "",
        "summary": {
            "items": len(items),
            "breaking": sum(1 for i in items if i["breaking"]),
            "seeds": len(items),
            "kinds": {i["kind"]: 1 for i in items},
            "confidence": {"exact": len(items)},
        },
        "content_sha256": sha256("f"),
        "created_by": ACTOR,
        "created_at": NOW,
        "git": None,
        "declared": [],
        "items": items,
        "seeds": [],
        "scope": {"agent": "support-refund-agent", "version": candidate},
        "created": created,
    } | over


def impact_scenario(name: str, *, why: Sequence[str] = (), **over: Any) -> dict[str, Any]:
    """A selected scenario of a change impact (``ImpactScenario``)."""
    return {
        "id": uuid(0xC201),
        "name": name,
        "agent": "support-refund-agent",
        "twin": "demo-co-support",
        "severity": "critical",
        "tags": [],
        "source": "library",
        "latest_version": 1,
        "description": "",
        "in_library": True,
        "reasons": {"graph": [], "similar": [], "always_run_tags": [], "known_regression": False},
        "why": list(why) or [f"tests refund_payment, which changed ({name})"],
    } | over


def change_impact(scenarios: Sequence[Mapping[str, Any]] = (), **over: Any) -> dict[str, Any]:
    """``getChangeSetImpact``: complete unless ``problems`` say otherwise."""
    selected = [dict(s) for s in scenarios]
    problems = over.get("problems", [])
    return {
        "change_set_id": uuid(0xC101),
        "project_id": PROJECT,
        "agent": "support-refund-agent",
        "base_version": "1.2.4",
        "candidate_version": "1.3.0",
        "policy": {
            "always_run_tags": ["critical", "security"],
            "include_known_regressions": True,
            "max_depth": 4,
        },
        "complete": not problems,
        "problems": problems,
        "computed_at": NOW,
        "scenarios": selected,
        "counts": {
            "scenarios": len(selected),
            "graph": sum(1 for s in selected if s["reasons"]["graph"]),
            "similar": sum(1 for s in selected if s["reasons"]["similar"]),
            "always_run": sum(1 for s in selected if s["reasons"]["always_run_tags"]),
            "known_regression": sum(1 for s in selected if s["reasons"]["known_regression"]),
        },
        "unlinked_scenarios": [],
        "graph": None,
        "irreversible_actions": [],
        "new_privileges": [],
        "embedding_model": "hashing-v1",
        "min_similarity": 0.2,
        "truncated": False,
        "notes": [],
    } | over


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


def simulation_pair(
    eval_run_id: str, names: Sequence[str], *, created: bool = True, **over: Any
) -> dict[str, Any]:
    """``startSimulationPair``: two queued runs over one suite (``names`` in
    run order), baseline 1.2.4 and candidate 1.3.0 unless overridden."""
    base_id, cand_id = uuid(0xE101), uuid(0xE102)
    pinned = [
        {
            "scenario": name,
            "scenario_version_id": uuid(0xD100 + i),
            "spec_hash": sha256("abcdef0123456789"[i % 16]),
            "twin": "demo-co-support",
            "twin_definition_id": uuid(0xC101),
            "twin_version": 1,
            "twin_spec_hash": sha256("c"),
            "seed": 42 + i,
        }
        for i, name in enumerate(names)
    ]

    def side(run_id: str, side_: str, version: str, other: str) -> dict[str, Any]:
        out = run(
            id=run_id, agent_version=version, side=side_, eval_run_id=eval_run_id, case_count=len(names)
        )
        out["pinning"] = out["pinning"] | {
            "scenarios": pinned,
            "pair": {"eval_run_id": eval_run_id, "side": side_, "counterpart_run_id": other},
        }
        return out

    return {
        "eval_run_id": eval_run_id,
        "created": created,
        "seed": 42,
        "baseline": side(base_id, "BASELINE", over.pop("baseline_version", "1.2.4"), cand_id),
        "candidate": side(cand_id, "CANDIDATE", over.pop("candidate_version", "1.3.0"), base_id),
        "cases": [
            {
                "position": i,
                "scenario_id": uuid(0xD000 + i),
                "scenario_name": name,
                "severity": "critical",
                "seed": 42 + i,
                "tenant": None,
                "baseline_case_id": uuid(0xF100 + i),
                "candidate_case_id": uuid(0xF200 + i),
            }
            for i, name in enumerate(names)
        ],
    } | over


def run_detail(run_: Mapping[str, Any], cases: list[dict[str, Any]]) -> dict[str, Any]:
    """``getSimulation``: the run, its cases and no transitions."""
    return {"run": dict(run_), "cases": cases, "transitions": []}


def dataset(**over: Any) -> dict[str, Any]:
    """A dataset (evaluation API ``Dataset``)."""
    return {
        "id": uuid(0xB001),
        "organization_id": ORGANIZATION,
        "project_id": PROJECT,
        "name": "refund-regression-suite",
        "description": None,
        "owner": None,
        "tags": [],
        "latest_version": 1,
        "archived": False,
        "created_by": ACTOR,
        "created_at": NOW,
        "updated_at": NOW,
    } | over


def dataset_detail(dataset_: Mapping[str, Any], names: Sequence[str]) -> dict[str, Any]:
    """``createDataset``, ``getDataset``, ``addDatasetCases``: the dataset and
    its latest version with ``names`` as synthetic cases."""
    version = int(dataset_["latest_version"])
    summary: dict[str, Any] = {
        "version": version,
        "case_count": len(names),
        "note": None,
        "created_by": ACTOR,
        "created_at": NOW,
    }
    cases: list[dict[str, Any]] = [
        {
            "scenario": name,
            "tags": [],
            "source": "manual",
            "trace_id": None,
            "privacy": "synthetic",
            "note": None,
            "added_by": ACTOR,
            "added_at": NOW,
            "last_result": None,
        }
        for name in names
    ]
    return {"dataset": dict(dataset_), "version": summary | {"cases": cases}, "versions": [summary]}


def eval_run(**over: Any) -> dict[str, Any]:
    """An evaluation run (evaluation API ``EvalRun``), queued unless
    overridden."""
    status = over.get("status", "QUEUED")
    return {
        "id": uuid(0xB101),
        "organization_id": ORGANIZATION,
        "project_id": PROJECT,
        "agent_name": "support-refund-agent",
        "baseline_version": "1.2.4",
        "candidate_version": "1.3.0",
        "seed": None,
        "release_id": None,
        "release_evaluation_id": None,
        "status": status,
        "requested_by": ACTOR,
        "cancel_requested": False,
        "attempts": 0 if status == "QUEUED" else 1,
        "baseline_run_id": None,
        "candidate_run_id": None,
        "case_count": 0,
        "error": None,
        "created_at": NOW,
        "started_at": None if status == "QUEUED" else NOW,
        "finished_at": NOW if status in _FINAL else None,
        "updated_at": NOW,
        "selection": {"scenarios": None, "tags": None, "dataset": None, "scenario_versions": None},
        "counts": {"NEW_CRITICAL_FAILURE": 0, "REGRESSED": 0, "IMPROVED": 0, "UNCHANGED": 0, "INCOMPLETE": 0},
        "pinning": None,
        "judge": None,
        "budget": None,
    } | over


def eval_run_summary(
    *,
    new_critical_failures: Mapping[str, Sequence[str]] | None = None,
    regressed: Sequence[str] = (),
    improved: Sequence[str] = (),
    incomplete: Sequence[str] = (),
    unchanged: int = 0,
) -> dict[str, Any]:
    """An evaluation run's summary (``EvalRunSummary``): the named cases per
    class (``new_critical_failures`` maps a scenario to its expectations) and
    ``unchanged`` more; side totals count the cases only."""
    critical = dict(new_critical_failures or {})
    counts = {
        "NEW_CRITICAL_FAILURE": len(critical),
        "REGRESSED": len(regressed),
        "IMPROVED": len(improved),
        "UNCHANGED": unchanged,
        "INCOMPLETE": len(incomplete),
    }
    side = {
        "cases": sum(counts.values()),
        "passed": 0,
        "failed": 0,
        "incomplete": 0,
        "critical_failures": 0,
        "policy_violations": 0,
        "retries": 0,
        "duplicate_side_effects": 0,
        "escalations": 0,
        "tool_calls": 0,
        "latency_ms_p50": None,
        "latency_ms_p95": None,
        "tokens": None,
        "tokens_known": 0,
        "cost_usd": None,
        "cost_known": 0,
        "semantic_score": None,
    }
    return {
        "counts": counts,
        "baseline": side,
        "candidate": dict(side),
        "new_critical_failures": [
            {"scenario_name": name, "expectations": list(expectations)}
            for name, expectations in critical.items()
        ],
        "regressed": list(regressed),
        "improved": list(improved),
        "incomplete": list(incomplete),
        "slices": {"failure_class": {}},
    }


def eval_run_detail(run_: Mapping[str, Any], summary: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """``getEvalRun``: the run, its summary (``None`` until it is evaluated),
    no cases and no transitions."""
    return {"run": dict(run_), "summary": dict(summary) if summary else None, "cases": [], "transitions": []}


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


def regression(**over: Any) -> dict[str, Any]:
    """A mined regression group (evaluation API ``Regression``)."""
    return {
        "id": uuid(0xE001),
        "organization_id": ORGANIZATION,
        "project_id": PROJECT,
        "agent": "support-refund-agent",
        "title": "refund_payment took effect twice",
        "status": "CANDIDATE",
        "taxonomy": "DUPLICATE_SIDE_EFFECT",
        "suggested_taxonomy": "DUPLICATE_SIDE_EFFECT",
        "secondary": ["RETRY_SAFETY", "TIMEOUT"],
        "severity": "critical",
        "suggested_severity": "critical",
        "severity_reason": "DUPLICATE_SIDE_EFFECT is critical",
        "triaged_by": None,
        "component": "refund_payment",
        "assignee": None,
        "tags": [],
        "evidence": ["DUPLICATE_SIDE_EFFECT: refund_payment took effect more than once"],
        "fingerprint": "a" * 32,
        "representative_trace_id": "f" * 32,
        "occurrence_count": 1,
        "first_seen": NOW,
        "last_seen": NOW,
        "versions": ["1.3.0"],
        "environments": ["production"],
        "merged_into": None,
        "scenario_id": None,
        "scenario_name": None,
        "dataset_id": None,
        "dataset_version": None,
        "promoted_by": None,
        "promoted_at": None,
        "fixed_version": None,
        "fixed_eval_run_id": None,
        "created_at": NOW,
        "updated_at": NOW,
    } | over


def regression_detail(regression_: Mapping[str, Any]) -> dict[str, Any]:
    """``getRegression``: the group, its one failure and its creation."""
    occurrence = {
        "trace_id": regression_["representative_trace_id"],
        "agent_version": "1.3.0",
        "environment": "production",
        "started_at": NOW,
        "title": regression_["title"],
        "component": regression_["component"],
        "taxonomy": regression_["suggested_taxonomy"],
        "secondary": list(regression_["secondary"]),
        "severity": regression_["suggested_severity"],
        "severity_reason": "DUPLICATE_SIDE_EFFECT is critical",
        "evidence": list(regression_["evidence"]),
        "reasons": ["an irreversible write took effect more than once"],
        "join_kind": "new",
        "join_reason": "the first failure of its kind",
        "similarity": None,
        "created_at": NOW,
    }
    created = {
        "seq": 1,
        "action": "created",
        "from_status": None,
        "to_status": "CANDIDATE",
        "actor": "system:regression-miner",
        "reason": "the first failure of its kind",
        "detail": {},
        "at": NOW,
    }
    return {"regression": dict(regression_), "occurrences": [occurrence], "events": [created]}


def regression_draft(regression_: Mapping[str, Any], *, problems: Sequence[str] = ()) -> dict[str, Any]:
    """``draftRegressionScenario``: a scenario document and its YAML."""
    name = "regression-refund-payment-took-effect-twice"
    document = {
        "apiVersion": "agenttwin.dev/v1",
        "kind": "Scenario",
        "metadata": {"name": name, "source": "production_regression"},
        "spec": {"agent": regression_["agent"], "twin": "demo-co-support"},
    }
    return {
        "regression_id": regression_["id"],
        "trace_id": regression_["representative_trace_id"],
        "draft": {
            "document": document,
            "yaml": f"apiVersion: agenttwin.dev/v1\nkind: Scenario\nmetadata:\n  name: {name}\n",
            "notes": [],
            "problems": list(problems),
            "mappings": [],
            "complete": not problems,
        },
    }


def promoted_regression(regression_: Mapping[str, Any], **over: Any) -> dict[str, Any]:
    """``promoteRegression``: the promoted group, its scenario and dataset."""
    name = "regression-refund-payment-took-effect-twice"
    promoted = dict(regression_) | {
        "status": "PROMOTED",
        "scenario_id": uuid(0xE101),
        "scenario_name": name,
        "dataset_id": uuid(0xE201),
        "dataset_version": 1,
        "promoted_by": ACTOR,
        "promoted_at": NOW,
    }
    return {
        "regression": promoted,
        "scenario": {"id": uuid(0xE101), "name": name, "version": 1, "created": True, "warnings": []},
        "dataset": {"id": uuid(0xE201), "name": "production-regressions", "version": 1},
        "redacted": 0,
    } | over


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
    ("/internal/v1/simulation-pairs", "simulation-service"),
    ("/api/v1/datasets", "evaluation-service"),
    ("/api/v1/eval-runs", "evaluation-service"),
    ("/api/v1/reviews", "evaluation-service"),
    ("/api/v1/judges", "evaluation-service"),
    ("/api/v1/regressions", "evaluation-service"),
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
