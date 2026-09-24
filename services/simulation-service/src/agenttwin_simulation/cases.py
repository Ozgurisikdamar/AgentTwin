"""What one simulation case needs besides the twin: the scenario's runtime
settings, the request sent to the agent, the evaluation context built from
the twin's records and the verified outcome reported to the trace service.

Everything here is a pure function of its inputs, so a case can be rebuilt
and re-evaluated from what the database stored.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from agenttwin_core.evaluators import EvaluationContext, EvaluationResult, ToolCall
from agenttwin_simulation.twin.definition import TwinDefinition
from agenttwin_simulation.twin.engine import CallRecord, merge_patch
from agenttwin_simulation.twin.faults import FaultRule, parse_faults

__all__ = [
    "DEFAULT_NOW",
    "ScenarioRuntime",
    "agent_request",
    "case_seed",
    "case_tenant",
    "evaluation_context",
    "foreign_values",
    "initial_state",
    "outcome_body",
    "scenario_runtime",
    "synthetic_result",
    "tool_calls",
    "valid_trace_id",
]

# The simulated clock: twins see a fixed "now" (``{now}`` in templates) so a
# scenario replays identically; pin another instant with input.context.now.
DEFAULT_NOW = "2026-01-01T00:00:00Z"
OUTCOMES = frozenset({"SUCCESS", "PARTIAL", "FAILURE", "UNKNOWN"})
_TRACE_ID = re.compile(r"^[a-f0-9]{32}$")
_MAX_FOREIGN = 500


@dataclass(frozen=True)
class ScenarioRuntime:
    """The parts of a scenario version the twin endpoint consults on every call."""

    spec: Mapping[str, Any]
    rules: tuple[FaultRule, ...]
    allowed_tools: frozenset[str] | None
    documents: tuple[Mapping[str, Any], ...]
    now: str


def _context(doc: Mapping[str, Any]) -> Mapping[str, Any]:
    ctx = doc["spec"]["input"].get("context")
    return ctx if isinstance(ctx, Mapping) else {}


def scenario_runtime(doc: Mapping[str, Any]) -> ScenarioRuntime:
    spec = doc["spec"]
    allowed = spec.get("allowedTools")
    now = _context(doc).get("now")
    return ScenarioRuntime(
        spec=spec,
        rules=tuple(parse_faults(spec.get("faults"))),
        allowed_tools=frozenset(str(t) for t in allowed) if allowed is not None else None,
        documents=tuple(spec["input"].get("documents") or ()),
        now=now if isinstance(now, str) and now else DEFAULT_NOW,
    )


def case_tenant(doc: Mapping[str, Any]) -> str | None:
    """The tenant the case runs as (``input.context.tenant``). It comes from
    the scenario, never from what the agent sends."""
    tenant = _context(doc).get("tenant")
    return tenant if isinstance(tenant, str) and tenant else None


def case_seed(run_seed: int, scenario_name: str, pinned: int | None = None) -> int:
    """A scenario's own ``spec.seed`` wins; otherwise the run seed and the
    scenario name derive a stable per-case seed."""
    if pinned is not None:
        return pinned
    digest = hashlib.sha256(f"{run_seed}:{scenario_name}".encode()).digest()
    return int.from_bytes(digest[:4], "big")


def initial_state(definition: TwinDefinition, doc: Mapping[str, Any]) -> dict[str, Any]:
    """The twin's initial state with the scenario's ``spec.state`` merged in
    (JSON Merge Patch: ``null`` removes a key)."""
    state = merge_patch(dict(definition.initial_state), doc["spec"].get("state") or {})
    return state if isinstance(state, dict) else {}


def _strings(value: Any, out: set[str], depth: int = 0) -> None:
    if depth > 32:
        return
    if isinstance(value, str):
        if len(value) >= 4:
            out.add(value)
    elif isinstance(value, Mapping):
        for v in value.values():
            _strings(v, out, depth + 1)
    elif isinstance(value, list):
        for v in value:
            _strings(v, out, depth + 1)


def foreign_values(state: Mapping[str, Any], tenant_key: str | None, tenant: str | None) -> tuple[str, ...]:
    """String values that occur only in records of *other* tenants: seeing
    one in the agent's answer means another tenant's data leaked."""
    if not tenant_key or not tenant:
        return ()
    foreign: set[str] = set()
    own: set[str] = set()

    def walk(value: Any, depth: int = 0) -> None:
        if depth > 32:
            return
        if isinstance(value, Mapping):
            owner = value.get(tenant_key)
            if isinstance(owner, str) and owner != tenant:
                _strings(value, foreign)
                return
            for k, v in value.items():
                if k != tenant_key:
                    if isinstance(v, Mapping | list):
                        walk(v, depth + 1)
                    else:
                        _strings(v, own)
        elif isinstance(value, list):
            for v in value:
                walk(v, depth + 1)
        else:
            _strings(value, own)

    walk(state)
    foreign.discard(tenant)
    return tuple(sorted(foreign - own)[:_MAX_FOREIGN])


def agent_request(
    *,
    doc: Mapping[str, Any],
    run: Mapping[str, Any],
    case: Mapping[str, Any],
    twin_url: str,
    token: str,
) -> dict[str, Any]:
    """The ``POST /run`` body of the agent adapter contract (ADR-0011)."""
    spec = doc["spec"]
    ctx = dict(_context(doc))
    body: dict[str, Any] = {
        "input": spec["input"]["message"],
        "agent_version": run["agent_version"],
        "session_id": f"sim-{case['id']}",
        "tools_base_url": twin_url,
        # The capability token of this case: the twin identifies the case by
        # it, so an agent can only ever touch its own isolated state.
        "tool_headers": {"Authorization": f"Bearer {token}"},
        "run_context": {
            "source": "simulation",
            "simulation_run_id": str(run["id"]),
            "scenario_id": str(case["scenario_id"]),
            "scenario_name": str(case["scenario_name"]),
            "case_id": str(case["id"]),
            "release_id": run.get("release_id"),
        },
    }
    tenant = case.get("tenant")
    if tenant:
        body["tenant"] = tenant
    customer = ctx.pop("customer_id", None)
    if isinstance(customer, str):
        body["customer_id"] = customer
    extra = {k: v for k, v in ctx.items() if k not in ("tenant", "now")}
    if extra:
        body["context"] = extra
    return body


def valid_trace_id(value: Any) -> str | None:
    return value if isinstance(value, str) and _TRACE_ID.match(value) else None


def tool_calls(steps: Sequence[Mapping[str, Any]]) -> tuple[ToolCall, ...]:
    """The tool calls the twin recorded (retrievals are not tool calls)."""
    out = []
    for s in steps:
        if s["kind"] == "tool_call":
            latency = s.get("latency_ms")
            out.append(CallRecord.from_json(s["record"]).to_tool_call(float(latency or 0.0)))
    return tuple(out)


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def evaluation_context(
    *,
    doc: Mapping[str, Any],
    definition: TwinDefinition,
    agent_result: Mapping[str, Any] | None,
    steps: Sequence[Mapping[str, Any]],
    state_before: Mapping[str, Any],
    state_after: Mapping[str, Any],
    tenant: str | None,
    latency_ms: float,
) -> EvaluationContext:
    result = agent_result or {}
    claimed = _opt_str(result.get("claimed_outcome"))
    raw_steps = result.get("steps")
    return EvaluationContext(
        input=str(doc["spec"]["input"]["message"]),
        output=_opt_str(result.get("output")),
        claimed_outcome=claimed.upper() if claimed else None,
        business_outcome=_opt_str(result.get("business_outcome")),
        agent_status=_opt_str(result.get("status")),
        steps=raw_steps if isinstance(raw_steps, int) and not isinstance(raw_steps, bool) else None,
        tool_calls=tool_calls(steps),
        state_before=state_before,
        state_after=state_after,
        latency_ms=latency_ms,
        cost_usd=None,  # agents do not report priced usage through the adapter contract yet
        secrets=tuple(definition.secrets),
        tenant=tenant,
        foreign_values=foreign_values(state_before, definition.tenant_key, tenant),
    )


def synthetic_result(status: str, label: str, reason: str) -> EvaluationResult:
    """A result about the run itself (the agent could not be run, crashed or
    ran away); it is critical so it decides the verdict."""
    return EvaluationResult(
        status=status,  # type: ignore[arg-type]
        reason=reason,
        evaluator="simulation.agent_run",
        evaluator_version="1.0.0",
        label=label,
        expectation={"id": "agent-run", "type": "agentRun", "critical": True},
    )


def outcome_body(case: Mapping[str, Any]) -> dict[str, Any]:
    """The verified outcome of a finished case for the trace service: the
    verdict against the tool twin's final state, next to what the agent
    claimed (a claimed success the twin disproves becomes a contradiction)."""
    status = case["status"]
    result = case.get("agent_result") or {}
    verdict = case.get("verdict") or {}
    if status == "PASSED":
        body: dict[str, Any] = {
            "status": "SUCCESS",
            "verified": True,
            "verification_source": "tool_twin_state",
        }
    elif status == "FAILED":
        body = {"status": "FAILURE", "verified": True, "verification_source": "tool_twin_state"}
    else:
        body = {"status": "UNKNOWN", "verified": False, "verification_source": "unavailable"}
    claimed = _opt_str(result.get("claimed_outcome"))
    if claimed and claimed.upper() in OUTCOMES:
        body["claimed_status"] = claimed.upper()
    business = _opt_str(result.get("business_outcome"))
    if business:
        body["business_outcome"] = business[:200]
    expected: dict[str, Any] = {}
    actual: dict[str, Any] = {}
    for r in case.get("results") or []:
        for ev in r.get("evidence") or []:
            if ev.get("kind") == "state" and ev.get("ref") and "expected" in ev:
                expected[str(ev["ref"])] = ev["expected"]
                actual[str(ev["ref"])] = ev.get("actual")
    if expected:
        body["expected_state"] = expected
        body["actual_state"] = actual
    reason = str(verdict.get("reason") or case.get("reason") or "")
    body["notes"] = f"Simulation case {case.get('scenario_name')}: {reason}"[:4000]
    return body
