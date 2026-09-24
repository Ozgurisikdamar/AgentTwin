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


class Evaluator(Protocol):
    name: str
    version: str

    async def evaluate(self, context: EvaluationContext) -> EvaluationResult: ...
