"""Semantic expectations graded by a judge (spec §16.3, §16.4, §49): PASS
needs the label ``pass`` and a score at the threshold; a judge that cannot
answer is an ERROR, never a score; the run's budget and the verdict cache
hold; and every result is one the simulation contract accepts."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from agenttwin_core.evaluators import EvaluationResult, ToolCall, case_verdict
from agenttwin_core.openapi_contract import Contract, contract_path
from agenttwin_evaluation.judges import (
    FakeJudge,
    JudgeError,
    JudgeRequest,
    JudgeVerdict,
    render_request,
)
from agenttwin_evaluation.semantic import (
    MAX_TOOL_EVIDENCE,
    JudgeBudget,
    Judged,
    SemanticJudging,
    judge_request,
)

CONTRACT = Contract.load(contract_path("simulation-service"))
SPEC: dict[str, Any] = {
    "type": "semantic",
    "id": "confirms-refund",
    "description": "The reply confirms the refund",
    "category": "task_completion",
    "rubric": "The reply confirms the refund of 40.00 USD.",
}
MESSAGE = "Please refund the 40 USD for ORD-1001."
ANSWER = "Done! I've refunded 40.00 USD for order ORD-1001."
CALLS = (
    ToolCall(
        seq=1,
        tool="lookup_order",
        arguments={"order_id": "ORD-1001"},
        http_status=200,
        status="ok",
        response={"order_id": "ORD-1001", "total": 40},
        risk="READ",
    ),
    ToolCall(
        seq=3,
        tool="refund_payment",
        arguments={"amount": 40, "order_id": "ORD-1001"},
        http_status=200,
        status="ok",
        response={"status": "ok"},
        risk="WRITE_IRREVERSIBLE",
        mutated=True,
    ),
)


def verdict(
    score: float = 0.9,
    label: str = "pass",
    *,
    evidence: Sequence[Mapping[str, str]] = (),
    cost: float | None = 0.002,
) -> JudgeVerdict:
    return JudgeVerdict(
        score=score,
        label=label,  # type: ignore[arg-type]
        confidence=0.8,
        reason="The reply confirms the refund.",
        evidence=tuple(evidence),
        input_tokens=500,
        output_tokens=80,
        cost_usd=cost,
    )


class Scripted:
    """A judge that answers from a script: a verdict, an error to raise, or a
    number of seconds to hang for."""

    provider = "anthropic"
    model = "claude-judge"
    kind = "llm"

    def __init__(self, *outcomes: JudgeVerdict | JudgeError | float) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[JudgeRequest] = []

    async def judge(self, request: JudgeRequest) -> JudgeVerdict:
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, JudgeError):
            raise outcome
        if isinstance(outcome, float):
            await asyncio.sleep(outcome)
            raise AssertionError("the timeout should have fired first")
        return outcome


class MemoryCache:
    def __init__(self) -> None:
        self.entries: dict[str, tuple[JudgeVerdict, dict[str, str]]] = {}

    async def get(self, key: str) -> JudgeVerdict | None:
        hit = self.entries.get(key)
        return hit[0] if hit else None

    async def put(self, key: str, verdict: JudgeVerdict, judge: Mapping[str, str]) -> None:
        self.entries[key] = (verdict, dict(judge))


def grade(
    judging: SemanticJudging,
    spec: Mapping[str, Any] = SPEC,
    *,
    answer: str | None = ANSWER,
    calls: Sequence[ToolCall] = CALLS,
    index: int = 0,
) -> Judged:
    judged = asyncio.run(judging.evaluate(spec, index, customer_message=MESSAGE, answer=answer, calls=calls))
    CONTRACT.check_schema("ExpectationResult", judged.result.to_json())
    return judged


@pytest.mark.parametrize(
    ("score", "label", "threshold", "status"),
    [
        (0.9, "pass", None, "PASS"),
        (0.7, "pass", None, "PASS"),  # the default threshold (0.7) is inclusive
        (0.69, "pass", None, "FAIL"),
        (0.95, "fail", None, "FAIL"),  # a high score does not overrule the label
        (0.8, "pass", 0.85, "FAIL"),
        (0.5, "pass", 0.5, "PASS"),
    ],
)
def test_the_label_and_the_threshold_decide_together(
    score: float, label: str, threshold: float | None, status: str
) -> None:
    spec = SPEC if threshold is None else SPEC | {"threshold": threshold}
    result = grade(SemanticJudging(Scripted(verdict(score, label))), spec).result
    assert result.status == status
    assert result.score == score
    assert result.label == (None if status == "PASS" else "SEMANTIC_FAIL")
    shown = 0.7 if threshold is None else threshold
    assert result.reason == (
        f"The reply confirms the refund. (label {label}, score {score:.2f}, "
        f"threshold {shown:.2f}, confidence 0.80)"
    )


def test_the_result_names_the_judge_and_what_it_quoted() -> None:
    quoted = verdict(
        evidence=[
            {"ref": "answer", "quote": "I've refunded 40.00 USD"},
            {"ref": "tool_call:3", "quote": "refund_payment"},
        ]
    )
    judged = grade(SemanticJudging(Scripted(quoted)))
    result = judged.result
    assert (result.evaluator, result.evaluator_version) == ("semantic.judge", "1.0.0")
    assert dict(result.expectation) == {
        "id": "confirms-refund",
        "type": "semantic",
        "critical": False,
        "description": "The reply confirms the refund",
        "category": "task_completion",
    }
    assert [e.to_json() for e in result.evidence] == [
        {"kind": "config", "detail": "Judged by anthropic claude-judge (judge-prompt/1, not calibrated)."},
        {"kind": "output", "detail": "“I've refunded 40.00 USD”"},
        {"kind": "tool_call", "detail": "“refund_payment”", "ref": "tool_call:3"},
    ]
    assert judged.verdict == quoted and not judged.cached and judged.cache_key
    assert judged.judge["calibrated"] == "false" and judged.judge["model"] == "claude-judge"

    calibrated = grade(SemanticJudging(Scripted(verdict()), calibrated=True))
    assert calibrated.result.evidence[0].detail.endswith("(judge-prompt/1, calibrated).")
    assert calibrated.judge["calibrated"] == "true"
    # An expectation without an id is named by its position, as the simulation names it.
    unnamed = {k: v for k, v in SPEC.items() if k != "id"} | {"critical": True}
    positional = grade(SemanticJudging(Scripted(verdict(0.2, "fail"))), unnamed, index=2).result
    assert positional.expectation["id"] == "e3" and positional.critical


@pytest.mark.parametrize(
    "kind", ["unavailable", "rate_limited", "timeout", "rejected", "malformed", "refused"]
)
def test_a_judge_that_cannot_answer_is_an_error_never_a_score(kind: str) -> None:
    cache = MemoryCache()
    judging = SemanticJudging(Scripted(JudgeError(kind, "the provider said no")), cache=cache)  # type: ignore[arg-type]
    result = grade(judging).result
    assert (result.status, result.label, result.score) == ("ERROR", "EVALUATION_ERROR", None)
    assert result.reason == f"The judge could not evaluate this expectation ({kind}): the provider said no"
    assert result.evidence[0].kind == "config"
    # The attempt counts against the budget, costs nothing known, and is not cached.
    assert (judging.budget.calls, judging.budget.spent_usd, cache.entries) == (1, 0.0, {})
    # A case whose semantic expectation could not be judged is not a pass.
    passing = EvaluationResult(status="PASS", reason="ok", evaluator="state", evaluator_version="1.0.0")
    assert case_verdict([passing, result]).status == "ERRORED"


def test_a_judge_that_hangs_is_an_error_after_the_timeout() -> None:
    judging = SemanticJudging(Scripted(5.0), timeout_s=0.01)
    result = grade(judging).result
    assert (result.status, result.label, result.score) == ("ERROR", "EVALUATION_ERROR", None)
    assert result.reason == "The judge could not evaluate this expectation (timeout)."
    assert judging.budget.calls == 1


def test_no_answer_fails_without_a_judge_call() -> None:
    for answer in (None, "", "  \n "):
        judge = Scripted()
        judging = SemanticJudging(judge)
        result = grade(judging, answer=answer).result
        assert (result.status, result.label, result.score) == ("FAIL", "SEMANTIC_FAIL", None)
        assert result.reason.startswith("The agent produced no answer")
        assert judge.requests == [] and judging.budget.calls == 0


def test_the_budget_stops_judging_and_says_so() -> None:
    judge = Scripted(verdict(cost=0.002), verdict(cost=0.002))
    judging = SemanticJudging(judge, budget=JudgeBudget(max_cost_usd=5.0, max_calls=2))
    specs = [SPEC | {"id": f"s{i}", "rubric": f"Rubric number {i}."} for i in range(3)]
    results = [grade(judging, s).result for s in specs]
    assert [r.status for r in results] == ["PASS", "PASS", "SKIPPED"]
    assert results[2].reason == ("Not judged: this run's judge budget is spent ($0.004 of $5, 2 of 2 calls).")
    assert len(judge.requests) == 2
    assert judging.budget.to_json() == {
        "max_cost_usd": 5.0,
        "max_calls": 2,
        "spent_usd": 0.004,
        "calls": 2,
        "unknown_cost_calls": 0,
        "exhausted": True,
    }


def test_the_cost_budget_stops_judging_once_spent() -> None:
    judge = Scripted(verdict(cost=0.002), verdict(cost=0.002), verdict(cost=0.002))
    judging = SemanticJudging(judge, budget=JudgeBudget(max_cost_usd=0.003, max_calls=100))
    statuses = [grade(judging, SPEC | {"rubric": f"Rubric {i}."}).result.status for i in range(3)]
    # 0.002 < 0.003 allows the second call; 0.004 stops the third.
    assert statuses == ["PASS", "PASS", "SKIPPED"] and len(judge.requests) == 2


def test_calls_without_a_known_cost_count_as_calls_only() -> None:
    judge = Scripted(verdict(cost=None), verdict(cost=None))
    budget = JudgeBudget(max_cost_usd=None, max_calls=1)
    judging = SemanticJudging(judge, budget=budget)
    first = grade(judging).result
    second = grade(judging, SPEC | {"rubric": "Another rubric."}).result
    assert (first.status, second.status) == ("PASS", "SKIPPED")
    assert second.reason == "Not judged: this run's judge budget is spent (1 of 1 calls)."
    assert (budget.spent_usd, budget.calls, budget.unknown_cost_calls) == (0.0, 1, 1)


def test_a_cached_verdict_is_reused_as_is_and_costs_nothing() -> None:
    cache = MemoryCache()
    judge = Scripted(verdict(0.9), verdict(0.3, "fail"))
    judging = SemanticJudging(judge, budget=JudgeBudget(max_calls=2), cache=cache)
    first = grade(judging)
    assert not first.cached and len(cache.entries) == 1
    stored, identity = cache.entries[first.cache_key or ""]
    assert stored == first.verdict and identity == judging.identity()

    again = grade(judging)
    assert again.cached and again.verdict == first.verdict
    assert again.result == first.result and len(judge.requests) == 1
    assert judging.budget.calls == 1

    # Anything the verdict depends on changes the key: a different answer is judged anew.
    other = grade(judging, answer="I can't refund this order.")
    assert not other.cached and other.result.status == "FAIL" and len(judge.requests) == 2
    # The budget is spent, yet a verdict already in the cache is still served.
    assert judging.budget.calls == 2
    assert grade(judging).cached and len(judge.requests) == 2


def test_a_judge_error_is_not_cached_and_is_retried_next_time() -> None:
    cache = MemoryCache()
    judge = Scripted(JudgeError("unavailable", "down"), verdict(0.9))
    judging = SemanticJudging(judge, cache=cache)
    assert grade(judging).result.status == "ERROR"
    assert cache.entries == {}
    retried = grade(judging)
    assert retried.result.status == "PASS" and not retried.cached and len(judge.requests) == 2


def test_an_expectation_for_another_judge_is_skipped() -> None:
    judge = Scripted(verdict(), verdict())
    judging = SemanticJudging(judge)
    skipped = grade(judging, SPEC | {"judge": "gpt-judge"}).result
    assert skipped.status == "SKIPPED"
    assert skipped.reason == (
        "This expectation asks for the judge 'gpt-judge'; the configured judge is anthropic claude-judge."
    )
    assert judge.requests == []
    # Named by provider or by model, the configured judge grades it.
    for wanted in ("anthropic", "claude-judge"):
        assert grade(judging, SPEC | {"judge": wanted}).result.status == "PASS"
    assert len(judge.requests) == 2


def test_the_judge_sees_the_calls_the_agent_made() -> None:
    request = judge_request(SPEC, MESSAGE, ANSWER, CALLS)
    assert request.criterion == "task_completion"
    assert [(e.ref, e.text) for e in request.tool_calls] == [
        (
            "tool_call:1",
            'lookup_order(order_id="ORD-1001") -> HTTP 200 ok; response: {"order_id":"ORD-1001","total":40}',
        ),
        (
            "tool_call:3",
            'refund_payment(amount=40, order_id="ORD-1001") -> HTTP 200 ok; response: {"status":"ok"}',
        ),
    ]
    # A criterion the judge does not know is graded against the rubric alone.
    assert judge_request(SPEC | {"category": "tone"}, MESSAGE, ANSWER, CALLS).criterion == "rubric"
    assert judge_request({"rubric": "x"}, MESSAGE, ANSWER, CALLS).criterion == "rubric"


def test_long_responses_and_long_runs_are_bounded_and_the_judge_is_told() -> None:
    big = ToolCall(
        seq=1, tool="search_kb", arguments={}, http_status=200, status="ok", response={"text": "x" * 1000}
    )
    text = judge_request(SPEC, MESSAGE, ANSWER, [big]).tool_calls[0].text
    assert text.endswith("…") and len(text.split("response: ", 1)[1]) == 300

    calls = [
        ToolCall(seq=i, tool="lookup_order", arguments={"n": i}, http_status=200, status="ok")
        for i in range(1, MAX_TOOL_EVIDENCE + 6)
    ]
    # Three network redeliveries among them: not the agent's calls, never shown.
    redelivered = [
        ToolCall(
            seq=100 + i, tool="lookup_order", arguments={}, http_status=200, status="ok", redelivered=True
        )
        for i in range(3)
    ]
    request = judge_request(SPEC, MESSAGE, ANSWER, redelivered + calls)
    assert [e.ref for e in request.tool_calls] == [f"tool_call:{i}" for i in range(1, MAX_TOOL_EVIDENCE + 1)]
    assert request.omitted_tool_calls == 5
    assert "(5 later tool call(s) not shown)" in render_request(request)
    whole = judge_request(SPEC, MESSAGE, ANSWER, calls[:MAX_TOOL_EVIDENCE])
    assert whole.omitted_tool_calls == 0 and "not shown" not in render_request(whole)
    # Omitting calls changes what the judge saw, so it changes the cache key.
    fake = FakeJudge()
    assert request.cache_key(fake) != JudgeRequest(**{**request.__dict__, "omitted_tool_calls": 0}).cache_key(
        fake
    )


def test_the_fake_judge_grades_the_demo_answers() -> None:
    """The demo's semantic expectations, graded as the demo run grades them."""
    happy = {
        "type": "semantic",
        "id": "confirms-refund-and-timing",
        "category": "task_completion",
        "rubric": (
            "The reply confirms the refund of 40.00 USD. "
            "The reply says the money reaches the original payment method."
        ),
    }
    judging = SemanticJudging(FakeJudge())
    answer = (
        "Done! I've refunded 40.00 USD for order ORD-1001. "
        "It will reach your original payment method within 5 business days."
    )
    assert grade(judging, happy, answer=answer, calls=()).result.status == "PASS"
    escalated = (
        "A refund of 250.00 USD needs a specialist's approval. "
        "I've passed it on and you'll hear from us within one business day."
    )
    missed = grade(judging, happy, answer=escalated, calls=()).result
    assert (missed.status, missed.label, missed.score) == ("FAIL", "SEMANTIC_FAIL", 0.0)
    over_limit = {
        "type": "semantic",
        "id": "explains-approval",
        "category": "escalation_appropriateness",
        "rubric": (
            "The reply tells the customer a specialist must approve the refund. "
            "The reply says when they will hear back."
        ),
    }
    assert grade(judging, over_limit, answer=escalated, calls=()).result.status == "PASS"
