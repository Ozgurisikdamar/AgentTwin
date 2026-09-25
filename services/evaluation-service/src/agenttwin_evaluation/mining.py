"""Regression mining (spec §18, §44, §121; ADR-0032): production failures in,
groups that a person can promote to a permanent regression test out.

Pure: no I/O. The worker feeds it what the trace service announces
(``trace.ingested.v1``) or answers (a trace row), and stores what it decides.

* :func:`observation_from_event` / :func:`observation_from_trace` normalize a
  trace into an :class:`Observation`; :func:`with_flag` adds a person's flag.
* :func:`detect` says whether the trace is a candidate, and why. Only
  deterministic signals count: an unverified outcome alone is not a failure.
* :func:`features` are the structured features stored for a candidate (never
  its content); :func:`feature_text` is the sentence built from them that is
  embedded, so no customer data reaches a vector either.
* :func:`suggest_taxonomy` and :func:`suggest_severity` apply rules, each with
  the evidence that fired it; :func:`fingerprint` identifies a known failure.
* :func:`choose_group` decides exact match, nearest neighbour or a new group
  of one; nothing is forced into a cluster.
* :func:`transition` is the lifecycle: CANDIDATE, CONFIRMED, PROMOTED, FIXED,
  DISMISSED, REOPENED.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal

__all__ = [
    "ACTIONS",
    "FLAG_KINDS",
    "SEVERITIES",
    "STATUSES",
    "TAXONOMY",
    "GroupChoice",
    "InvalidTransition",
    "Observation",
    "OccurrenceEffect",
    "Suggestion",
    "at_or_after",
    "choose_group",
    "component",
    "detect",
    "feature_text",
    "features",
    "fingerprint",
    "observation_from_detail",
    "observation_from_event",
    "observation_from_trace",
    "on_occurrence",
    "suggest_severity",
    "suggest_taxonomy",
    "title",
    "transition",
    "violation_kinds",
    "with_flag",
]

#: The failure taxonomy of spec §18. Organizations add custom tags on top.
TAXONOMY: tuple[str, ...] = (
    "WRONG_TOOL",
    "WRONG_ARGUMENT",
    "MISSING_TOOL",
    "TOOL_ERROR_HANDLING",
    "DUPLICATE_SIDE_EFFECT",
    "RETRY_SAFETY",
    "LOOP",
    "TIMEOUT",
    "COST_BUDGET",
    "LATENCY",
    "POLICY_VIOLATION",
    "AUTHORIZATION",
    "PROMPT_INJECTION",
    "DATA_LEAKAGE",
    "STALE_CONTEXT",
    "RETRIEVAL_FAILURE",
    "HALLUCINATED_SUCCESS",
    "INCORRECT_ESCALATION",
    "MISSING_APPROVAL",
    "STATE_MISMATCH",
    "UNKNOWN",
)
Severity = Literal["critical", "high", "medium", "low"]
SEVERITIES: tuple[Severity, ...] = ("critical", "high", "medium", "low")
Status = Literal["CANDIDATE", "CONFIRMED", "PROMOTED", "FIXED", "DISMISSED", "REOPENED"]
STATUSES: tuple[Status, ...] = ("CANDIDATE", "CONFIRMED", "PROMOTED", "FIXED", "DISMISSED", "REOPENED")
FLAG_KINDS = ("incident", "negative_feedback", "manual")
Action = Literal["confirm", "dismiss", "reopen", "promote", "fixed"]
ACTIONS: tuple[Action, ...] = ("confirm", "dismiss", "reopen", "promote", "fixed")

_WRITE_RISKS = frozenset({"WRITE_REVERSIBLE", "WRITE_IRREVERSIBLE", "EXECUTE", "ADMIN"})
_DENIED = frozenset({"denied", "forbidden", "unauthorized", "access_denied", "cross_tenant"})
_TIMEOUT = frozenset({"timeout", "timed_out", "deadline_exceeded"})


# ---------------------------------------------------------------- observation


@dataclass(frozen=True)
class Observation:
    """What is known about one trace, from the trace service's summary.

    Tool risks come from the ingestion event (``observed_tools``); a trace row
    read later does not carry them, so an update keeps the risks it had."""

    trace_id: str
    agent: str
    agent_version: str | None = None
    environment: str | None = None
    source: str | None = None
    simulation_run_id: str | None = None
    started_at: str | None = None
    signals: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    violations: tuple[str, ...] = ()
    policy_decisions: tuple[str, ...] = ()
    tool_risks: tuple[tuple[str, str], ...] = ()
    outcome: str | None = None
    outcome_verified: bool | None = None
    contradiction: bool = False
    failing_tool: str | None = None
    error_type: str | None = None
    last_successful_step: str | None = None
    retry_count: int = 0
    step_count: int = 0
    cost_usd: float | None = None
    duration_ms: float | None = None
    model: str | None = None
    prompt_hash: str | None = None
    tool_sequence_sketch: str | None = None
    flags: tuple[tuple[str, str], ...] = ()

    def risk_of(self, tool: str | None) -> str | None:
        return dict(self.tool_risks).get(tool) if tool else None

    @property
    def production(self) -> bool:
        """A production trace, not a simulated one (only those are mined)."""
        return self.source == "production" and not self.simulation_run_id


def _strs(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        return ()
    return tuple(str(v) for v in value if isinstance(v, str) and v)


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _map(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _from_summary(summary: Mapping[str, Any]) -> dict[str, Any]:
    verified = summary.get("outcome_verified")
    return {
        "tools": _strs(summary.get("tools")),
        "errors": _strs(summary.get("errors")),
        "violations": _strs(summary.get("violations")),
        "policy_decisions": _strs(summary.get("policy_decisions")),
        "outcome": _str(summary.get("outcome")),
        "outcome_verified": verified if isinstance(verified, bool) else None,
        "failing_tool": _str(summary.get("failing_tool")),
        "error_type": _str(summary.get("error_type")),
        "last_successful_step": _str(summary.get("last_successful_step")),
        "retry_count": _int(summary.get("retry_count")),
        "step_count": _int(summary.get("step_count")),
        "cost_usd": _num(summary.get("cost_usd")) if summary.get("cost_known", True) else None,
        "duration_ms": _num(summary.get("duration_ms")),
        "model": _str(summary.get("model")),
        "prompt_hash": _str(summary.get("prompt_hash")),
        "tool_sequence_sketch": _str(summary.get("tool_sequence_sketch")),
    }


def observation_from_event(payload: Mapping[str, Any]) -> Observation:
    """From a ``trace.ingested.v1`` payload."""
    summary = _map(payload.get("summary"))
    risks = []
    for t in payload.get("observed_tools") or []:
        if isinstance(t, Mapping) and _str(t.get("name")) and _str(t.get("risk")):
            risks.append((str(t["name"]), str(t["risk"])))
    signals = _strs(payload.get("signals"))
    return Observation(
        trace_id=str(payload["trace_id"]),
        agent=str(payload.get("agent") or summary.get("agent") or ""),
        agent_version=_str(payload.get("agent_version")) or _str(summary.get("agent_version")),
        environment=_str(payload.get("environment")),
        source=_str(payload.get("source")),
        simulation_run_id=_str(payload.get("simulation_run_id")),
        started_at=_str(payload.get("started_at")),
        signals=tuple(sorted(set(signals))),
        tool_risks=tuple(sorted(set(risks))),
        contradiction="contradiction" in signals,
        **_from_summary(summary),
    )


def observation_from_trace(row: Mapping[str, Any], previous: Observation | None = None) -> Observation:
    """From a trace row of the trace service (``GET /api/v1/traces/{id}``),
    e.g. after its outcome was recorded. Keeps the risks and flags already
    known."""
    summary = _map(row.get("summary"))
    signals = _strs(row.get("signals"))
    base = _from_summary(summary)
    status = _str(row.get("outcome_status"))
    if status:
        base["outcome"] = status
    verified = row.get("outcome_verified")
    if isinstance(verified, bool):
        base["outcome_verified"] = verified
    return Observation(
        trace_id=str(row["trace_id"]),
        agent=str(row.get("agent_name") or summary.get("agent") or (previous.agent if previous else "")),
        agent_version=_str(row.get("agent_version")) or _str(summary.get("agent_version")),
        environment=_str(row.get("environment")),
        source=_str(row.get("source")),
        simulation_run_id=_str(row.get("simulation_run_id")),
        started_at=_str(row.get("started_at")),
        signals=tuple(sorted(set(signals))),
        tool_risks=previous.tool_risks if previous else (),
        contradiction="contradiction" in signals,
        flags=previous.flags if previous else (),
        **base,
    )


def observation_from_detail(detail: Mapping[str, Any]) -> Observation:
    """From the trace service's trace detail (``GET /api/v1/traces/{id}``:
    trace, spans, flags): the row, the risks its tool spans recorded and the
    flags people put on it."""
    risks = set()
    for span in detail.get("spans") or []:
        if isinstance(span, Mapping) and span.get("kind") == "tool":
            name, risk = _str(span.get("tool_name")), _str(span.get("tool_risk"))
            if name and risk:
                risks.add((name, risk))
    obs = replace(observation_from_trace(_map(detail.get("trace"))), tool_risks=tuple(sorted(risks)))
    for flag in detail.get("flags") or []:
        if isinstance(flag, Mapping) and _str(flag.get("reason")):
            obs = with_flag(obs, str(flag.get("kind") or "manual"), str(flag["reason"]))
    return obs


def with_flag(obs: Observation, kind: str, reason: str) -> Observation:
    """A person flagged the trace (``trace.flagged.v1``)."""
    kind = kind if kind in FLAG_KINDS else "manual"
    flag = (kind, reason.strip()[:500])
    if flag in obs.flags:
        return obs
    return replace(obs, flags=(*obs.flags, flag))


# ---------------------------------------------------------------- detection


def violation_kinds(violations: Iterable[str]) -> tuple[str, ...]:
    """``policy_denied:refund_payment`` → ``policy_denied``: the kind, without
    what it was about (tool names, arguments)."""
    return tuple(sorted({v.split(":", 1)[0] for v in violations if v}))


def _tool_errors(obs: Observation) -> list[tuple[str, str]]:
    """(tool, error) pairs from ``tool:error`` entries of the summary."""
    out = []
    for e in obs.errors:
        tool, sep, err = e.partition(":")
        if sep and tool not in ("model", "agent", "http", "mcp") and err:
            out.append((tool, err.lower()))
    return out


def detect(obs: Observation) -> list[str]:
    """Why the trace is a regression candidate — empty if it is not.

    Only production traces are mined, and only for deterministic signals or a
    person's flag. An unverified outcome alone is not a failure: most
    production outcomes are unverified, and an inbox of every one of them
    would be read by nobody."""
    if not obs.production:
        return []
    sig = set(obs.signals)
    kinds = set(violation_kinds(obs.violations))
    why: list[str] = []
    if obs.outcome in ("FAILURE", "PARTIAL") or "outcome_failure" in sig:
        why.append(f"the outcome was {obs.outcome or 'a failure'}")
    if obs.contradiction:
        why.append("it claimed a success the verified outcome disproved")
    if "duplicate_side_effect" in sig or "duplicate_irreversible_action" in kinds:
        why.append("it took an irreversible action twice")
    if "retry_without_idempotency_key" in kinds:
        why.append("it retried a write without an idempotency key")
    if "timeout_after_mutation" in sig:
        why.append("a tool timed out after it may have changed state")
    if "loop_detected" in sig:
        why.append("it repeated the same step in a loop")
    if "policy_denied" in sig or "policy_denied" in kinds:
        why.append("a policy denied one of its actions")
    if "tool_error" in sig:
        failed = sorted({t for t, _ in _tool_errors(obs)})
        why.append(f"a tool failed ({', '.join(failed)})" if failed else "a tool failed")
    if "model_error" in sig:
        why.append("a model call failed")
    for kind, reason in obs.flags:
        label = kind.replace("_", " ")
        why.append(f"flagged as {label}: {reason}" if reason else f"flagged as {label}")
    return why


# ---------------------------------------------------------------- features


def features(obs: Observation) -> dict[str, Any]:
    """The structured features of a candidate (spec §44), without content."""
    return {
        "agent": obs.agent,
        "version": obs.agent_version,
        "environment": obs.environment,
        "outcome": obs.outcome,
        "outcome_verified": obs.outcome_verified,
        "contradiction": obs.contradiction,
        "last_successful_step": obs.last_successful_step,
        "failing_tool": obs.failing_tool,
        "failing_tool_risk": obs.risk_of(obs.failing_tool),
        "error_type": obs.error_type,
        "tool_errors": [f"{t}:{e}" for t, e in _tool_errors(obs)],
        "retry_count": obs.retry_count,
        "step_count": obs.step_count,
        "violations": list(violation_kinds(obs.violations)),
        "policy_decisions": sorted(set(obs.policy_decisions)),
        "signals": list(obs.signals),
        "tool_sequence_sketch": obs.tool_sequence_sketch,
        "prompt_hash": obs.prompt_hash,
        "model": obs.model,
        "cost_usd": obs.cost_usd,
        "duration_ms": obs.duration_ms,
        "flags": sorted({k for k, _ in obs.flags}),
    }


_PHRASES: dict[str, str] = {
    "duplicate_irreversible_action": "took an irreversible action twice",
    "retry_without_idempotency_key": "retried a write without an idempotency key",
    "policy_denied": "a policy denied an action",
}


def feature_text(f: Mapping[str, Any], taxonomy: Sequence[str]) -> str:
    """The sentence that is embedded: built from the features alone, so no
    customer data reaches a vector. The version is left out on purpose —
    the same failure in two versions is the same failure."""
    parts = [f"agent {f.get('agent')}"]
    if taxonomy:
        parts.append("failure " + " ".join(t.lower().replace("_", " ") for t in taxonomy))
    if f.get("failing_tool"):
        err = f.get("error_type") or "an error"
        parts.append(f"tool {f['failing_tool']} failed with {err}")
    for t in f.get("tool_errors") or []:
        parts.append(f"tool error {str(t).replace(':', ' ')}")
    for v in f.get("violations") or []:
        parts.append(_PHRASES.get(str(v), str(v).replace("_", " ")))
    if f.get("contradiction"):
        parts.append("claimed success that the final state disproved")
    if f.get("outcome"):
        parts.append(f"outcome {str(f['outcome']).lower()}")
    if f.get("tool_sequence_sketch"):
        parts.append("tools " + str(f["tool_sequence_sketch"]).replace(">", " then "))
    for s in f.get("signals") or []:
        parts.append("signal " + str(s).replace("_", " "))
    for k in f.get("flags") or []:
        parts.append("flagged " + str(k).replace("_", " "))
    return "; ".join(parts)


# ---------------------------------------------------------------- suggestions


@dataclass(frozen=True)
class Suggestion:
    """A suggested label with the evidence of every rule that fired."""

    primary: str
    secondary: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()

    @property
    def all(self) -> tuple[str, ...]:
        return (self.primary, *self.secondary)


def _duplicated_tool(obs: Observation) -> str | None:
    """The irreversible tool taken twice: the failing one if irreversible,
    else the irreversible tool called most often."""
    if obs.risk_of(obs.failing_tool) == "WRITE_IRREVERSIBLE":
        return obs.failing_tool
    counts: dict[str, int] = {}
    for t in obs.tools:
        if obs.risk_of(t) == "WRITE_IRREVERSIBLE":
            counts[t] = counts.get(t, 0) + 1
    repeated = sorted((n, t) for t, n in counts.items() if n > 1)
    return repeated[-1][1] if repeated else obs.failing_tool


def suggest_taxonomy(obs: Observation) -> Suggestion:
    """Deterministic rules from signals and violations to the §18 taxonomy.
    The first rule that fires is primary; the others are secondary."""
    sig = set(obs.signals)
    kinds = set(violation_kinds(obs.violations))
    errors = _tool_errors(obs)
    denied = sorted({t for t, e in errors if e in _DENIED})
    timed_out = sorted({t for t, e in errors if e in _TIMEOUT})
    other = sorted({t for t, e in errors if e not in _DENIED | _TIMEOUT})
    fired: list[tuple[str, str]] = []
    if "duplicate_side_effect" in sig or "duplicate_irreversible_action" in kinds:
        tool = _duplicated_tool(obs)
        fired.append(
            ("DUPLICATE_SIDE_EFFECT", f"{tool or 'an irreversible action'} took effect more than once")
        )
    if obs.contradiction:
        fired.append(
            ("HALLUCINATED_SUCCESS", "the agent claimed success and the verified outcome disproved it")
        )
    if denied:
        fired.append(("AUTHORIZATION", f"access was denied to {', '.join(denied)}"))
    if "policy_denied" in sig or "policy_denied" in kinds:
        fired.append(("POLICY_VIOLATION", "a policy denied one of its actions"))
    if "retry_without_idempotency_key" in kinds:
        fired.append(("RETRY_SAFETY", "a write was retried without an idempotency key"))
    if "loop_detected" in sig:
        fired.append(("LOOP", "the same step repeated in a loop"))
    if timed_out or "timeout_after_mutation" in sig or (obs.error_type or "").lower() in _TIMEOUT:
        tools = timed_out or [obs.failing_tool or "a tool"]
        fired.append(("TIMEOUT", f"{', '.join(tools)} timed out"))
    if other or ("tool_error" in sig and not (denied or timed_out)):
        fired.append(("TOOL_ERROR_HANDLING", f"{', '.join(other) or 'a tool'} failed"))
    if not fired and obs.outcome in ("FAILURE", "PARTIAL") and obs.outcome_verified:
        fired.append(("STATE_MISMATCH", f"the verified outcome was {obs.outcome}"))
    if not fired:
        fired.append(("UNKNOWN", "no rule explains the failure; a person should label it"))
    # Each rule has its own label, so the labels are distinct.
    labels = [label for label, _ in fired]
    evidence = [f"{label}: {why}" for label, why in fired]
    return Suggestion(labels[0], tuple(labels[1:]), tuple(evidence))


_BASE_SEVERITY: dict[str, Severity] = {
    "DUPLICATE_SIDE_EFFECT": "critical",
    "DATA_LEAKAGE": "critical",
    "HALLUCINATED_SUCCESS": "high",
    "AUTHORIZATION": "high",
    "POLICY_VIOLATION": "high",
    "RETRY_SAFETY": "high",
    "MISSING_APPROVAL": "high",
    "PROMPT_INJECTION": "high",
    "TIMEOUT": "medium",
    "LOOP": "medium",
    "TOOL_ERROR_HANDLING": "medium",
    "STATE_MISMATCH": "medium",
}


def suggest_severity(obs: Observation, taxonomy: Suggestion) -> tuple[Severity, str]:
    """The highest severity any label or flag calls for, with why."""
    best: Severity = "low"
    why = "no rule raised it"
    for label in taxonomy.all:
        s = _BASE_SEVERITY.get(label, "low")
        if SEVERITIES.index(s) < SEVERITIES.index(best):
            best, why = s, f"{label} is {s}"
    irreversible = any(r == "WRITE_IRREVERSIBLE" for _, r in obs.tool_risks)
    if "HALLUCINATED_SUCCESS" in taxonomy.all and irreversible and best != "critical":
        best, why = "critical", "a success was misreported around an irreversible action"
    kinds = {k for k, _ in obs.flags}
    if "incident" in kinds and SEVERITIES.index(best) > SEVERITIES.index("high"):
        best, why = "high", "flagged as an incident"
    elif "negative_feedback" in kinds and SEVERITIES.index(best) > SEVERITIES.index("medium"):
        best, why = "medium", "flagged with negative feedback"
    return best, why


def fingerprint(obs: Observation, taxonomy: Suggestion) -> str:
    """A known failure: same agent, same primary label, same failing tool and
    error, same kinds of violation. Versions and arguments are left out."""
    key = "|".join(
        [
            "v1",
            obs.agent,
            taxonomy.primary,
            obs.failing_tool or "",
            (obs.error_type or "").lower(),
            ",".join(violation_kinds(obs.violations)),
        ]
    )
    return hashlib.sha256(key.encode()).hexdigest()[:32]


_TITLES: dict[str, str] = {
    "DUPLICATE_SIDE_EFFECT": "{tool} took effect twice",
    "HALLUCINATED_SUCCESS": "Claimed success that the final state disproved",
    "AUTHORIZATION": "Access to {tool} was denied",
    "POLICY_VIOLATION": "A policy denied {tool}",
    "RETRY_SAFETY": "{tool} retried without an idempotency key",
    "LOOP": "Looped on {tool}",
    "TIMEOUT": "{tool} timed out",
    "TOOL_ERROR_HANDLING": "{tool} failed",
    "STATE_MISMATCH": "The final state was wrong",
    "UNKNOWN": "Unexplained failure",
}


def component(obs: Observation, taxonomy: Suggestion) -> str | None:
    """The tool the failure points at: the one taken twice for a duplicated
    side effect, else the failing one."""
    if taxonomy.primary == "DUPLICATE_SIDE_EFFECT":
        return _duplicated_tool(obs)
    return obs.failing_tool


def title(obs: Observation, taxonomy: Suggestion) -> str:
    """A short failure title for the inbox."""
    text = _TITLES.get(taxonomy.primary, taxonomy.primary.replace("_", " ").capitalize())
    return text.format(tool=component(obs, taxonomy) or "a tool")


# ---------------------------------------------------------------- grouping


@dataclass(frozen=True)
class GroupChoice:
    """Where a candidate goes: ``exact`` (same fingerprint), ``similar``
    (nearest neighbour above the threshold) or ``new`` (a group of one)."""

    kind: Literal["exact", "similar", "new"]
    group_id: str | None
    similarity: float | None
    reason: str


def choose_group(
    *,
    exact_group: str | None,
    nearest_group: str | None,
    nearest_similarity: float | None,
    threshold: float,
) -> GroupChoice:
    """Known failure first, then the nearest neighbour if close enough, else
    a new group of one. Nothing is forced into a cluster."""
    if exact_group:
        return GroupChoice("exact", exact_group, None, "the same failure (same fingerprint) is known")
    if nearest_group and nearest_similarity is not None and nearest_similarity >= threshold:
        return GroupChoice(
            "similar",
            nearest_group,
            nearest_similarity,
            f"close to a known failure (similarity {nearest_similarity:.2f} ≥ {threshold:.2f})",
        )
    if nearest_similarity is not None:
        reason = f"no known failure is close enough (nearest {nearest_similarity:.2f} < {threshold:.2f})"
    else:
        reason = "the first failure of its kind"
    return GroupChoice("new", None, nearest_similarity, reason)


# ---------------------------------------------------------------- lifecycle


class InvalidTransition(ValueError):
    """The action is not possible from the group's status."""


_ALLOWED: dict[Action, frozenset[str]] = {
    "confirm": frozenset({"CANDIDATE", "REOPENED"}),
    "dismiss": frozenset({"CANDIDATE", "CONFIRMED", "REOPENED"}),
    "reopen": frozenset({"DISMISSED", "FIXED"}),
    "promote": frozenset({"CANDIDATE", "CONFIRMED", "REOPENED"}),
    "fixed": frozenset({"PROMOTED", "REOPENED"}),
}
_TO: dict[Action, Status] = {
    "confirm": "CONFIRMED",
    "dismiss": "DISMISSED",
    "reopen": "REOPENED",
    "promote": "PROMOTED",
    "fixed": "FIXED",
}


def transition(status: str, action: Action, *, has_case: bool = False) -> Status:
    """The status after a person's (or the system's) action.

    A group already promoted cannot be promoted again (its test exists: a
    reopened group waits for a fix instead), and only a group with a test can
    be fixed."""
    if action not in _ALLOWED:
        raise InvalidTransition(f"Unknown action {action!r}.")
    if status not in _ALLOWED[action]:
        raise InvalidTransition(f"A {status.lower()} regression cannot be {_TO[action].lower()}.")
    if action == "promote" and has_case:
        raise InvalidTransition("This regression already has a test; it is fixed when a version passes it.")
    if action == "fixed" and not has_case:
        raise InvalidTransition("Only a promoted regression can be fixed: promote it first.")
    return _TO[action]


_TOKEN = re.compile(r"\d+|[A-Za-z]+")


def _version_key(v: str) -> tuple[tuple[int, ...], tuple[tuple[int, int | str], ...]] | None:
    """(release numbers, pre-release tokens), or None if not a version."""
    core, _, pre = v.strip().lstrip("vV").partition("-")
    parts = core.split(".")
    if not all(p.isdigit() for p in parts):
        return None
    tokens = tuple((0, int(t)) if t.isdigit() else (1, t) for t in _TOKEN.findall(pre))
    return tuple(int(p) for p in parts), tokens


def at_or_after(version: str | None, fixed: str | None) -> bool:
    """Whether ``version`` is the fixed version or a later one (``1.4`` equals
    ``1.4.0``; ``1.4.0-rc1`` comes before ``1.4.0``). When either is missing
    or not comparable, the answer is yes: reopening a regression by mistake is
    safer than hiding one."""
    if not version or not fixed:
        return True
    a, b = _version_key(version), _version_key(fixed)
    if a is None or b is None:
        return True
    width = max(len(a[0]), len(b[0]))
    core_a = a[0] + (0,) * (width - len(a[0]))
    core_b = b[0] + (0,) * (width - len(b[0]))
    if core_a != core_b:
        return core_a > core_b
    if not a[1]:  # a release comes after its pre-releases
        return True
    if not b[1]:
        return False
    return a[1] >= b[1]


@dataclass(frozen=True)
class OccurrenceEffect:
    """What a new occurrence does to its group's status."""

    status: str
    reopened: bool = False
    reason: str | None = None


def on_occurrence(status: str, version: str | None, fixed_version: str | None) -> OccurrenceEffect:
    """A new failure joins a group. A fixed regression that fails again in the
    fixed version or a later one is reopened; a dismissed group keeps
    counting but stays dismissed (known noise stays quiet)."""
    if status == "FIXED" and at_or_after(version, fixed_version):
        return OccurrenceEffect(
            "REOPENED",
            True,
            f"failed again in {version or 'an unknown version'} after the fix in {fixed_version}",
        )
    return OccurrenceEffect(status)
