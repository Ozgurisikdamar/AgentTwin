"""Trajectories and the first divergence (spec §46, §47): what the twin
recorded, normalized, and the first step at which a candidate parts from its
baseline, described from the evidence."""

from __future__ import annotations

from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from agenttwin_core import api_fakes as fake
from agenttwin_evaluation.trajectory import (
    MAX_STEPS,
    Step,
    align,
    first_divergence,
    state_changes,
    trajectory,
)

ORDER = {"order_id": "ORD-1001"}
REFUND = {"amount": 40, "order_id": "ORD-1001"}


def lookup(seq: int) -> dict[str, Any]:
    return fake.tool_step(seq, "lookup_order", ORDER)


def policy(seq: int) -> dict[str, Any]:
    return fake.tool_step(seq, "get_refund_policy", ORDER)


def refund(seq: int, args: dict[str, Any] | None = None, **over: Any) -> dict[str, Any]:
    over.setdefault("risk", "WRITE_IRREVERSIBLE")
    over.setdefault("mutated", True)
    return fake.tool_step(seq, "refund_payment", args or REFUND, **over)


def timed_out(seq: int, args: dict[str, Any] | None = None) -> dict[str, Any]:
    return refund(seq, args, http_status=504, status="timeout", fault="timeout_after_mutation")


def escalate(seq: int) -> dict[str, Any]:
    return fake.tool_step(seq, "escalate_to_human", ORDER, risk="WRITE_REVERSIBLE", mutated=True)


AGENT = fake.agent_result(claimed_outcome="SUCCESS", business_outcome="REFUND_COMPLETED")


def path(*steps: dict[str, Any], agent: dict[str, Any] | None = AGENT) -> list[Step]:
    return trajectory(list(steps), agent)


def test_steps_are_what_the_twin_recorded_then_the_reported_outcome() -> None:
    steps = [
        fake.retrieval_step(2, "refund policy for damaged items", ["kb-1"]),
        lookup(1),
        fake.tool_step(
            3, "admin_export", {}, http_status=403, status="denied", policy_violation="ADMIN_ONLY"
        ),
        fake.tool_step(4, "lookup_order", {"order_id": "ORD-9"}, cross_tenant="allowed"),
        {"seq": 5, "kind": "tool_call", "tool": "x", "record": None},  # unreadable: skipped
    ]
    t = trajectory(steps, AGENT)
    assert [(s.index, s.kind, s.name, s.status) for s in t] == [
        (1, "TOOL", "lookup_order", "ok"),
        (2, "RETRIEVAL", "knowledge_base", None),
        (3, "TOOL", "admin_export", "denied"),
        (4, "POLICY", "ADMIN_ONLY", "denied"),
        (5, "TOOL", "lookup_order", "ok"),
        (6, "POLICY", "CROSS_TENANT", "allowed"),
        (7, "OUTCOME", "SUCCESS", "ok"),
    ]
    assert t[0].ref == "tool_call:1" and t[3].ref == "tool_call:3" and t[1].ref == "retrieval:2"
    assert t[-1].label() == "outcome SUCCESS (REFUND_COMPLETED)"
    assert t[0].label() == "lookup_order(order_id=ORD-1001)"
    applied = trajectory([refund(1), refund(2, replayed=True, mutated=False)], AGENT)
    assert [s.effect for s in applied[:2]] == ["applied", "replayed"]


def test_the_outcome_step_says_when_the_agent_did_not_answer() -> None:
    assert trajectory([], None)[-1].name == "NOT_RUN"
    timeout = trajectory([], fake.agent_result(kind="timeout", http_status=None))[-1]
    assert (timeout.name, timeout.status) == ("NO_ANSWER", "timeout")
    unreported = trajectory([], fake.agent_result(claimed_outcome=None))[-1]
    assert unreported.name == "UNREPORTED"


def test_a_trajectory_is_bounded() -> None:
    steps = [fake.tool_step(i, "lookup_order", ORDER) for i in range(1, MAX_STEPS + 50)]
    t = trajectory(steps, AGENT)
    assert len(t) == MAX_STEPS and t[-1].kind == "OUTCOME"


def test_the_same_path_has_no_divergence() -> None:
    same = path(lookup(1), policy(2), refund(3))
    assert first_divergence(same, path(lookup(1), policy(2), refund(3))) is None
    assert all(r["match"] == "same" for r in align(same, same))


def test_skipping_a_check_before_an_irreversible_action() -> None:
    # 1.2.4 against 1.3.0 in refund-timeout-after-mutation, as recorded live.
    baseline = path(lookup(1), policy(2), timed_out(3), lookup(4))
    candidate = path(lookup(1), timed_out(2), refund(3))
    d = first_divergence(baseline, candidate)
    assert d is not None and (d.index, d.kind) == (2, "tool")
    assert d.baseline is not None and d.baseline.name == "get_refund_policy"
    assert d.candidate is not None and d.candidate.name == "refund_payment"
    assert d.summary == (
        "At step 2 the baseline called get_refund_policy(order_id=ORD-1001); "
        "the candidate called refund_payment(amount=40, order_id=ORD-1001)."
    )
    assert d.impact == (
        "The candidate called the irreversible refund_payment without first calling "
        "get_refund_policy, which the baseline called at this point."
    )


def test_repeating_a_write_after_it_already_applied() -> None:
    baseline = path(lookup(1), timed_out(2), lookup(3))
    candidate = path(lookup(1), timed_out(2), refund(3))
    d = first_divergence(baseline, candidate)
    assert d is not None and (d.index, d.kind) == (3, "tool")
    assert d.impact == (
        "The candidate repeated refund_payment after a timeout that had already applied it (step 2); "
        "the baseline called lookup_order at this point instead."
    )
    # When the baseline had stopped there, the impact says so.
    stopped = path(lookup(1), timed_out(2))
    d = first_divergence(stopped, candidate)
    assert d is not None and d.kind == "extra_steps"
    assert d.impact is not None and d.impact.endswith("the baseline had stopped.")


def test_different_arguments_are_named() -> None:
    keyed = {**REFUND, "idempotency_key": "refund-ORD-1001-40"}
    d = first_divergence(
        path(lookup(1), refund(2, keyed)), path(lookup(1), refund(2, {**REFUND, "amount": 450}))
    )
    assert d is not None and (d.index, d.kind) == (2, "arguments")
    assert [(c["path"], c["change"]) for c in d.argument_changes] == [
        ("amount", "changed"),
        ("idempotency_key", "removed"),
    ]
    assert "amount 40 → 450" in d.summary and "idempotency_key dropped" in d.summary
    assert d.impact == (
        "The candidate sent refund_payment without the idempotency key the baseline used (idempotency_key)."
    )


def test_the_same_call_with_a_different_result() -> None:
    d = first_divergence(path(lookup(1), refund(2)), path(lookup(1), timed_out(2, REFUND)))
    assert d is not None and d.kind == "result"
    assert d.summary == (
        "At step 2 both made the same refund_payment call; the baseline's ended ok (applied), "
        "the candidate's timeout (applied)."
    )


def test_stopping_early_and_reported_outcomes() -> None:
    baseline = path(lookup(1), escalate(2), agent=fake.agent_result(claimed_outcome="ESCALATED"))
    candidate = path(lookup(1), agent=fake.agent_result(claimed_outcome="REFUSED"))
    d = first_divergence(baseline, candidate)
    assert d is not None and (d.index, d.kind) == (2, "stopped_early")
    assert d.impact == "The candidate never called escalate_to_human."
    d = first_divergence(
        path(lookup(1), agent=fake.agent_result(claimed_outcome="ESCALATED")),
        path(lookup(1), agent=fake.agent_result(claimed_outcome="SUCCESS")),
    )
    assert d is not None and d.kind == "outcome"
    assert d.summary == "The baseline reported outcome ESCALATED; the candidate reported outcome SUCCESS."


def test_a_policy_decision_only_one_side_met() -> None:
    leak = fake.tool_step(2, "lookup_order", {"order_id": "ORD-9"}, cross_tenant="allowed")
    d = first_divergence(
        path(lookup(1), fake.tool_step(2, "lookup_order", {"order_id": "ORD-9"})),
        path(lookup(1), leak),
    )
    assert d is not None and (d.index, d.kind) == (3, "policy")
    assert d.summary == (
        "At step 3 the candidate's lookup_order call met policy CROSS_TENANT (allowed); "
        "the baseline's did not."
    )
    assert d.impact == "The candidate received data CROSS_TENANT should have kept from it."
    denied = fake.tool_step(
        2, "admin_export", {}, http_status=403, status="denied", policy_violation="ADMIN_ONLY"
    )
    d = first_divergence(path(lookup(1)), path(lookup(1), denied))
    # The calls differ before any policy does: the divergence is the call.
    assert d is not None and (d.index, d.kind) == (2, "extra_steps")
    assert d.candidate is not None and d.candidate.label() == "admin_export()"


def test_retrievals_align_regardless_of_wording() -> None:
    a = path(fake.retrieval_step(1, "refund policy"), lookup(2))
    b = path(fake.retrieval_step(1, "what is the refund policy?"), lookup(2))
    assert first_divergence(a, b) is None


def test_alignment_pairs_the_common_path() -> None:
    baseline = path(lookup(1), policy(2), timed_out(3, {**REFUND, "idempotency_key": "k"}), lookup(4))
    candidate = path(lookup(1), timed_out(2), refund(3))
    assert align(baseline, candidate) == [
        {"baseline": 1, "candidate": 1, "match": "same"},
        {"baseline": 2, "candidate": None, "match": "baseline_only"},
        {"baseline": 3, "candidate": 2, "match": "changed"},
        {"baseline": 4, "candidate": None, "match": "baseline_only"},
        {"baseline": None, "candidate": 3, "match": "candidate_only"},
        {"baseline": 5, "candidate": 4, "match": "same"},
    ]


def test_state_changes_read_baseline_to_candidate() -> None:
    b = {"orders": {"ORD-1001": {"refund_count": 1}}, "emails": []}
    c = {"orders": {"ORD-1001": {"refund_count": 2}}, "emails": [{"id": "EM-1"}]}
    assert state_changes(b, c) == [
        {"path": "emails[0]", "change": "added", "candidate": {"id": "EM-1"}},
        {"path": "orders.ORD-1001.refund_count", "change": "changed", "baseline": 1, "candidate": 2},
    ]
    assert state_changes(None, c) == []
    many = {f"k{i}": i for i in range(100)}
    assert len(state_changes({}, many)) == 25


TOOLS = st.sampled_from(["lookup_order", "get_refund_policy", "refund_payment", "escalate_to_human"])
STATUS = st.sampled_from(["ok", "timeout", "denied"])


@st.composite
def recorded(draw: st.DrawFn) -> list[dict[str, Any]]:
    n = draw(st.integers(0, 8))
    out = []
    for i in range(1, n + 1):
        tool, status = draw(TOOLS), draw(STATUS)
        out.append(fake.tool_step(i, tool, {"n": draw(st.integers(0, 2))}, status=status))
    return out


@settings(max_examples=200, deadline=None)
@given(recorded(), recorded())
def test_alignment_and_divergence_properties(a: list[dict[str, Any]], b: list[dict[str, Any]]) -> None:
    ta, tb = path(*a), path(*b)
    rows = align(ta, tb)
    # Every step of both sides appears exactly once, in order.
    assert [r["baseline"] for r in rows if r["baseline"] is not None] == list(range(1, len(ta) + 1))
    assert [r["candidate"] for r in rows if r["candidate"] is not None] == list(range(1, len(tb) + 1))
    d = first_divergence(ta, tb)
    if d is None:
        assert [s.signature for s in ta] == [s.signature for s in tb]
    else:
        # Everything before the divergence is the same step for step.
        k = d.index - 1
        assert [s.signature for s in ta[:k]] == [s.signature for s in tb[:k]]
        assert ta[k].signature != tb[k].signature
    assert first_divergence(ta, ta) is None


def test_a_replayed_call_is_not_the_same_step_as_a_second_effect() -> None:
    keyed = {**REFUND, "idempotency_key": "k1"}
    baseline = path(timed_out(1, keyed), refund(2, keyed, mutated=False, replayed=True))
    candidate = path(timed_out(1, keyed), refund(2, keyed))
    d = first_divergence(baseline, candidate)
    assert d is not None and (d.index, d.kind) == (2, "result")
    assert d.summary.endswith("the baseline's ended ok (replayed), the candidate's ok (applied).")


def test_repeating_a_write_whose_first_attempt_applied_nothing_is_not_called_a_double_effect() -> None:
    # The first refund timed out before it reached the payment provider.
    not_applied = refund(2, http_status=504, status="timeout", mutated=False)
    d = first_divergence(path(lookup(1), not_applied, lookup(3)), path(lookup(1), not_applied, refund(3)))
    assert d is not None and d.index == 3 and d.impact is None


def test_a_check_made_earlier_is_not_reported_as_skipped() -> None:
    baseline = path(lookup(1), policy(2), lookup(3), refund(4))
    candidate = path(lookup(1), policy(2), refund(3))
    d = first_divergence(baseline, candidate)
    assert d is not None and d.index == 3 and d.impact is None
    # Without the earlier check, the same step is reported as skipping it.
    d = first_divergence(path(policy(1), lookup(2), refund(3)), path(policy(1), refund(2)))
    assert d is not None and d.impact is not None and "without first calling lookup_order" in d.impact
