"""The evaluator plugin model (spec §84).

An evaluator is an object with a ``name``, a ``version`` and an async
``evaluate(context) -> EvaluationResult``. Built-in evaluators are created
from expectation specs through an explicit registry; there is no dynamic
plugin loading (no remote code execution by configuration).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

__all__ = [
    "EvaluationContext",
    "EvaluationResult",
    "Evaluator",
    "Evidence",
    "Status",
    "ToolCall",
]

Status = Literal["PASS", "FAIL", "ERROR", "SKIPPED"]


@dataclass(frozen=True)
class ToolCall:
    """One tool invocation as recorded by the tool twin (not self-reported)."""

    seq: int
    tool: str
    arguments: Mapping[str, Any]
    http_status: int
    status: str  # ok | error | denied | rate_limited | timeout | invalid | not_found | unknown_tool
    response: Any = None
    error_code: str | None = None
    risk: str | None = None
    fault: str | None = None
    # The call applied its declared effects to the twin's state.
    mutated: bool = False
    # Per its definition the tool mutates state when it succeeds (a twin
    # "mutate" handler with effects); a success without a mutation is a lie.
    expects_mutation: bool = False
    # Answered from the idempotency record instead of executing again.
    replayed: bool = False
    # Executed under a granted human approval.
    approved: bool = False
    # A duplicate delivery of the previous call injected by the network
    # (message duplication): a side effect, not a decision of the agent.
    redelivered: bool = False
    effect_key: str | None = None
    cross_tenant: str | None = None  # "denied" | "allowed"
    policy_violation: str | None = None
    latency_ms: float = 0.0

    @classmethod
    def from_record(cls, record: Mapping[str, Any], latency_ms: float | None = None) -> ToolCall:
        """A tool call as the twin recorded it (the simulation contract's
        ``ToolCallRecord``): the one conversion the simulation worker and the
        evaluation service share, so both evaluate the same call."""

        def text(key: str) -> str | None:
            value = record.get(key)
            return value if isinstance(value, str) else None

        arguments = record.get("arguments")
        return cls(
            seq=int(record["seq"]),
            tool=str(record["tool"]),
            arguments=dict(arguments) if isinstance(arguments, Mapping) else {},
            http_status=int(record.get("http_status") or 0),
            status=str(record.get("status") or "error"),
            response=record.get("response"),
            error_code=text("error_code"),
            risk=text("risk"),
            fault=text("fault"),
            mutated=bool(record.get("mutated", False)),
            expects_mutation=bool(record.get("expects_mutation", False)),
            replayed=bool(record.get("replayed", False)),
            approved=bool(record.get("approved", False)),
            redelivered=bool(record.get("redelivered", False)),
            effect_key=text("effect_key"),
            cross_tenant=text("cross_tenant"),
            policy_violation=text("policy_violation"),
            latency_ms=float(latency_ms or record.get("delay_ms") or 0.0),
        )

    @property
    def succeeded(self) -> bool:
        return 200 <= self.http_status < 300

    @property
    def executed(self) -> bool:
        """The side effect happened (or the tool told the caller it did)."""
        return self.mutated or (self.succeeded and not self.replayed and self.risk not in (None, "READ"))


@dataclass(frozen=True)
class EvaluationContext:
    """Everything an evaluator may look at for one case."""

    input: str = ""
    output: str | None = None
    claimed_outcome: str | None = None
    business_outcome: str | None = None
    agent_status: str | None = None
    steps: int | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    state_before: Mapping[str, Any] | None = None
    state_after: Mapping[str, Any] | None = None
    latency_ms: float | None = None
    cost_usd: float | None = None
    # Planted canary values that must never leave the agent.
    secrets: tuple[str, ...] = ()
    tenant: str | None = None
    # String values of other tenants' records (cross-tenant leak detection).
    foreign_values: tuple[str, ...] = ()
    escalation_tools: tuple[str, ...] = ("escalate_to_human",)


@dataclass(frozen=True)
class Evidence:
    """A pointer to what supports a verdict. Never carries secret values."""

    kind: str  # tool_call | state | output | agent | config
    detail: str
    ref: str | None = None
    expected: Any = None
    actual: Any = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind, "detail": self.detail}
        if self.ref is not None:
            out["ref"] = self.ref
        if self.expected is not None:
            out["expected"] = self.expected
        if self.actual is not None:
            out["actual"] = self.actual
        return out

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> Evidence:
        ref = raw.get("ref")
        return cls(
            kind=str(raw.get("kind") or ""),
            detail=str(raw.get("detail") or ""),
            ref=ref if isinstance(ref, str) else None,
            expected=raw.get("expected"),
            actual=raw.get("actual"),
        )


@dataclass(frozen=True)
class EvaluationResult:
    status: Status
    reason: str
    evaluator: str
    evaluator_version: str
    score: float | None = None
    label: str | None = None
    evidence: tuple[Evidence, ...] = ()
    expectation: Mapping[str, Any] = field(default_factory=dict)

    @property
    def critical(self) -> bool:
        return bool(self.expectation.get("critical", False))

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "evaluator": self.evaluator,
            "evaluator_version": self.evaluator_version,
            "score": self.score,
            "label": self.label,
            "critical": self.critical,
            "expectation": dict(self.expectation),
            "evidence": [e.to_json() for e in self.evidence],
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> EvaluationResult:
        """The inverse of ``to_json`` (a stored or received expectation result)."""
        status = raw.get("status")
        if status not in ("PASS", "FAIL", "ERROR", "SKIPPED"):
            raise ValueError(f"unknown result status {status!r}")
        score, label = raw.get("score"), raw.get("label")
        expectation = raw.get("expectation")
        evidence = [e for e in raw.get("evidence") or () if isinstance(e, Mapping)]
        return cls(
            status=status,
            reason=str(raw.get("reason") or ""),
            evaluator=str(raw.get("evaluator") or "unknown"),
            evaluator_version=str(raw.get("evaluator_version") or "0"),
            score=float(score) if isinstance(score, int | float) and not isinstance(score, bool) else None,
            label=label if isinstance(label, str) else None,
            evidence=tuple(Evidence.from_json(e) for e in evidence),
            expectation=dict(expectation) if isinstance(expectation, Mapping) else {},
        )


class Evaluator(Protocol):
    name: str
    version: str

    async def evaluate(self, context: EvaluationContext) -> EvaluationResult: ...
