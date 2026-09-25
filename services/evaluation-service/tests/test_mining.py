"""The regression miner's rules (spec §18, §44, §121; ADR-0032): which
production traces are candidates and why, their features, the suggested
label and severity with evidence, the fingerprint of a known failure, the
grouping decision and the lifecycle. Real traces of the demo stack are used
where they exist (tests/data/regressions)."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from agenttwin_evaluation import mining
from agenttwin_evaluation.mining import (
    InvalidTransition,
    Observation,
    Suggestion,
    at_or_after,
    choose_group,
    detect,
    feature_text,
    features,
    fingerprint,
    observation_from_event,
    observation_from_trace,
    on_occurrence,
    suggest_severity,
    suggest_taxonomy,
    title,
    transition,
    violation_kinds,
    with_flag,
)

DATA = Path(__file__).parent / "data" / "regressions"


def load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((DATA / f"{name}.json").read_text())
    return data


def risks(detail: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    return tuple(
        sorted({(s["tool_name"], s["tool_risk"]) for s in detail["spans"] if s.get("kind") == "tool"})
    )


def real(name: str) -> Observation:
    d = load(name)
    return replace(observation_from_trace(d["trace"]), tool_risks=risks(d))


def event_of(name: str) -> dict[str, Any]:
    """A trace.ingested.v1 payload for a real trace, as the trace service
    would send it."""
    d = load(name)
    t = d["trace"]
    counts: dict[str, int] = {}
    for s in d["spans"]:
        if s.get("kind") == "tool":
            counts[s["tool_name"]] = counts.get(s["tool_name"], 0) + 1
    return {
        "trace_id": t["trace_id"],
        "agent": t["agent_name"],
        "agent_version": t["agent_version"],
        "environment": t["environment"],
        "started_at": t["started_at"],
        "source": t["source"],
        "signals": t["signals"],
        "summary": t["summary"],
        "observed_tools": [
            {"name": n, "risk": r, "http_host": None, "count": counts[n]} for n, r in risks(d)
        ],
    }


def obs(**kw: Any) -> Observation:
    base: dict[str, Any] = {"trace_id": "a" * 32, "agent": "support-refund-agent", "source": "production"}
    base.update(kw)
    return Observation(**base)


# ---------------------------------------------------------------- reading


def test_an_ingestion_event_and_the_trace_row_describe_the_same_trace() -> None:
    from_event = observation_from_event(event_of("duplicate-refund"))
    from_row = real("duplicate-refund")
    assert from_event.trace_id == "f310c0de303dcad73b1f5f0b10509536"
    assert from_event.agent == "support-refund-agent"
    assert from_event.agent_version == "1.3.0"
    assert from_event.tool_risks == from_row.tool_risks
    assert from_event.risk_of("refund_payment") == "WRITE_IRREVERSIBLE"
    assert from_event.risk_of(None) is None
    assert from_event.failing_tool == "refund_payment"
    assert from_event.error_type == "timeout"
    assert from_event.retry_count == 1
    assert from_event.step_count == 9
    assert from_event.contradiction and from_row.contradiction
    assert from_event.violations == ("retry_without_idempotency_key", "duplicate_irreversible_action")
    assert from_event.tool_sequence_sketch == "lookup_order>refund_payment x2>send_email"
    assert from_event.production and from_row.production
    assert features(from_event) == features(from_row)


def test_a_trace_row_keeps_the_risks_and_flags_already_known() -> None:
    first = with_flag(
        observation_from_event(event_of("duplicate-refund")), "incident", "Customer refunded twice"
    )
    later = observation_from_trace(load("duplicate-refund")["trace"], previous=first)
    assert later.tool_risks == first.tool_risks
    assert later.flags == (("incident", "Customer refunded twice"),)
    assert observation_from_trace(load("duplicate-refund")["trace"]).tool_risks == ()


def test_the_recorded_outcome_wins_over_the_summary() -> None:
    row = dict(load("timeout-handled")["trace"])
    row["outcome_status"] = "FAILURE"
    row["outcome_verified"] = True
    o = observation_from_trace(row)
    assert o.outcome == "FAILURE" and o.outcome_verified is True
    row["outcome_status"] = None
    row["outcome_verified"] = None
    o = observation_from_trace(row)
    assert o.outcome == row["summary"]["outcome"]


def test_unknown_cost_is_not_zero() -> None:
    e = event_of("duplicate-refund")
    assert e["summary"]["cost_known"] is False
    assert observation_from_event(e).cost_usd is None
    e["summary"] = {**e["summary"], "cost_known": True, "cost_usd": 0.25}
    assert observation_from_event(e).cost_usd == 0.25


def test_malformed_fields_are_ignored_not_trusted() -> None:
    o = observation_from_event(
        {
            "trace_id": "b" * 32,
            "agent": "a",
            "signals": ["retry", 3, None, "retry"],
            "summary": {"retry_count": True, "step_count": -2, "cost_usd": "1", "tools": "x", "outcome": ""},
            "observed_tools": [{"name": "t"}, "junk", {"name": "u", "risk": "READ"}],
        }
    )
    assert o.signals == ("retry",)
    assert o.retry_count == 0 and o.step_count == 0
    assert o.cost_usd is None and o.tools == () and o.outcome is None
    assert o.tool_risks == (("u", "READ"),)


def test_flags_are_kept_once_and_bounded() -> None:
    o = with_flag(obs(), "incident", "  refunded twice  ")
    assert with_flag(o, "incident", "refunded twice") is o
    o = with_flag(o, "weird", "x" * 900)
    assert o.flags[0] == ("incident", "refunded twice")
    assert o.flags[1][0] == "manual" and len(o.flags[1][1]) == 500


# ---------------------------------------------------------------- detection


def test_real_production_failures_are_candidates_with_reasons() -> None:
    assert detect(real("duplicate-refund")) == [
        "the outcome was FAILURE",
        "it claimed a success the verified outcome disproved",
        "it took an irreversible action twice",
        "it retried a write without an idempotency key",
        "a tool timed out after it may have changed state",
        "a tool failed (refund_payment)",
    ]
    assert detect(real("cross-tenant-denied")) == ["the outcome was FAILURE", "a tool failed (lookup_order)"]
    assert "it repeated the same step in a loop" in detect(real("duplicate-refund-loop"))
    assert detect(real("timeout-handled")) == [
        "a tool timed out after it may have changed state",
        "a tool failed (refund_payment)",
    ]


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({"outcome": "FAILURE"}, "the outcome was FAILURE"),
        ({"outcome": "PARTIAL"}, "the outcome was PARTIAL"),
        ({"signals": ("outcome_failure",)}, "the outcome was a failure"),
        ({"contradiction": True}, "it claimed a success the verified outcome disproved"),
        ({"signals": ("duplicate_side_effect",)}, "it took an irreversible action twice"),
        ({"violations": ("duplicate_irreversible_action",)}, "it took an irreversible action twice"),
        ({"violations": ("retry_without_idempotency_key",)}, "it retried a write without an idempotency key"),
        ({"signals": ("timeout_after_mutation",)}, "a tool timed out after it may have changed state"),
        ({"signals": ("loop_detected",)}, "it repeated the same step in a loop"),
        ({"signals": ("policy_denied",)}, "a policy denied one of its actions"),
        ({"violations": ("policy_denied:refund_payment",)}, "a policy denied one of its actions"),
        ({"signals": ("tool_error",)}, "a tool failed"),
        (
            {"signals": ("tool_error",), "errors": ("b:timeout", "a:denied", "model:x")},
            "a tool failed (a, b)",
        ),
        ({"signals": ("model_error",)}, "a model call failed"),
        ({"flags": (("negative_feedback", "rude"),)}, "flagged as negative feedback: rude"),
        ({"flags": (("manual", ""),)}, "flagged as manual"),
    ],
)
def test_each_signal_makes_a_candidate(kw: dict[str, Any], reason: str) -> None:
    assert detect(obs(**kw)) == [reason]


def test_an_unverified_outcome_alone_is_not_a_failure() -> None:
    assert detect(obs(signals=("outcome_unverified", "retry", "error"), outcome="SUCCESS")) == []
    assert detect(obs(outcome="SUCCESS", outcome_verified=True)) == []


def test_only_production_traces_are_mined() -> None:
    failing = {"outcome": "FAILURE", "signals": ("duplicate_side_effect",)}
    assert detect(obs(source="simulation", **failing)) == []
    assert detect(obs(simulation_run_id="r1", **failing)) == []
    assert detect(obs(source=None, **failing)) == []
    assert detect(obs(**failing)) != []


def test_violation_kinds_drop_what_they_were_about() -> None:
    assert violation_kinds(["policy_denied:refund_payment", "policy_denied:x", "loop", ""]) == (
        "loop",
        "policy_denied",
    )


# ---------------------------------------------------------------- features


def test_features_hold_no_content() -> None:
    o = real("duplicate-refund")
    f = features(o)
    assert f["failing_tool"] == "refund_payment"
    assert f["failing_tool_risk"] == "WRITE_IRREVERSIBLE"
    assert f["tool_errors"] == ["refund_payment:timeout"]
    assert f["violations"] == ["duplicate_irreversible_action", "retry_without_idempotency_key"]
    assert f["version"] == "1.3.0" and f["contradiction"] is True and f["retry_count"] == 1
    assert set(f) == {
        "agent", "version", "environment", "outcome", "outcome_verified", "contradiction",
        "last_successful_step", "failing_tool", "failing_tool_risk", "error_type", "tool_errors",
        "retry_count", "step_count", "violations", "policy_decisions", "signals",
        "tool_sequence_sketch", "prompt_hash", "model", "cost_usd", "duration_ms", "flags",
    }  # fmt: skip
    text = json.dumps(f) + feature_text(f, suggest_taxonomy(o).all)
    # Nothing the customer wrote, and no production identifiers.
    for secret in ("ORD-3036", "arrived broken", "$40", "CUS-100"):
        assert secret not in text


def test_the_embedded_sentence_leaves_the_version_out() -> None:
    o = real("duplicate-refund")
    t = suggest_taxonomy(o).all
    same = feature_text(features(replace(o, agent_version="9.9.9")), t)
    assert feature_text(features(o), t) == same
    assert "1.3.0" not in same
    assert same.startswith("agent support-refund-agent; failure duplicate side effect hallucinated success")
    assert "tool refund_payment failed with timeout" in same
    assert "took an irreversible action twice" in same
    assert "retried a write without an idempotency key" in same
    assert "claimed success that the final state disproved" in same
    assert "tools lookup_order then refund_payment x2 then send_email" in same


def test_the_sentence_names_flags_and_unknown_errors() -> None:
    f = features(obs(failing_tool="t", flags=(("incident", "x"),), violations=("odd_thing:x",)))
    s = feature_text(f, ())
    assert "tool t failed with an error" in s
    assert "odd thing" in s
    assert "flagged incident" in s
    assert "failure" not in s


# ---------------------------------------------------------------- taxonomy


def test_real_failures_are_labelled_with_evidence() -> None:
    dup = suggest_taxonomy(real("duplicate-refund"))
    assert dup == Suggestion(
        "DUPLICATE_SIDE_EFFECT",
        ("HALLUCINATED_SUCCESS", "RETRY_SAFETY", "TIMEOUT"),
        (
            "DUPLICATE_SIDE_EFFECT: refund_payment took effect more than once",
            "HALLUCINATED_SUCCESS: the agent claimed success and the verified outcome disproved it",
            "RETRY_SAFETY: a write was retried without an idempotency key",
            "TIMEOUT: refund_payment timed out",
        ),
    )
    assert suggest_taxonomy(real("duplicate-refund-loop")).all == (
        "DUPLICATE_SIDE_EFFECT",
        "HALLUCINATED_SUCCESS",
        "RETRY_SAFETY",
        "LOOP",
        "TIMEOUT",
    )
    denied = suggest_taxonomy(real("cross-tenant-denied"))
    assert denied.all == ("AUTHORIZATION",)
    assert denied.evidence == ("AUTHORIZATION: access was denied to lookup_order",)
    assert suggest_taxonomy(real("timeout-handled")).all == ("TIMEOUT",)


@pytest.mark.parametrize(
    ("kw", "labels"),
    [
        ({"signals": ("policy_denied",)}, ("POLICY_VIOLATION",)),
        ({"violations": ("policy_denied:refund_payment",)}, ("POLICY_VIOLATION",)),
        ({"signals": ("loop_detected",)}, ("LOOP",)),
        ({"error_type": "TIMEOUT", "failing_tool": "t"}, ("TIMEOUT",)),
        ({"signals": ("timeout_after_mutation",)}, ("TIMEOUT",)),
        ({"errors": ("t:invalid_argument",)}, ("TOOL_ERROR_HANDLING",)),
        ({"signals": ("tool_error",)}, ("TOOL_ERROR_HANDLING",)),
        (
            {"errors": ("t:forbidden", "u:timeout", "v:boom")},
            ("AUTHORIZATION", "TIMEOUT", "TOOL_ERROR_HANDLING"),
        ),
        ({"outcome": "FAILURE", "outcome_verified": True}, ("STATE_MISMATCH",)),
        ({"outcome": "PARTIAL", "outcome_verified": True}, ("STATE_MISMATCH",)),
        ({"outcome": "FAILURE", "outcome_verified": False}, ("UNKNOWN",)),
        ({"outcome": "FAILURE"}, ("UNKNOWN",)),
        ({}, ("UNKNOWN",)),
    ],
)
def test_each_rule(kw: dict[str, Any], labels: tuple[str, ...]) -> None:
    assert suggest_taxonomy(obs(**kw)).all == labels


def test_a_tool_error_that_is_a_denial_or_timeout_is_not_also_generic() -> None:
    assert suggest_taxonomy(obs(signals=("tool_error",), errors=("t:denied",))).all == ("AUTHORIZATION",)
    assert suggest_taxonomy(obs(signals=("tool_error",), errors=("t:timeout",))).all == ("TIMEOUT",)


def test_the_duplicated_tool_is_named() -> None:
    o = obs(
        signals=("duplicate_side_effect",),
        tools=("lookup", "refund", "refund", "email"),
        tool_risks=(("email", "WRITE_REVERSIBLE"), ("lookup", "READ"), ("refund", "WRITE_IRREVERSIBLE")),
        failing_tool="lookup",
    )
    s = suggest_taxonomy(o)
    assert s.evidence[0] == "DUPLICATE_SIDE_EFFECT: refund took effect more than once"
    assert title(o, s) == "refund took effect twice"
    unknown = obs(signals=("duplicate_side_effect",))
    assert suggest_taxonomy(unknown).evidence[0] == (
        "DUPLICATE_SIDE_EFFECT: an irreversible action took effect more than once"
    )
    assert title(unknown, suggest_taxonomy(unknown)) == "a tool took effect twice"


def test_the_duplicated_tool_is_the_failing_one_or_the_most_repeated() -> None:
    risky = (("charge", "WRITE_IRREVERSIBLE"), ("lookup", "READ"), ("refund", "WRITE_IRREVERSIBLE"))
    failing = obs(
        signals=("duplicate_side_effect",),
        tools=("charge", "charge", "refund"),
        tool_risks=risky,
        failing_tool="refund",
    )
    assert suggest_taxonomy(failing).evidence[0].startswith("DUPLICATE_SIDE_EFFECT: refund ")
    most = obs(
        signals=("duplicate_side_effect",),
        tools=("refund", "refund", "charge", "charge", "charge"),
        tool_risks=risky,
        failing_tool="lookup",
    )
    assert suggest_taxonomy(most).evidence[0].startswith("DUPLICATE_SIDE_EFFECT: charge ")
    once = obs(
        signals=("duplicate_side_effect",),
        tools=("refund", "charge"),
        tool_risks=risky,
        failing_tool="lookup",
    )
    assert suggest_taxonomy(once).evidence[0].startswith("DUPLICATE_SIDE_EFFECT: lookup ")


def test_tool_errors_are_compared_without_case() -> None:
    o = obs(errors=("t:TIMEOUT", "u:Denied"))
    assert suggest_taxonomy(o).all == ("AUTHORIZATION", "TIMEOUT")
    assert features(o)["tool_errors"] == ["t:timeout", "u:denied"]


def test_titles() -> None:
    assert title(real("duplicate-refund"), suggest_taxonomy(real("duplicate-refund"))) == (
        "refund_payment took effect twice"
    )
    assert title(real("cross-tenant-denied"), suggest_taxonomy(real("cross-tenant-denied"))) == (
        "Access to lookup_order was denied"
    )
    assert (
        title(real("timeout-handled"), suggest_taxonomy(real("timeout-handled")))
        == "refund_payment timed out"
    )
    assert title(obs(), Suggestion("UNKNOWN")) == "Unexplained failure"
    assert title(obs(), Suggestion("DATA_LEAKAGE")) == "Data leakage"
    assert title(obs(failing_tool="t"), Suggestion("LOOP")) == "Looped on t"


# ---------------------------------------------------------------- severity


def test_severity_follows_the_worst_label() -> None:
    assert suggest_severity(real("duplicate-refund"), suggest_taxonomy(real("duplicate-refund"))) == (
        "critical",
        "DUPLICATE_SIDE_EFFECT is critical",
    )
    denied = real("cross-tenant-denied")
    assert suggest_severity(denied, suggest_taxonomy(denied)) == ("high", "AUTHORIZATION is high")
    handled = real("timeout-handled")
    assert suggest_severity(handled, suggest_taxonomy(handled)) == ("medium", "TIMEOUT is medium")
    assert suggest_severity(obs(), Suggestion("UNKNOWN")) == ("low", "no rule raised it")
    assert suggest_severity(obs(), Suggestion("LOOP", ("POLICY_VIOLATION",)))[0] == "high"


def test_a_misreported_success_around_an_irreversible_action_is_critical() -> None:
    s = Suggestion("HALLUCINATED_SUCCESS")
    assert suggest_severity(obs(), s) == ("high", "HALLUCINATED_SUCCESS is high")
    risky = obs(tool_risks=(("refund", "WRITE_IRREVERSIBLE"),))
    assert suggest_severity(risky, s) == (
        "critical",
        "a success was misreported around an irreversible action",
    )
    assert suggest_severity(risky, Suggestion("LOOP"))[0] == "medium"


def test_flags_raise_severity_but_never_lower_it() -> None:
    incident = obs(flags=(("incident", "x"),))
    assert suggest_severity(incident, Suggestion("UNKNOWN")) == ("high", "flagged as an incident")
    assert suggest_severity(incident, Suggestion("DUPLICATE_SIDE_EFFECT"))[0] == "critical"
    feedback = obs(flags=(("negative_feedback", "x"),))
    assert suggest_severity(feedback, Suggestion("UNKNOWN")) == ("medium", "flagged with negative feedback")
    assert suggest_severity(feedback, Suggestion("AUTHORIZATION"))[0] == "high"
    assert suggest_severity(obs(flags=(("manual", "x"),)), Suggestion("UNKNOWN"))[0] == "low"


# ---------------------------------------------------------------- fingerprint


def test_the_same_failure_twice_has_one_fingerprint() -> None:
    a, b = real("duplicate-refund"), real("duplicate-refund-loop")
    assert fingerprint(a, suggest_taxonomy(a)) == fingerprint(b, suggest_taxonomy(b))
    assert len(fingerprint(a, suggest_taxonomy(a))) == 32
    # Versions and arguments do not make a different failure.
    assert fingerprint(
        replace(a, agent_version="1.2.4", trace_id="c" * 32), suggest_taxonomy(a)
    ) == fingerprint(a, suggest_taxonomy(a))
    assert fingerprint(replace(a, violations=("policy_denied:x",)), suggest_taxonomy(a)) == fingerprint(
        replace(a, violations=("policy_denied:y",)), suggest_taxonomy(a)
    )


@pytest.mark.parametrize(
    "change",
    [
        {"agent": "other"},
        {"failing_tool": "send_email"},
        {"error_type": "denied"},
        {"violations": ("duplicate_irreversible_action",)},
    ],
)
def test_a_different_failure_has_another_fingerprint(change: dict[str, Any]) -> None:
    a = real("duplicate-refund")
    s = suggest_taxonomy(a)
    assert fingerprint(replace(a, **change), s) != fingerprint(a, s)
    assert fingerprint(a, Suggestion("TIMEOUT")) != fingerprint(a, s)


def test_the_error_type_is_compared_without_case() -> None:
    a = real("duplicate-refund")
    s = suggest_taxonomy(a)
    assert fingerprint(replace(a, error_type="TIMEOUT"), s) == fingerprint(a, s)


# ---------------------------------------------------------------- grouping


def test_a_known_failure_joins_its_group_first() -> None:
    c = choose_group(exact_group="g1", nearest_group="g2", nearest_similarity=0.99, threshold=0.85)
    assert (c.kind, c.group_id, c.similarity) == ("exact", "g1", None)
    assert c.reason == "the same failure (same fingerprint) is known"


def test_a_close_failure_joins_its_nearest_neighbour() -> None:
    c = choose_group(exact_group=None, nearest_group="g2", nearest_similarity=0.85, threshold=0.85)
    assert (c.kind, c.group_id, c.similarity) == ("similar", "g2", 0.85)
    assert c.reason == "close to a known failure (similarity 0.85 ≥ 0.85)"


def test_nothing_is_forced_into_a_cluster() -> None:
    c = choose_group(exact_group=None, nearest_group="g2", nearest_similarity=0.8499, threshold=0.85)
    assert (c.kind, c.group_id, c.similarity) == ("new", None, 0.8499)
    assert c.reason == "no known failure is close enough (nearest 0.85 < 0.85)"
    first = choose_group(exact_group=None, nearest_group=None, nearest_similarity=None, threshold=0.85)
    assert (first.kind, first.group_id, first.reason) == ("new", None, "the first failure of its kind")
    no_group = choose_group(exact_group=None, nearest_group=None, nearest_similarity=0.99, threshold=0.85)
    assert no_group.kind == "new"


# ---------------------------------------------------------------- lifecycle


@pytest.mark.parametrize(
    ("status", "action", "has_case", "to"),
    [
        ("CANDIDATE", "confirm", False, "CONFIRMED"),
        ("REOPENED", "confirm", True, "CONFIRMED"),
        ("CANDIDATE", "dismiss", False, "DISMISSED"),
        ("CONFIRMED", "dismiss", False, "DISMISSED"),
        ("REOPENED", "dismiss", True, "DISMISSED"),
        ("DISMISSED", "reopen", False, "REOPENED"),
        ("FIXED", "reopen", True, "REOPENED"),
        ("CANDIDATE", "promote", False, "PROMOTED"),
        ("CONFIRMED", "promote", False, "PROMOTED"),
        ("REOPENED", "promote", False, "PROMOTED"),
        ("PROMOTED", "fixed", True, "FIXED"),
        ("REOPENED", "fixed", True, "FIXED"),
    ],
)
def test_allowed_transitions(status: str, action: mining.Action, has_case: bool, to: str) -> None:
    assert transition(status, action, has_case=has_case) == to


@pytest.mark.parametrize(
    ("status", "action", "has_case", "message"),
    [
        ("PROMOTED", "confirm", True, "A promoted regression cannot be confirmed."),
        ("DISMISSED", "dismiss", False, "A dismissed regression cannot be dismissed."),
        ("PROMOTED", "dismiss", True, "A promoted regression cannot be dismissed."),
        ("CANDIDATE", "reopen", False, "A candidate regression cannot be reopened."),
        ("PROMOTED", "promote", True, "A promoted regression cannot be promoted."),
        ("FIXED", "promote", True, "A fixed regression cannot be promoted."),
        (
            "REOPENED",
            "promote",
            True,
            "This regression already has a test; it is fixed when a version passes it.",
        ),
        ("CANDIDATE", "fixed", False, "A candidate regression cannot be fixed."),
        ("REOPENED", "fixed", False, "Only a promoted regression can be fixed: promote it first."),
    ],
)
def test_refused_transitions(status: str, action: mining.Action, has_case: bool, message: str) -> None:
    with pytest.raises(InvalidTransition) as err:
        transition(status, action, has_case=has_case)
    assert str(err.value) == message


def test_an_unknown_action_is_refused() -> None:
    with pytest.raises(InvalidTransition, match="Unknown action 'merge'"):
        transition("CANDIDATE", "merge")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("version", "fixed", "after"),
    [
        ("1.3.1", "1.3.1", True),
        ("1.3.2", "1.3.1", True),
        ("1.3.0", "1.3.1", False),
        ("1.10.0", "1.9.0", True),
        ("1.4", "1.4.0", True),
        ("1.4.0", "1.4", True),
        ("v1.4.1", "1.4.0", True),
        ("1.4.0-rc1", "1.4.0", False),
        ("1.4.0", "1.4.0-rc1", True),
        ("1.4.0-rc2", "1.4.0-rc1", True),
        ("1.4.0-rc1", "1.4.0-rc2", False),
        ("1.4.0-rc10", "1.4.0-rc9", True),
        ("1.4.0-beta", "1.4.0-alpha", True),
        ("main", "1.0.0", True),
        ("1.0.0", "latest", True),
        (None, "1.0.0", True),
        ("1.0.0", None, True),
    ],
)
def test_versions_at_or_after_the_fix(version: str | None, fixed: str | None, after: bool) -> None:
    assert at_or_after(version, fixed) is after


def test_a_fixed_regression_that_fails_again_is_reopened() -> None:
    e = on_occurrence("FIXED", "1.3.2", "1.3.1")
    assert (e.status, e.reopened) == ("REOPENED", True)
    assert e.reason == "failed again in 1.3.2 after the fix in 1.3.1"
    assert (
        on_occurrence("FIXED", None, "1.3.1").reason
        == "failed again in an unknown version after the fix in 1.3.1"
    )


def test_an_older_version_failing_does_not_reopen_the_fix() -> None:
    e = on_occurrence("FIXED", "1.3.0", "1.3.1")
    assert (e.status, e.reopened, e.reason) == ("FIXED", False, None)


@pytest.mark.parametrize("status", ["CANDIDATE", "CONFIRMED", "PROMOTED", "DISMISSED", "REOPENED"])
def test_other_statuses_keep_counting(status: str) -> None:
    assert on_occurrence(status, "9.9.9", "1.0.0").status == status


# ---------------------------------------------------------------- properties


signal = st.sampled_from(
    [
        "outcome_failure", "duplicate_side_effect", "timeout_after_mutation", "loop_detected",
        "policy_denied", "tool_error", "model_error", "retry", "error", "outcome_unverified", "contradiction",
    ]
)  # fmt: skip
observations = st.builds(
    obs,
    signals=st.lists(signal, max_size=6).map(lambda s: tuple(sorted(set(s)))),
    errors=st.lists(st.sampled_from(["t:timeout", "t:denied", "u:bad", "model:x"]), max_size=3).map(tuple),
    violations=st.lists(
        st.sampled_from(
            ["duplicate_irreversible_action", "retry_without_idempotency_key", "policy_denied:t"]
        ),
        max_size=3,
    ).map(tuple),
    outcome=st.sampled_from([None, "SUCCESS", "FAILURE", "PARTIAL", "UNKNOWN"]),
    outcome_verified=st.sampled_from([None, True, False]),
    contradiction=st.booleans(),
    failing_tool=st.sampled_from([None, "t", "u"]),
    error_type=st.sampled_from([None, "timeout", "denied", "bad"]),
    tool_risks=st.sampled_from([(), (("t", "WRITE_IRREVERSIBLE"),), (("t", "READ"),)]),
    source=st.sampled_from(["production", "simulation"]),
)


@given(observations)
@settings(max_examples=400, deadline=None)
def test_properties(o: Observation) -> None:
    why = detect(o)
    if o.source != "production":
        assert why == []
    s = suggest_taxonomy(o)
    assert s.primary in mining.TAXONOMY
    assert all(label in mining.TAXONOMY for label in s.secondary)
    assert len(set(s.all)) == len(s.all)
    assert len(s.evidence) == len(s.all)
    assert all(e.startswith(label + ": ") for e, label in zip(s.evidence, s.all, strict=True))
    severity, reason = suggest_severity(o, s)
    assert severity in mining.SEVERITIES and reason
    # A duplicated irreversible action is always critical and always a candidate.
    if o.production and "duplicate_side_effect" in o.signals:
        assert severity == "critical" and why
    # The fingerprint does not depend on the trace or its version.
    assert fingerprint(replace(o, trace_id="f" * 32, agent_version="7.0.0"), s) == fingerprint(o, s)
    # Rules are pure: the same observation, the same answer.
    assert suggest_taxonomy(o) == s and detect(o) == why
