"""Baseline versus candidate, case by case (spec §27).

Both sides ran the same scenario version from the same initial state with
the same seed. A case is classified from its expectations, never from an
average:

* ``NEW_CRITICAL_FAILURE`` — a critical expectation fails for the candidate
  and did not fail for the baseline;
* ``REGRESSED`` — another expectation newly fails;
* ``IMPROVED`` — expectations the baseline failed now pass, none newly fails;
* ``UNCHANGED`` — every expectation ends the same (including "still failing");
* ``INCOMPLETE`` — a side did not finish (errored, cancelled, not run): the
  case cannot be compared, which is never read as a pass.

The asymmetry is deliberate: a candidate failure counts as new whenever the
baseline did not fail that expectation (even if the baseline could not
evaluate it), but an improvement needs a baseline failure that now passes.
A regression must always be visible; an improvement must be shown.

Metrics (policy violations, retries, duplicate side effects, latency, tokens,
cost, semantic scores…) are compared alongside and reported as better, worse
or the same; they never change the classification.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from agenttwin_core.evaluators import EvaluationResult, ToolCall, case_verdict
from agenttwin_core.evaluators.signals import (
    agent_calls,
    duplicate_side_effects,
    escalations,
    policy_violations,
    retries,
)
from agenttwin_evaluation.trajectory import Step, align, first_divergence, state_changes, trajectory

__all__ = [
    "CLASSIFICATIONS",
    "METRICS",
    "CaseComparison",
    "Side",
    "compare_case",
    "summarize",
]

Classification = Literal["NEW_CRITICAL_FAILURE", "REGRESSED", "IMPROVED", "UNCHANGED", "INCOMPLETE"]
CLASSIFICATIONS: tuple[Classification, ...] = (
    "NEW_CRITICAL_FAILURE",
    "REGRESSED",
    "IMPROVED",
    "UNCHANGED",
    "INCOMPLETE",
)
DONE = frozenset({"PASSED", "FAILED"})
DEFAULT_ESCALATION_TOOLS = ("escalate_to_human",)

# Per metric: which direction is better, and how large a change must be to
# count (absolute, relative to the baseline) — latency jitter of a few
# milliseconds is not a regression.
Direction = Literal["lower", "higher", "neutral"]
METRICS: dict[str, tuple[Direction, float, float]] = {
    "critical_failures": ("lower", 0.0, 0.0),
    "failed_expectations": ("lower", 0.0, 0.0),
    "policy_violations": ("lower", 0.0, 0.0),
    "retries": ("lower", 0.0, 0.0),
    "duplicate_side_effects": ("lower", 0.0, 0.0),
    "argument_failures": ("lower", 0.0, 0.0),
    "tool_calls": ("neutral", 0.0, 0.0),
    "escalations": ("neutral", 0.0, 0.0),
    "steps": ("lower", 0.0, 0.0),
    "latency_ms": ("lower", 100.0, 0.2),
    "tokens": ("lower", 0.0, 0.1),
    "cost_usd": ("lower", 0.0, 0.1),
    "semantic_score": ("higher", 0.05, 0.0),
}
_RISK_ORDER = ("READ", "WRITE_REVERSIBLE", "EXECUTE", "WRITE_IRREVERSIBLE", "ADMIN")


@dataclass(frozen=True)
class Side:
    """One side of a compared case: what the simulation recorded, with the
    judged semantic expectations merged in and the trace's usage when known."""

    case_id: str | None
    status: str  # PASSED | FAILED | ERRORED | CANCELLED | PENDING | RUNNING | MISSING
    reason: str | None
    results: tuple[EvaluationResult, ...]
    calls: tuple[ToolCall, ...]
    steps: tuple[Step, ...]
    final_state: Mapping[str, Any] | None
    latency_ms: float | None
    agent_steps: int | None
    trace_id: str | None = None
    tokens: int | None = None
    cost_usd: float | None = None
    escalation_tools: tuple[str, ...] = DEFAULT_ESCALATION_TOOLS

    @classmethod
    def missing(cls, reason: str) -> Side:
        return cls(None, "MISSING", reason, (), (), (), None, None, None)

    @classmethod
    def from_case_detail(
        cls,
        detail: Mapping[str, Any],
        *,
        judged: Sequence[EvaluationResult] = (),
        tokens: int | None = None,
        cost_usd: float | None = None,
        escalation_tools: tuple[str, ...] = DEFAULT_ESCALATION_TOOLS,
    ) -> Side:
        """From the simulation contract's ``CaseDetail``. ``judged`` replaces
        the SKIPPED semantic results of the same expectation id, and the case
        status is recomputed from the merged results for a finished case."""
        case = detail["case"]
        results = [EvaluationResult.from_json(r) for r in case.get("results") or ()]
        by_id = {str(r.expectation.get("id")): r for r in judged}
        merged = [by_id.pop(str(r.expectation.get("id")), r) for r in results]
        merged.extend(by_id.values())
        status = str(case.get("status") or "MISSING")
        reason = case.get("reason")
        if judged and status in DONE and merged:
            verdict = case_verdict(merged)
            # A judge that could not evaluate leaves the case ERRORED, which
            # the comparison reports as INCOMPLETE rather than a pass.
            status, reason = verdict.status, verdict.reason
        steps = detail.get("steps") or ()
        calls = tuple(
            ToolCall.from_record(s["record"], s.get("latency_ms"))
            for s in steps
            if s.get("kind") == "tool_call" and isinstance(s.get("record"), Mapping)
        )
        agent = case.get("agent_result")
        agent_steps = agent.get("steps") if isinstance(agent, Mapping) else None
        latency = case.get("latency_ms")
        state = detail.get("state") or {}
        return cls(
            case_id=str(case.get("id")) if case.get("id") else None,
            status=status,
            reason=reason if isinstance(reason, str) else None,
            results=tuple(merged),
            calls=calls,
            steps=tuple(trajectory(steps, agent if isinstance(agent, Mapping) else None)),
            final_state=state.get("final") if isinstance(state.get("final"), Mapping) else None,
            latency_ms=float(latency) if isinstance(latency, int | float) else None,
            agent_steps=agent_steps
            if isinstance(agent_steps, int) and not isinstance(agent_steps, bool)
            else None,
            trace_id=case.get("trace_id") if isinstance(case.get("trace_id"), str) else None,
            tokens=tokens,
            cost_usd=cost_usd,
            escalation_tools=escalation_tools,
        )

    @property
    def done(self) -> bool:
        return self.status in DONE

    def metrics(self) -> dict[str, float | int | None]:
        failed = [r for r in self.results if r.status == "FAIL"]
        semantic = [
            r.score for r in self.results if r.expectation.get("type") == "semantic" and r.score is not None
        ]
        dups = duplicate_side_effects(self.calls)
        return {
            "critical_failures": sum(1 for r in failed if r.critical),
            "failed_expectations": len(failed),
            "policy_violations": len(policy_violations(self.calls)),
            "retries": len(retries(self.calls)),
            "duplicate_side_effects": sum(len(v) - 1 for v in dups.values()),
            "argument_failures": sum(1 for r in failed if r.expectation.get("type") == "toolArgs"),
            "tool_calls": len(agent_calls(self.calls)),
            "escalations": len(escalations(self.calls, self.escalation_tools)),
            "steps": self.agent_steps,
            "latency_ms": self.latency_ms,
            "tokens": self.tokens,
            "cost_usd": self.cost_usd,
            "semantic_score": round(statistics.fmean(semantic), 4) if semantic else None,
        }

    def tools(self) -> list[str]:
        return sorted({c.tool for c in agent_calls(self.calls)})

    def risk_tier(self) -> str:
        """The riskiest tool the case touched (``none`` without tool calls)."""
        risks = {c.risk for c in agent_calls(self.calls) if c.risk in _RISK_ORDER}
        return max(risks, key=_RISK_ORDER.index) if risks else "none"

    def labels(self) -> list[str]:
        return sorted({r.label for r in self.results if r.status == "FAIL" and r.label})

    def to_json(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "status": self.status,
            "reason": self.reason,
            "trace_id": self.trace_id,
            "labels": self.labels(),
            "tools": self.tools(),
            "metrics": self.metrics(),
        }


def _change(direction: Direction, abs_tol: float, rel_tol: float, b: Any, c: Any) -> str:
    if b is None or c is None:
        return "unknown"
    delta = float(c) - float(b)
    if abs(delta) <= max(abs_tol, rel_tol * abs(float(b))) or delta == 0:
        return "same"
    if direction == "neutral":
        return "changed"
    better = delta < 0 if direction == "lower" else delta > 0
    return "better" if better else "worse"


def metric_deltas(b: Side, c: Side) -> dict[str, dict[str, Any]]:
    bm, cm = b.metrics(), c.metrics()
    out: dict[str, dict[str, Any]] = {}
    for name, (direction, abs_tol, rel_tol) in METRICS.items():
        bv, cv = bm[name], cm[name]
        delta = None if bv is None or cv is None else round(float(cv) - float(bv), 6)
        out[name] = {
            "baseline": bv,
            "candidate": cv,
            "delta": delta,
            "change": _change(direction, abs_tol, rel_tol, bv, cv),
        }
    return out


ExpectationChange = Literal["fixed", "broken", "still_failing", "same", "not_comparable"]


def _expectation_change(b: EvaluationResult | None, c: EvaluationResult | None) -> ExpectationChange:
    bs = b.status if b is not None else None
    cs = c.status if c is not None else None
    if cs == "FAIL":
        return "still_failing" if bs == "FAIL" else "broken"
    if bs == "FAIL" and cs == "PASS":
        return "fixed"
    if bs == cs == "PASS":
        return "same"
    return "not_comparable"


def expectation_changes(b: Side, c: Side) -> list[dict[str, Any]]:
    """Every expectation of the scenario, baseline next to candidate."""

    def key(r: EvaluationResult) -> str:
        return str(r.expectation.get("id"))

    by_b = {key(r): r for r in b.results}
    by_c = {key(r): r for r in c.results}
    order = list(by_b) + [k for k in by_c if k not in by_b]
    out: list[dict[str, Any]] = []
    for k in order:
        rb, rc = by_b.get(k), by_c.get(k)
        meta = (rc or rb).expectation if (rc or rb) is not None else {}  # type: ignore[union-attr]
        change = _expectation_change(rb, rc)
        entry: dict[str, Any] = {
            "id": k,
            "type": str(meta.get("type") or ""),
            "critical": bool(meta.get("critical", False)),
            "baseline": rb.status if rb else None,
            "candidate": rc.status if rc else None,
            "change": change,
        }
        if change == "broken" and rb is not None and rb.status != "PASS":
            # The baseline could not evaluate it: counted as new, flagged.
            entry["baseline_unverified"] = True
        failing = rc if rc is not None and rc.status in ("FAIL", "ERROR") else None
        if failing is not None:
            entry["candidate_reason"] = failing.reason
            if failing.label:
                entry["label"] = failing.label
        if rb is not None and rb.status in ("FAIL", "ERROR"):
            entry["baseline_reason"] = rb.reason
        for side, r in (("baseline_score", rb), ("candidate_score", rc)):
            if r is not None and meta.get("type") == "semantic" and r.score is not None:
                entry[side] = r.score
        out.append(entry)
    return out


@dataclass(frozen=True)
class CaseComparison:
    scenario_name: str
    severity: str
    tags: tuple[str, ...]
    classification: Classification
    reason: str
    baseline: Side
    candidate: Side
    expectations: tuple[Mapping[str, Any], ...] = ()
    metrics: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    divergence: Mapping[str, Any] | None = None
    alignment: tuple[Mapping[str, Any], ...] = ()
    state_changes: tuple[Mapping[str, Any], ...] = ()
    tool_selection: Mapping[str, list[str]] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "scenario_name": self.scenario_name,
            "severity": self.severity,
            "tags": list(self.tags),
            "classification": self.classification,
            "reason": self.reason,
            "baseline": self.baseline.to_json(),
            "candidate": self.candidate.to_json(),
            "expectations": [dict(e) for e in self.expectations],
            "metrics": {k: dict(v) for k, v in self.metrics.items()},
            "divergence": dict(self.divergence) if self.divergence is not None else None,
            "trajectories": {
                "baseline": [s.to_json() for s in self.baseline.steps],
                "candidate": [s.to_json() for s in self.candidate.steps],
                "alignment": [dict(r) for r in self.alignment],
            },
            "state_changes": [dict(c) for c in self.state_changes],
            "tool_selection": {k: list(v) for k, v in self.tool_selection.items()},
            "risk": max(
                (self.baseline.risk_tier(), self.candidate.risk_tier()),
                key=lambda t: _RISK_ORDER.index(t) if t in _RISK_ORDER else -1,
            ),
        }


def _ids(entries: Iterable[Mapping[str, Any]]) -> str:
    ids = [str(e["id"]) for e in entries]
    return ", ".join(ids[:5]) + (f" (+{len(ids) - 5} more)" if len(ids) > 5 else "")


def _classify(b: Side, c: Side, expectations: Sequence[Mapping[str, Any]]) -> tuple[Classification, str]:
    if not c.done or not b.done:
        side, other = ("candidate", c) if not c.done else ("baseline", b)
        detail = f": {other.reason}" if other.reason else ""
        return "INCOMPLETE", f"The {side}'s case ended {other.status}{detail}"
    broken = [e for e in expectations if e["change"] == "broken"]
    fixed = [e for e in expectations if e["change"] == "fixed"]
    critical = [e for e in broken if e["critical"]]
    if critical:
        first = critical[0]
        why = first.get("candidate_reason") or ""
        return "NEW_CRITICAL_FAILURE", f"New critical failure {first['id']}: {why}".rstrip(": ")
    if broken:
        return "REGRESSED", f"Newly failing: {_ids(broken)}."
    if fixed:
        return "IMPROVED", f"Now passing: {_ids(fixed)}."
    still = [e for e in expectations if e["change"] == "still_failing"]
    if still:
        return "UNCHANGED", f"Still failing on both sides: {_ids(still)}."
    return "UNCHANGED", "Same result on both sides."


def compare_case(
    scenario_name: str, severity: str, tags: Sequence[str], baseline: Side, candidate: Side
) -> CaseComparison:
    expectations = expectation_changes(baseline, candidate) if baseline.done or candidate.done else []
    classification, reason = _classify(baseline, candidate, expectations)
    divergence = None
    rows: list[dict[str, Any]] = []
    changes: list[dict[str, Any]] = []
    if baseline.steps and candidate.steps:
        d = first_divergence(baseline.steps, candidate.steps)
        divergence = d.to_json() if d is not None else None
        rows = align(baseline.steps, candidate.steps)
        changes = state_changes(baseline.final_state, candidate.final_state)
    bt, ct = set(baseline.tools()), set(candidate.tools())
    return CaseComparison(
        scenario_name=scenario_name,
        severity=severity,
        tags=tuple(tags),
        classification=classification,
        reason=reason,
        baseline=baseline,
        candidate=candidate,
        expectations=tuple(expectations),
        metrics=metric_deltas(baseline, candidate),
        divergence=divergence,
        alignment=tuple(rows),
        state_changes=tuple(changes),
        tool_selection={"added": sorted(ct - bt), "removed": sorted(bt - ct)},
    )


def _percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, round(q * (len(ordered) - 1))))
    return round(ordered[k], 3)


def _side_totals(sides: Sequence[Side]) -> dict[str, Any]:
    metrics = [s.metrics() for s in sides]
    latencies = [float(m["latency_ms"]) for m in metrics if m["latency_ms"] is not None]
    tokens = [int(m["tokens"]) for m in metrics if m["tokens"] is not None]
    costs = [float(m["cost_usd"]) for m in metrics if m["cost_usd"] is not None]
    semantic = [float(m["semantic_score"]) for m in metrics if m["semantic_score"] is not None]
    statuses = Counter(s.status for s in sides)

    def total(name: str) -> int:
        return sum(int(m[name] or 0) for m in metrics)

    return {
        "cases": len(sides),
        "passed": statuses["PASSED"],
        "failed": statuses["FAILED"],
        "incomplete": len(sides) - statuses["PASSED"] - statuses["FAILED"],
        "critical_failures": total("critical_failures"),
        "policy_violations": total("policy_violations"),
        "retries": total("retries"),
        "duplicate_side_effects": total("duplicate_side_effects"),
        "escalations": total("escalations"),
        "tool_calls": total("tool_calls"),
        "latency_ms_p50": _percentile(latencies, 0.5),
        "latency_ms_p95": _percentile(latencies, 0.95),
        # Usage is summed over the cases whose trace reported it, and says
        # how many that was: an unknown is never added as zero.
        "tokens": sum(tokens) if tokens else None,
        "tokens_known": len(tokens),
        "cost_usd": round(sum(costs), 6) if costs else None,
        "cost_known": len(costs),
        "semantic_score": round(statistics.fmean(semantic), 4) if semantic else None,
    }


def _counts(cases: Iterable[CaseComparison]) -> dict[str, int]:
    counter = Counter(c.classification for c in cases)
    return {k: counter[k] for k in CLASSIFICATIONS}


def summarize(cases: Sequence[CaseComparison]) -> dict[str, Any]:
    """The run at a glance, without averaging away a single failure: counts
    per classification, totals per side, the new critical failures by name,
    and the same counts sliced by severity, tag, tool, risk tier and failure
    class."""
    by: dict[str, dict[str, list[CaseComparison]]] = defaultdict(lambda: defaultdict(list))
    labels: dict[str, Counter[str]] = defaultdict(Counter)
    for c in cases:
        by["severity"][c.severity].append(c)
        for t in c.tags:
            by["tag"][t].append(c)
        for tool in sorted(set(c.baseline.tools()) | set(c.candidate.tools())):
            by["tool"][tool].append(c)
        by["risk"][c.to_json()["risk"]].append(c)
        for side, s in (("baseline", c.baseline), ("candidate", c.candidate)):
            for label in s.labels():
                labels[label][side] += 1
    slices: dict[str, dict[str, Any]] = {
        dim: {value: _counts(members) for value, members in sorted(values.items())}
        for dim, values in sorted(by.items())
    }
    slices["failure_class"] = {
        label: {"baseline": n["baseline"], "candidate": n["candidate"]} for label, n in sorted(labels.items())
    }
    return {
        "counts": _counts(cases),
        "baseline": _side_totals([c.baseline for c in cases]),
        "candidate": _side_totals([c.candidate for c in cases]),
        "new_critical_failures": [
            {
                "scenario_name": c.scenario_name,
                "expectations": [
                    e["id"] for e in c.expectations if e["change"] == "broken" and e["critical"]
                ],
            }
            for c in cases
            if c.classification == "NEW_CRITICAL_FAILURE"
        ],
        "regressed": [c.scenario_name for c in cases if c.classification == "REGRESSED"],
        "improved": [c.scenario_name for c in cases if c.classification == "IMPROVED"],
        "incomplete": [c.scenario_name for c in cases if c.classification == "INCOMPLETE"],
        "slices": slices,
    }
