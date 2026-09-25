"""Judge calibration (spec §16.4, §66): agreement with people, Cohen's kappa
and the confusion matrix are computed exactly; a judge that cannot answer
disagrees; and a judge is calibrated only past every threshold."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from agenttwin_evaluation.calibration import (
    MIN_EXAMPLES,
    CalibrationReport,
    LabeledExample,
    agreement,
    calibrate,
)
from agenttwin_evaluation.judges import FakeJudge, JudgeError, JudgeEvidence, JudgeRequest, JudgeVerdict

IDENTITY = {"provider": "anthropic", "model": "claude-judge", "kind": "llm"}


def pairs(tp: int, tn: int, fp: int, fn: int, errors: tuple[str, ...] = ()) -> list[tuple[Any, Any]]:
    """``(human, judge)`` pairs with the given confusion counts, then one pair
    per error for the human label given."""
    return (
        [("pass", "pass")] * tp
        + [("fail", "fail")] * tn
        + [("fail", "pass")] * fp
        + [("pass", "fail")] * fn
        + [(h, None) for h in errors]
    )


def test_agreement_and_kappa_are_computed_exactly() -> None:
    # accuracy 17/20; chance agreement .5 * .55 + .5 * .45 = .5; kappa (.85 - .5) / .5.
    assert agreement(pairs(9, 8, 2, 1)) == {
        "examples": 20,
        "agreed": 17,
        "accuracy": 0.85,
        "kappa": 0.7,
        "confusion": {"true_pass": 9, "true_fail": 8, "false_pass": 2, "false_fail": 1},
        "errors": 0,
    }


def test_a_judge_error_is_a_disagreement() -> None:
    answered = agreement(pairs(5, 5, 0, 0))
    errored = agreement(pairs(4, 5, 0, 0, errors=("pass",)))
    assert (answered["accuracy"], answered["kappa"]) == (1.0, 1.0)
    # 9 of 10 agree; chance .5 * .4 + .5 * .5 = .45; kappa (.9 - .45) / .55.
    assert (errored["agreed"], errored["accuracy"], errored["kappa"], errored["errors"]) == (
        9,
        0.9,
        0.8182,
        1,
    )
    assert errored["confusion"] == {"true_pass": 4, "true_fail": 5, "false_pass": 0, "false_fail": 0}


def test_kappa_is_undefined_when_everyone_gave_one_label_and_zero_for_chance_agreement() -> None:
    assert agreement(pairs(20, 0, 0, 0))["kappa"] is None
    assert agreement(pairs(0, 20, 0, 0))["kappa"] is None
    # A judge that says "pass" to everything agrees half the time on a balanced set, by chance alone.
    always_pass = agreement(pairs(10, 0, 10, 0))
    assert (always_pass["accuracy"], always_pass["kappa"]) == (0.5, 0.0)
    assert agreement([]) == {
        "examples": 0,
        "agreed": 0,
        "accuracy": None,
        "kappa": None,
        "confusion": {"true_pass": 0, "true_fail": 0, "false_pass": 0, "false_fail": 0},
        "errors": 0,
    }


@given(
    st.lists(
        st.tuples(st.sampled_from(["pass", "fail"]), st.sampled_from(["pass", "fail", None])), max_size=60
    )
)
def test_agreement_properties(sample: list[tuple[Any, Any]]) -> None:
    m = agreement(sample)
    c = m["confusion"]
    assert c["true_pass"] + c["true_fail"] + c["false_pass"] + c["false_fail"] + m["errors"] == len(sample)
    assert m["agreed"] == c["true_pass"] + c["true_fail"]
    if m["kappa"] is not None:
        assert -1.0 <= m["kappa"] <= m["accuracy"] <= 1.0
    # Turning any agreement into an error never raises agreement.
    agreed = [i for i, (h, j) in enumerate(sample) if h == j]
    if agreed:
        worse = list(sample)
        worse[agreed[0]] = (sample[agreed[0]][0], None)
        assert agreement(worse)["accuracy"] < m["accuracy"]


class ByExample:
    """Answers each example with the label scripted for its answer text."""

    provider = "anthropic"
    model = "claude-judge"
    kind = "llm"

    def __init__(self, labels: dict[str, str | JudgeError]) -> None:
        self.labels = labels
        self.seen: list[str] = []

    async def judge(self, request: JudgeRequest) -> JudgeVerdict:
        answer = request.answer or ""
        self.seen.append(answer)
        label = self.labels[answer]
        if isinstance(label, JudgeError):
            raise label
        return JudgeVerdict(score=0.9 if label == "pass" else 0.1, label=label, confidence=0.9, reason="r")  # type: ignore[arg-type]


def examples(spec: list[tuple[str, str]]) -> list[LabeledExample]:
    """(human label, judge label or "error") per example; the answer text is the example id."""
    return [
        LabeledExample(
            id=f"x{i}",
            criterion="task_completion",
            rubric="The reply confirms the refund.",
            customer_message="Refund please.",
            answer=f"x{i}",
            human_label=human,  # type: ignore[arg-type]
        )
        for i, (human, _) in enumerate(spec)
    ]


def run_calibration(spec: list[tuple[str, str]], **kw: Any) -> tuple[CalibrationReport, ByExample]:
    judge = ByExample(
        {
            f"x{i}": JudgeError("timeout", "slow") if judged == "error" else judged
            for i, (_, judged) in enumerate(spec)
        }
    )
    report = asyncio.run(calibrate(judge, IDENTITY, "task_completion", examples(spec), **kw))
    return report, judge


BALANCED_AGREEING = [("pass", "pass")] * 10 + [("fail", "fail")] * 10


def test_a_judge_that_agrees_enough_is_calibrated_and_every_example_is_judged_in_order() -> None:
    spec = [*BALANCED_AGREEING[:-2], ("fail", "pass"), ("fail", "error")]
    report, judge = run_calibration(spec)
    assert judge.seen == [f"x{i}" for i in range(20)]
    assert report.calibrated
    # 18 of 20 agree; chance .5 * .55 + .5 * .4 = .475; kappa (.9 - .475) / .525.
    assert report.reason == "Agreed with people on 18 of 20 examples (kappa 0.81)."
    assert report.to_json() == {
        "criterion": "task_completion",
        "judge": IDENTITY,
        "metrics": {
            "examples": 20,
            "agreed": 18,
            "accuracy": 0.9,
            "kappa": 0.8095,
            "confusion": {"true_pass": 10, "true_fail": 8, "false_pass": 1, "false_fail": 0},
            "errors": 1,
        },
        "calibrated": True,
        "reason": "Agreed with people on 18 of 20 examples (kappa 0.81).",
        "disagreements": [
            {"id": "x18", "human": "fail", "judge": "pass", "score": 0.9},
            {"id": "x19", "human": "fail", "judge": None, "error": "timeout"},
        ],
    }


@pytest.mark.parametrize(
    ("spec", "reason"),
    [
        (BALANCED_AGREEING[:19], "Only 19 labeled example(s); calibration needs at least 20."),
        ([], "Only 0 labeled example(s); calibration needs at least 20."),
        (
            [*BALANCED_AGREEING[:15], *[("fail", "pass")] * 5],
            "Agreement 75.0% is below the required 80.0%.",
        ),
        (
            [("pass", "pass")] * 20,
            "Cohen's kappa is undefined: people and the judge gave every example the same label.",
        ),
        (
            # 17 of 20 agree (85%), but a judge that says "pass" this often
            # would agree 77% of the time by chance: kappa (.85 - .77) / .23.
            [("pass", "pass")] * 16 + [("fail", "pass")] * 3 + [("fail", "fail")],
            "Cohen's kappa 0.35 is below the required 0.60.",
        ),
        (
            # Errors are disagreements: 15 of 20 is below 80%.
            [*BALANCED_AGREEING[:15], *[("pass", "error")] * 5],
            "Agreement 75.0% is below the required 80.0%.",
        ),
    ],
)
def test_a_judge_is_not_calibrated_below_any_threshold(spec: list[tuple[str, str]], reason: str) -> None:
    report, _ = run_calibration(spec)
    assert not report.calibrated
    assert report.reason == reason


def test_the_thresholds_are_inclusive() -> None:
    # 16 of 20 agree, exactly 80%; the four errors are disagreements. Chance
    # agreement .7 * .5 + .3 * .3 = .44, so kappa (.8 - .44) / .56 = .64.
    report, _ = run_calibration([*BALANCED_AGREEING[:16], *[("pass", "error")] * 4])
    assert report.calibrated
    assert (report.metrics["accuracy"], report.metrics["kappa"]) == (0.8, 0.6429)


def test_labeled_examples_are_read_strictly() -> None:
    raw = {
        "id": "happy-1",
        "rubric": "The reply confirms the refund.",
        "customer_message": "Refund please.",
        "answer": "Refunded.",
        "human_label": "pass",
        "tool_calls": [{"ref": "tool_call:3", "text": "refund_payment(amount=40) -> HTTP 200 ok"}],
    }
    example = LabeledExample.from_json(raw)
    assert example.criterion == "rubric"
    assert example.request() == JudgeRequest(
        criterion="rubric",
        rubric="The reply confirms the refund.",
        customer_message="Refund please.",
        answer="Refunded.",
        tool_calls=(JudgeEvidence("tool_call:3", "refund_payment(amount=40) -> HTTP 200 ok"),),
    )
    for label in ("PASS", "maybe", None):
        with pytest.raises(ValueError, match="human_label must be pass or fail"):
            LabeledExample.from_json(raw | {"human_label": label})


def test_the_fake_judge_is_calibrated_like_any_other_judge() -> None:
    labeled = [
        LabeledExample(
            id=f"f{i}",
            criterion="task_completion",
            rubric="The reply confirms the refund of 40.00 USD.",
            customer_message="Refund please.",
            answer="I've refunded 40.00 USD." if i % 2 == 0 else "Our store opens at nine.",
            human_label="pass" if i % 2 == 0 else "fail",
        )
        for i in range(MIN_EXAMPLES)
    ]
    report = asyncio.run(
        calibrate(FakeJudge(), {"provider": "deterministic-fake"}, "task_completion", labeled)
    )
    assert report.metrics["accuracy"] == 1.0 and report.calibrated
    assert report.judge == {"provider": "deterministic-fake"}
