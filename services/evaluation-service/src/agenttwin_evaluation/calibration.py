"""Judge calibration against human labels (spec §16.4, §66).

A judge is run over examples whose correct label a person decided, and its
agreement is measured: raw agreement, Cohen's kappa (agreement beyond what
the two label rates alone would produce) and the confusion matrix. A judge
error on an example counts as a disagreement — a judge that cannot answer is
not a judge that agrees.

A judge is *calibrated* for a criterion when it agreed on enough examples:
at least ``min_examples``, raw agreement at least ``min_agreement`` and
kappa at least ``min_kappa``. Only a calibrated judge's failure may block a
release on its own (the gate, Phase 5); an uncalibrated judge still grades,
and says so.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from agenttwin_evaluation.judges import JudgeError, JudgeEvidence, JudgeProvider, JudgeRequest

__all__ = [
    "MIN_AGREEMENT",
    "MIN_EXAMPLES",
    "MIN_KAPPA",
    "CalibrationReport",
    "LabeledExample",
    "agreement",
    "calibrate",
]

MIN_EXAMPLES = 20
MIN_AGREEMENT = 0.8
MIN_KAPPA = 0.6
Label = Literal["pass", "fail"]


@dataclass(frozen=True)
class LabeledExample:
    id: str
    criterion: str
    rubric: str
    customer_message: str
    answer: str
    human_label: Label
    tool_calls: tuple[JudgeEvidence, ...] = ()

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> LabeledExample:
        label = raw.get("human_label")
        if label not in ("pass", "fail"):
            raise ValueError(f"example {raw.get('id')!r}: human_label must be pass or fail")
        return cls(
            id=str(raw["id"]),
            criterion=str(raw.get("criterion") or "rubric"),
            rubric=str(raw["rubric"]),
            customer_message=str(raw["customer_message"]),
            answer=str(raw["answer"]),
            human_label=label,
            tool_calls=tuple(
                JudgeEvidence(str(t["ref"]), str(t["text"])) for t in raw.get("tool_calls") or ()
            ),
        )

    def request(self) -> JudgeRequest:
        return JudgeRequest(
            criterion=self.criterion,
            rubric=self.rubric,
            customer_message=self.customer_message,
            answer=self.answer,
            tool_calls=self.tool_calls,
        )


def agreement(pairs: Sequence[tuple[Label, Label | None]]) -> dict[str, Any]:
    """``(human, judge)`` pairs; ``None`` is a judge error (a disagreement)."""
    n = len(pairs)
    tp = sum(1 for h, j in pairs if h == "pass" and j == "pass")
    tn = sum(1 for h, j in pairs if h == "fail" and j == "fail")
    fp = sum(1 for h, j in pairs if h == "fail" and j == "pass")
    fn = sum(1 for h, j in pairs if h == "pass" and j == "fail")
    errors = sum(1 for _, j in pairs if j is None)
    agreed = tp + tn
    accuracy = agreed / n if n else None
    kappa = None
    if n:
        human_pass = sum(1 for h, _ in pairs if h == "pass") / n
        judge_pass = sum(1 for _, j in pairs if j == "pass") / n
        judge_fail = sum(1 for _, j in pairs if j == "fail") / n
        expected = human_pass * judge_pass + (1 - human_pass) * judge_fail
        if expected < 1:
            kappa = round((agreed / n - expected) / (1 - expected), 4)
    return {
        "examples": n,
        "agreed": agreed,
        "accuracy": round(accuracy, 4) if accuracy is not None else None,
        "kappa": kappa,
        "confusion": {"true_pass": tp, "true_fail": tn, "false_pass": fp, "false_fail": fn},
        "errors": errors,
    }


@dataclass(frozen=True)
class CalibrationReport:
    criterion: str
    judge: Mapping[str, str]
    metrics: Mapping[str, Any]
    calibrated: bool
    reason: str
    disagreements: tuple[Mapping[str, Any], ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "criterion": self.criterion,
            "judge": dict(self.judge),
            "metrics": dict(self.metrics),
            "calibrated": self.calibrated,
            "reason": self.reason,
            "disagreements": [dict(d) for d in self.disagreements],
        }


def _decision(
    m: Mapping[str, Any], min_examples: int, min_agreement: float, min_kappa: float
) -> tuple[bool, str]:
    n, accuracy, kappa = m["examples"], m["accuracy"], m["kappa"]
    needed = max(min_examples, 1)
    if n < needed:
        return False, f"Only {n} labeled example(s); calibration needs at least {needed}."
    if accuracy < min_agreement:
        return False, f"Agreement {accuracy:.1%} is below the required {min_agreement:.1%}."
    if kappa is None:
        # Everyone gave every example the same label: agreement beyond chance
        # cannot be measured, so neither can the judge.
        return False, "Cohen's kappa is undefined: people and the judge gave every example the same label."
    if kappa < min_kappa:
        return False, f"Cohen's kappa {kappa:.2f} is below the required {min_kappa:.2f}."
    return True, f"Agreed with people on {m['agreed']} of {n} examples (kappa {kappa:.2f})."


async def calibrate(
    judge: JudgeProvider,
    identity: Mapping[str, str],
    criterion: str,
    examples: Sequence[LabeledExample],
    *,
    min_examples: int = MIN_EXAMPLES,
    min_agreement: float = MIN_AGREEMENT,
    min_kappa: float = MIN_KAPPA,
) -> CalibrationReport:
    """Runs the judge over the examples of one criterion, one at a time (a
    calibration is small and must not trip provider rate limits)."""
    pairs: list[tuple[Label, Label | None]] = []
    disagreements: list[dict[str, Any]] = []
    for example in examples:
        try:
            verdict = await judge.judge(example.request())
        except JudgeError as err:
            pairs.append((example.human_label, None))
            disagreements.append(
                {"id": example.id, "human": example.human_label, "judge": None, "error": err.kind}
            )
            continue
        pairs.append((example.human_label, verdict.label))
        if verdict.label != example.human_label:
            disagreements.append(
                {
                    "id": example.id,
                    "human": example.human_label,
                    "judge": verdict.label,
                    "score": verdict.score,
                }
            )
    metrics = agreement(pairs)
    calibrated, reason = _decision(metrics, min_examples, min_agreement, min_kappa)
    return CalibrationReport(
        criterion=criterion,
        judge=dict(identity),
        metrics=metrics,
        calibrated=calibrated,
        reason=reason,
        disagreements=tuple(disagreements),
    )
