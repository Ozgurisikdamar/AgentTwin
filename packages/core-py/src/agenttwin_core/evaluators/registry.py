"""The explicit evaluator registry and the case verdict (spec §84, §26).

Evaluators are registered by name in code; a scenario can only select one of
the registered expectation types. There is no dynamic import of evaluator
code from configuration.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import regex

from agenttwin_core.evaluators.expectations import (
    BUILTIN_VERSIONS,
    Check,
    ExpectationEvaluator,
    builtin_checks,
)
from agenttwin_core.evaluators.model import EvaluationContext, EvaluationResult, Evaluator
from agenttwin_core.jsonschema_safe import schema_problem
from agenttwin_core.logx import Log
from agenttwin_core.paths import PathError, parse_path

__all__ = [
    "CaseVerdict",
    "Registry",
    "UnknownExpectation",
    "case_verdict",
    "default_registry",
    "evaluate_all",
    "expectation_problems",
]

EVALUATOR_PREFIX = "expectation."


class UnknownExpectation(KeyError):
    """The expectation type has no registered evaluator."""


@dataclass
class Registry:
    _checks: dict[str, tuple[str, str, Check]] = field(default_factory=dict)

    def register(self, expectation_type: str, check: Check, *, version: str, name: str | None = None) -> None:
        if expectation_type in self._checks:
            raise ValueError(f"expectation type {expectation_type!r} is already registered")
        self._checks[expectation_type] = (name or EVALUATOR_PREFIX + expectation_type, version, check)

    def create(self, spec: Mapping[str, Any], index: int = 0) -> Evaluator:
        kind = spec.get("type")
        entry = self._checks.get(str(kind))
        if entry is None:
            raise UnknownExpectation(str(kind))
        name, version, check = entry
        return ExpectationEvaluator(name=name, version=version, spec=spec, check=check, index=index)

    def types(self) -> list[str]:
        return sorted(self._checks)

    def versions(self) -> dict[str, str]:
        """Evaluator name -> version, pinned into run evidence."""
        return {name: version for name, version, _ in sorted(self._checks.values())}


def default_registry() -> Registry:
    reg = Registry()
    for kind, check in builtin_checks().items():
        reg.register(kind, check, version=BUILTIN_VERSIONS[kind])
    return reg


def _meta(spec: Mapping[str, Any], index: int) -> dict[str, Any]:
    return {
        "id": str(spec.get("id") or f"e{index + 1}"),
        "type": str(spec.get("type")),
        "critical": bool(spec.get("critical", False)),
    }


async def evaluate_all(
    registry: Registry,
    specs: Sequence[Mapping[str, Any]],
    context: EvaluationContext,
    *,
    log: Log | None = None,
) -> list[EvaluationResult]:
    """Evaluates every expectation. A broken evaluator yields an ERROR result
    for its expectation; it never aborts the others."""
    results: list[EvaluationResult] = []
    for i, spec in enumerate(specs):
        try:
            evaluator = registry.create(spec, i)
        except UnknownExpectation as err:
            results.append(
                EvaluationResult(
                    status="ERROR",
                    reason=f"No evaluator is registered for expectation type {err.args[0]!r}.",
                    evaluator="unknown",
                    evaluator_version="0",
                    label="EVALUATOR_ERROR",
                    expectation=_meta(spec, i),
                )
            )
            continue
        try:
            results.append(await evaluator.evaluate(context))
        except Exception as err:
            if log is not None:
                log.exception("evaluator crashed", evaluator=evaluator.name, error=str(err))
            results.append(
                EvaluationResult(
                    status="ERROR",
                    reason=f"The evaluator crashed ({type(err).__name__}).",
                    evaluator=evaluator.name,
                    evaluator_version=evaluator.version,
                    label="EVALUATOR_ERROR",
                    expectation=_meta(spec, i),
                )
            )
    return results


CaseStatus = Literal["PASSED", "FAILED", "ERRORED"]


@dataclass(frozen=True)
class CaseVerdict:
    status: CaseStatus
    reason: str
    passed: int
    failed: int
    errored: int
    skipped: int
    critical_failures: int
    # Fraction of evaluated (not skipped) expectations that passed.
    score: float | None
    labels: tuple[str, ...]


def case_verdict(results: Sequence[EvaluationResult]) -> CaseVerdict:
    """FAILED if any expectation failed; otherwise ERRORED if one could not be
    evaluated (including a SKIPPED critical expectation: mandatory critical
    evaluation that did not complete is never a pass); otherwise PASSED."""
    counts = {"PASS": 0, "FAIL": 0, "ERROR": 0, "SKIPPED": 0}
    for r in results:
        counts[r.status] += 1
    critical_failures = sum(1 for r in results if r.critical and r.status == "FAIL")
    skipped_critical = [r for r in results if r.critical and r.status == "SKIPPED"]
    labels = tuple(sorted({r.label for r in results if r.status == "FAIL" and r.label}))
    evaluated = counts["PASS"] + counts["FAIL"] + counts["ERROR"]
    score = counts["PASS"] / evaluated if evaluated else None
    failed = [r for r in results if r.status == "FAIL"]
    if failed:
        first = next((r for r in failed if r.critical), failed[0])
        status: CaseStatus = "FAILED"
        reason = first.reason
        if len(failed) > 1:
            reason += f" (+{len(failed) - 1} more failed expectation(s))"
    elif counts["ERROR"] or skipped_critical:
        status = "ERRORED"
        broken = next((r for r in results if r.status == "ERROR"), None)
        if broken is not None:
            reason = f"An expectation could not be evaluated: {broken.reason}"
        else:
            reason = f"A critical expectation was skipped: {skipped_critical[0].reason}"
    elif not results:
        status, reason = "ERRORED", "The scenario has no expectations."
    else:
        status = "PASSED"
        reason = f"All {counts['PASS']} evaluated expectation(s) passed."
        if counts["SKIPPED"]:
            reason += f" {counts['SKIPPED']} were skipped."
    return CaseVerdict(
        status=status,
        reason=reason,
        passed=counts["PASS"],
        failed=counts["FAIL"],
        errored=counts["ERROR"],
        skipped=counts["SKIPPED"],
        critical_failures=critical_failures,
        score=score,
        labels=labels,
    )


def _pattern_problem(pattern: Any) -> str | None:
    if not isinstance(pattern, str):
        return "must be a string"
    try:
        regex.compile(pattern)
    except regex.error as err:
        return f"is not a valid regular expression ({err})"
    return None


def expectation_problems(spec: Mapping[str, Any], registry: Registry | None = None) -> list[str]:
    """Problems the JSON schema cannot express (regex syntax, path syntax,
    usable JSON schema), reported when a scenario is registered."""
    problems: list[str] = []
    kind = spec.get("type")
    if registry is not None and str(kind) not in registry.types():
        problems.append(f"no evaluator is registered for type {kind!r}")
    if "path" in spec:
        try:
            parse_path(str(spec["path"]))
        except PathError as err:
            problems.append(f"path: {err}")
    for key in ("pattern", "matches"):
        if key in spec and (problem := _pattern_problem(spec[key])):
            problems.append(f"{key} {problem}")
    if kind == "outputJsonSchema" and (problem := schema_problem(spec.get("schema"))):
        problems.append(f"schema: {problem}")
    return problems
