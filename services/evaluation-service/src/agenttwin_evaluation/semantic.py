"""Semantic expectations graded by a judge (spec §16.3, §16.4, §49, §66).

A scenario's ``semantic`` expectation gives a rubric, an optional criterion
(``category``), a threshold (default 0.7) and optionally the judge it wants.
The simulation leaves it SKIPPED; the evaluation service grades it here with
the configured judge, for each side of a comparison.

Discipline:

* The case passes the expectation only when the judge labels the answer
  ``pass`` *and* its score reaches the threshold.
* A judge failure is ``ERROR`` with the label ``EVALUATION_ERROR`` — never a
  score of 0 and never a pass; the case it belongs to is then not a pass.
* A run's judging is budgeted (cost and calls). Past the budget, semantic
  expectations are ``SKIPPED`` with the reason; deterministic expectations
  are never skipped for cost.
* An agent that gave no answer fails every semantic expectation without a
  judge call: each criterion grades the reply.
* Verdicts are cached by everything that shapes them (the judge, its prompt
  version and hash, the criterion, the rubric and the material shown); a hit
  is reused as is, never approximated.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from agenttwin.hashing import canonical_json
from agenttwin_core.evaluators import EvaluationResult, Evidence, ToolCall
from agenttwin_evaluation.judges import (
    CRITERIA,
    JudgeError,
    JudgeEvidence,
    JudgeProvider,
    JudgeRequest,
    JudgeVerdict,
    judge_identity,
)

__all__ = [
    "DEFAULT_THRESHOLD",
    "EVALUATOR_NAME",
    "EVALUATOR_VERSION",
    "JudgeBudget",
    "Judged",
    "SemanticJudging",
    "VerdictCache",
    "judge_request",
]

EVALUATOR_NAME = "semantic.judge"
EVALUATOR_VERSION = "1.0.0"
DEFAULT_THRESHOLD = 0.7
MAX_TOOL_EVIDENCE = 30
_RESPONSE_CHARS = 300


class VerdictCache(Protocol):
    async def get(self, key: str) -> JudgeVerdict | None: ...

    async def put(self, key: str, verdict: JudgeVerdict, judge: Mapping[str, str]) -> None: ...


@dataclass
class JudgeBudget:
    """What one evaluation run may spend on judging. Calls without a known
    cost count against ``max_calls`` only; that they happened is reported."""

    max_cost_usd: float | None = 5.0
    max_calls: int = 200
    spent_usd: float = 0.0
    calls: int = 0
    unknown_cost_calls: int = 0
    exhausted: bool = False

    def allows(self) -> bool:
        over_cost = self.max_cost_usd is not None and self.spent_usd >= self.max_cost_usd
        if self.calls >= self.max_calls or over_cost:
            self.exhausted = True
            return False
        return True

    def record(self, verdict: JudgeVerdict) -> None:
        self.calls += 1
        if verdict.cost_usd is None:
            self.unknown_cost_calls += 1
        else:
            self.spent_usd = round(self.spent_usd + verdict.cost_usd, 6)

    def describe(self) -> str:
        calls = f"{self.calls} of {self.max_calls} calls"
        if self.max_cost_usd is None:
            return calls
        return f"${self.spent_usd:g} of ${self.max_cost_usd:g}, {calls}"

    def to_json(self) -> dict[str, Any]:
        return {
            "max_cost_usd": self.max_cost_usd,
            "max_calls": self.max_calls,
            "spent_usd": self.spent_usd,
            "calls": self.calls,
            "unknown_cost_calls": self.unknown_cost_calls,
            "exhausted": self.exhausted,
        }


def _call_text(call: ToolCall) -> str:
    args = ", ".join(f"{k}={canonical_json(v)}" for k, v in sorted(call.arguments.items()))
    response = canonical_json(call.response) if call.response is not None else "none"
    if len(response) > _RESPONSE_CHARS:
        response = response[: _RESPONSE_CHARS - 1] + "…"
    return f"{call.tool}({args}) -> HTTP {call.http_status} {call.status}; response: {response}"


def judge_request(
    spec: Mapping[str, Any], customer_message: str, answer: str | None, calls: Sequence[ToolCall]
) -> JudgeRequest:
    category = str(spec.get("category") or "")
    # Redeliveries are the network's, not the agent's: the judge grades what
    # the agent did.
    made = [c for c in calls if not c.redelivered]
    return JudgeRequest(
        criterion=category if category in CRITERIA else "rubric",
        rubric=str(spec.get("rubric") or ""),
        customer_message=customer_message,
        answer=answer,
        tool_calls=tuple(
            JudgeEvidence(ref=f"tool_call:{c.seq}", text=_call_text(c)) for c in made[:MAX_TOOL_EVIDENCE]
        ),
        omitted_tool_calls=max(0, len(made) - MAX_TOOL_EVIDENCE),
    )


@dataclass(frozen=True)
class Judged:
    """One graded semantic expectation, with what the result alone cannot
    carry: the judge, its verdict, and whether it came from the cache."""

    result: EvaluationResult
    judge: Mapping[str, str]
    verdict: JudgeVerdict | None = None
    cached: bool = False
    cache_key: str | None = None


def _meta(spec: Mapping[str, Any], index: int) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "id": str(spec.get("id") or f"e{index + 1}"),
        "type": "semantic",
        "critical": bool(spec.get("critical", False)),
    }
    for key in ("description", "category"):
        if spec.get(key) is not None:
            meta[key] = spec[key]
    return meta


@dataclass
class SemanticJudging:
    """Grades the semantic expectations of one evaluation run."""

    judge: JudgeProvider
    budget: JudgeBudget = field(default_factory=JudgeBudget)
    cache: VerdictCache | None = None
    # Whether the configured judge passed its latest calibration (§16.4) —
    # for every criterion, or for the criteria named — recorded with every
    # verdict for the release gate to weigh.
    calibrated: bool | frozenset[str] = False
    timeout_s: float = 120.0
    # The criteria this judging graded (for the run-level identity).
    used: set[str] = field(default_factory=set)

    def is_calibrated(self, criterion: str) -> bool:
        if isinstance(self.calibrated, bool):
            return self.calibrated
        return criterion in self.calibrated

    def identity(self, criterion: str | None = None) -> dict[str, str]:
        """The judge a verdict records; without a criterion, the run's: it
        counts as calibrated only when every criterion it graded is."""
        if criterion is not None:
            ok = self.is_calibrated(criterion)
        elif isinstance(self.calibrated, bool):
            ok = self.calibrated
        else:
            ok = bool(self.used) and all(self.is_calibrated(c) for c in self.used)
        return judge_identity(self.judge) | {"calibrated": "true" if ok else "false"}

    def _result(self, spec: Mapping[str, Any], index: int, **kw: Any) -> EvaluationResult:
        return EvaluationResult(
            evaluator=EVALUATOR_NAME,
            evaluator_version=EVALUATOR_VERSION,
            expectation=_meta(spec, index),
            **kw,
        )

    def _config_evidence(self, criterion: str) -> Evidence:
        j = self.identity(criterion)
        state = "calibrated" if self.is_calibrated(criterion) else "not calibrated"
        return Evidence(
            kind="config",
            detail=f"Judged by {j['provider']} {j['model']} ({j['prompt_version']}, {state}).",
        )

    async def evaluate(
        self,
        spec: Mapping[str, Any],
        index: int,
        *,
        customer_message: str,
        answer: str | None,
        calls: Sequence[ToolCall],
    ) -> Judged:
        category = str(spec.get("category") or "")
        identity = self.identity(category if category in CRITERIA else "rubric")
        if answer is None or not answer.strip():
            # Every criterion grades the reply; without one there is nothing
            # that could meet the rubric, and nothing worth paying a judge for.
            return Judged(
                self._result(
                    spec,
                    index,
                    status="FAIL",
                    reason="The agent produced no answer, so the rubric cannot be met (no judge call).",
                    label="SEMANTIC_FAIL",
                    evidence=(Evidence(kind="output", detail="no answer"),),
                ),
                identity,
            )
        wanted = spec.get("judge")
        if wanted and wanted not in (self.judge.provider, self.judge.model):
            return Judged(
                self._result(
                    spec,
                    index,
                    status="SKIPPED",
                    reason=(
                        f"This expectation asks for the judge {wanted!r}; the configured judge is "
                        f"{self.judge.provider} {self.judge.model}."
                    ),
                ),
                identity,
            )
        request = judge_request(spec, customer_message, answer, calls)
        key = request.cache_key(self.judge)
        if self.cache is not None:
            hit = await self.cache.get(key)
            if hit is not None:
                self.used.add(request.criterion)
                return Judged(
                    self._graded(spec, index, hit, request.criterion),
                    identity,
                    hit,
                    cached=True,
                    cache_key=key,
                )
        if not self.budget.allows():
            return Judged(
                self._result(
                    spec,
                    index,
                    status="SKIPPED",
                    reason=f"Not judged: this run's judge budget is spent ({self.budget.describe()}).",
                ),
                identity,
                cache_key=key,
            )
        try:
            async with asyncio.timeout(self.timeout_s):
                verdict = await self.judge.judge(request)
        except JudgeError as err:
            self.budget.calls += 1
            return Judged(
                self._result(
                    spec,
                    index,
                    status="ERROR",
                    reason=f"The judge could not evaluate this expectation ({err.kind}): {err}",
                    label="EVALUATION_ERROR",
                    evidence=(self._config_evidence(request.criterion),),
                ),
                identity,
                cache_key=key,
            )
        except TimeoutError:
            self.budget.calls += 1
            return Judged(
                self._result(
                    spec,
                    index,
                    status="ERROR",
                    reason="The judge could not evaluate this expectation (timeout).",
                    label="EVALUATION_ERROR",
                    evidence=(self._config_evidence(request.criterion),),
                ),
                identity,
                cache_key=key,
            )
        self.budget.record(verdict)
        if self.cache is not None:
            await self.cache.put(key, verdict, identity)
        self.used.add(request.criterion)
        return Judged(self._graded(spec, index, verdict, request.criterion), identity, verdict, cache_key=key)

    def _graded(
        self, spec: Mapping[str, Any], index: int, verdict: JudgeVerdict, criterion: str
    ) -> EvaluationResult:
        threshold = float(spec.get("threshold", DEFAULT_THRESHOLD))
        passed = verdict.label == "pass" and verdict.score >= threshold
        evidence = [self._config_evidence(criterion)]
        for e in verdict.evidence:
            ref = e["ref"]
            kind = "tool_call" if ref.startswith("tool_call:") else "output"
            evidence.append(
                Evidence(kind=kind, ref=ref if kind == "tool_call" else None, detail=f"“{e['quote']}”")
            )
        reason = (
            f"{verdict.reason} (label {verdict.label}, score {verdict.score:.2f}, threshold {threshold:.2f}, "
            f"confidence {verdict.confidence:.2f})"
        )
        return self._result(
            spec,
            index,
            status="PASS" if passed else "FAIL",
            reason=reason,
            score=verdict.score,
            label=None if passed else "SEMANTIC_FAIL",
            evidence=tuple(evidence),
        )
