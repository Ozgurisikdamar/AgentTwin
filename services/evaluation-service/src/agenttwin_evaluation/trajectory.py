"""Trajectories of simulated cases and where a candidate's parts from the
baseline's (spec §46, §47).

A trajectory is the case's steps normalized to ``TOOL(name)``,
``RETRIEVAL``, ``POLICY(id)`` and ``OUTCOME``, built from what the tool twin
recorded — never from the agent's own account. Model turns are not part of
it: their content is not captured by default (ADR-0008), and the tool calls
they led to are.

The *first divergence* is the first step at which the two trajectories
differ: a different tool, the same tool with different arguments, the same
call with a different result, a policy decision only one side met, one side
stopping earlier, or a different reported outcome. It is described in plain
language from the recorded evidence ("observed", never "caused"): the
comparison shows where the paths part, not why.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from agenttwin.hashing import canonical_json
from agenttwin_core.evaluators import ToolCall
from agenttwin_core.evaluators.signals import FAILED_STATUSES, WRITE_RISKS
from agenttwin_core.paths import diff_state

__all__ = [
    "MAX_STEPS",
    "Divergence",
    "Step",
    "align",
    "first_divergence",
    "state_changes",
    "trajectory",
]

# Cases are bounded by the twin's call budget; the trajectory kept for the
# comparison is bounded again so a runaway agent cannot bloat the result.
MAX_STEPS = 200
MAX_STATE_CHANGES = 25
_LABEL_CHARS = 160
_VALUE_CHARS = 60
_RISK_WORDS = {
    "WRITE_IRREVERSIBLE": "irreversible",
    "WRITE_REVERSIBLE": "state-changing",
    "EXECUTE": "executing",
    "ADMIN": "administrative",
}


def _short(value: Any, limit: int = _VALUE_CHARS) -> str:
    text = value if isinstance(value, str) else canonical_json(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass(frozen=True)
class Step:
    """One normalized step. ``effect`` is ``applied`` when the twin recorded
    the call's side effect and ``replayed`` when it answered from the
    idempotency record without a second effect."""

    index: int
    kind: str  # TOOL | RETRIEVAL | POLICY | OUTCOME
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    status: str | None = None
    risk: str | None = None
    fault: str | None = None
    effect: str | None = None
    ref: str | None = None

    @property
    def signature(self) -> tuple[str, str, str, str | None, str | None]:
        """What must be equal for two steps to be the same step. A retrieval's
        query is free text an agent may phrase differently each time, so
        retrievals align on the fact of retrieving."""
        args = "" if self.kind == "RETRIEVAL" else canonical_json(dict(self.arguments))
        return (self.kind, self.name, args, self.status, self.effect)

    @property
    def writes(self) -> bool:
        return self.kind == "TOOL" and self.risk in WRITE_RISKS

    def label(self) -> str:
        if self.kind == "TOOL":
            args = ", ".join(f"{k}={_short(v)}" for k, v in sorted(self.arguments.items()))
            text = f"{self.name}({args})"
        elif self.kind == "RETRIEVAL":
            text = f"retrieval({_short(self.arguments.get('query', ''))})"
        elif self.kind == "POLICY":
            text = f"policy {self.name} ({self.status})"
        else:
            detail = self.arguments.get("business_outcome")
            text = f"outcome {self.name}" + (f" ({detail})" if detail else "")
        return text if len(text) <= _LABEL_CHARS else text[: _LABEL_CHARS - 1] + "…"

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "index": self.index,
            "kind": self.kind,
            "name": self.name,
            "label": self.label(),
            "arguments": dict(self.arguments),
        }
        for key in ("status", "risk", "fault", "effect", "ref"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        return out


def _outcome(agent: Mapping[str, Any] | None) -> tuple[str, dict[str, Any], str | None]:
    if agent is None:
        return "NOT_RUN", {}, None
    kind = str(agent.get("kind") or "ok")
    if kind != "ok":
        return "NO_ANSWER", {}, kind
    claimed = agent.get("claimed_outcome")
    business = agent.get("business_outcome")
    args = {"business_outcome": business} if isinstance(business, str) and business else {}
    return (claimed if isinstance(claimed, str) and claimed else "UNREPORTED"), args, "ok"


def trajectory(steps: Sequence[Mapping[str, Any]], agent: Mapping[str, Any] | None) -> list[Step]:
    """The case's steps as the twin recorded them (the simulation contract's
    ``Step`` list), followed by the outcome the agent reported."""
    out: list[Step] = []

    def add(kind: str, name: str, **kw: Any) -> None:
        out.append(Step(index=len(out) + 1, kind=kind, name=name, **kw))

    for raw in sorted(steps, key=lambda s: int(s.get("seq") or 0)):
        if len(out) >= MAX_STEPS - 1:
            break
        record = raw.get("record")
        if not isinstance(record, Mapping):
            continue
        if raw.get("kind") == "tool_call":
            call = ToolCall.from_record(record, raw.get("latency_ms"))
            effect = "replayed" if call.replayed else ("applied" if call.mutated else None)
            ref = f"tool_call:{call.seq}"
            add(
                "TOOL",
                call.tool,
                arguments=dict(call.arguments),
                status=call.status,
                risk=call.risk,
                fault=call.fault,
                effect=effect,
                ref=ref,
            )
            if call.policy_violation:
                decision = "violated" if call.succeeded else "denied"
                add("POLICY", call.policy_violation, arguments={"tool": call.tool}, status=decision, ref=ref)
            elif call.cross_tenant == "allowed":
                # Another tenant's record came back: a violation nothing stopped.
                add("POLICY", "CROSS_TENANT", arguments={"tool": call.tool}, status="allowed", ref=ref)
        elif raw.get("kind") == "retrieval":
            add(
                "RETRIEVAL",
                "knowledge_base",
                arguments={"query": str(record.get("query") or "")},
                ref=f"retrieval:{record.get('seq')}",
            )
    name, args, status = _outcome(agent)
    add("OUTCOME", name, arguments=args, status=status)
    return out


@dataclass(frozen=True)
class Divergence:
    """The first step at which the candidate's trajectory differs."""

    index: int
    kind: str  # tool | arguments | result | policy | outcome | stopped_early | extra_steps
    summary: str
    impact: str | None
    baseline: Step | None
    candidate: Step | None
    argument_changes: tuple[Mapping[str, Any], ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "kind": self.kind,
            "summary": self.summary,
            "impact": self.impact,
            "baseline": self.baseline.to_json() if self.baseline else None,
            "candidate": self.candidate.to_json() if self.candidate else None,
            "argument_changes": [dict(c) for c in self.argument_changes],
        }


def _changes(before: Any, after: Any, limit: int) -> list[dict[str, Any]]:
    """``diff_state`` as baseline → candidate: ``{path, baseline?, candidate?}``."""
    out: list[dict[str, Any]] = []
    for c in diff_state(before, after, limit=limit):
        entry: dict[str, Any] = {"path": c["path"], "change": c["op"]}
        if "before" in c:
            entry["baseline"] = c["before"]
        if "after" in c:
            entry["candidate"] = c["after"]
        out.append(entry)
    return out


def state_changes(baseline: Any, candidate: Any, *, limit: int = MAX_STATE_CHANGES) -> list[dict[str, Any]]:
    """Where the candidate's final twin state differs from the baseline's."""
    if baseline is None or candidate is None:
        return []
    return _changes(baseline, candidate, limit)


def _describe_changes(changes: Sequence[Mapping[str, Any]]) -> str:
    parts = []
    for c in changes[:3]:
        path = c["path"]
        if c["change"] == "added":
            parts.append(f"{path}={_short(c['candidate'])} added")
        elif c["change"] == "removed":
            parts.append(f"{path} dropped")
        else:
            parts.append(f"{path} {_short(c['baseline'])} → {_short(c['candidate'])}")
    more = f" (+{len(changes) - 3} more)" if len(changes) > 3 else ""
    return "; ".join(parts) + more


def _risk_word(step: Step) -> str:
    return _RISK_WORDS.get(step.risk or "", "")


def _repeat_after_effect(candidate: Step, before: Sequence[Step]) -> Step | None:
    """An earlier call of the same write with the same arguments that failed
    after the twin had already applied its effect (a timeout after the
    refund went through)."""
    if not candidate.writes:
        return None
    for s in reversed(before):
        if s.kind == "TOOL" and s.name == candidate.name:
            same_args = canonical_json(dict(s.arguments)) == canonical_json(dict(candidate.arguments))
            if same_args and s.status in FAILED_STATUSES and s.effect == "applied":
                return s
            return None
    return None


def _tool_impact(i: int, b: Step | None, c: Step, cs: Sequence[Step]) -> str | None:
    """What the candidate's step at ``i`` means, from the recorded evidence;
    ``b`` is the baseline's step there, None when the baseline had stopped."""
    repeated = _repeat_after_effect(c, cs[:i])
    if repeated is not None:
        instead = "had stopped" if b is None else f"called {b.name} at this point instead"
        return (
            f"The candidate repeated {c.name} after a {repeated.status} that had already applied it "
            f"(step {repeated.index}); the baseline {instead}."
        )
    if b is None:
        if c.writes:
            return f"The candidate made the {_risk_word(c)} {c.name} after the baseline had stopped."
        return None
    called_before = {s.name for s in cs[:i] if s.kind == "TOOL"}
    if c.writes and b.kind == "TOOL" and not b.writes and b.name not in called_before:
        word = _risk_word(c)
        return (
            f"The candidate called the {word} {c.name} without first calling {b.name}, "
            f"which the baseline called at this point."
        )
    if b.writes and c.kind == "TOOL" and not c.writes:
        return f"The candidate checked with {c.name} where the baseline went on to {b.name}."
    return None


def _describe(i: int, b: Step, c: Step, cs: Sequence[Step]) -> Divergence:
    n = i + 1
    if b.kind == "OUTCOME" and c.kind == "OUTCOME":
        return Divergence(
            n,
            "outcome",
            f"The baseline reported {b.label()}; the candidate reported {c.label()}.",
            None,
            b,
            c,
        )
    if "POLICY" in (b.kind, c.kind):
        # A policy step follows its tool call, and every step before this one
        # is the same on both sides: the same call met the policy on one side.
        impact = None
        if b.kind == "POLICY" and c.kind == "POLICY":
            summary = f"At step {n} the baseline met {b.label()}; the candidate met {c.label()}."
        elif c.kind == "POLICY":
            tool = c.arguments.get("tool")
            summary = f"At step {n} the candidate's {tool} call met {c.label()}; the baseline's did not."
            if c.status == "allowed":
                impact = f"The candidate received data {c.name} should have kept from it."
            elif c.status == "denied":
                impact = f"The candidate's call was refused by policy {c.name}."
        else:
            tool = b.arguments.get("tool")
            summary = f"At step {n} the baseline's {tool} call met {b.label()}; the candidate's did not."
        return Divergence(n, "policy", summary, impact, b, c)
    if c.kind == "OUTCOME":
        impact = None
        if b.kind == "TOOL" and "escalat" in b.name:
            impact = f"The candidate never called {b.name}."
        elif b.writes:
            impact = f"The candidate ended without the {_risk_word(b)} {b.name} the baseline made."
        return Divergence(
            n,
            "stopped_early",
            f"The candidate stopped after step {i} and reported {c.name}; "
            f"the baseline went on with {b.label()}.",
            impact,
            b,
            c,
        )
    if b.kind == "OUTCOME":
        impact = _tool_impact(i, None, c, cs) if c.kind == "TOOL" else None
        return Divergence(
            n,
            "extra_steps",
            f"The baseline stopped after step {i} and reported {b.name}; "
            f"the candidate went on with {c.label()}.",
            impact,
            b,
            c,
        )
    if b.kind == "TOOL" and c.kind == "TOOL" and b.name == c.name:
        if canonical_json(dict(b.arguments)) != canonical_json(dict(c.arguments)):
            changes = _changes(dict(b.arguments), dict(c.arguments), 10)
            impact = None
            dropped = [ch["path"] for ch in changes if ch["change"] == "removed" and "idempot" in ch["path"]]
            if dropped:
                impact = (
                    f"The candidate sent {c.name} without the idempotency key the baseline used "
                    f"({dropped[0]})."
                )
            return Divergence(
                n,
                "arguments",
                f"At step {n} both called {b.name}, with different arguments: {_describe_changes(changes)}.",
                impact,
                b,
                c,
                tuple(changes),
            )
        return Divergence(
            n,
            "result",
            f"At step {n} both made the same {b.name} call; the baseline's ended {b.status}"
            f"{' (' + b.effect + ')' if b.effect else ''}, the candidate's {c.status}"
            f"{' (' + c.effect + ')' if c.effect else ''}.",
            None,
            b,
            c,
        )
    impact = _tool_impact(i, b, c, cs) if b.kind == "TOOL" and c.kind == "TOOL" else None
    return Divergence(
        n,
        "tool",
        f"At step {n} the baseline called {b.label()}; the candidate called {c.label()}.",
        impact,
        b,
        c,
    )


def first_divergence(baseline: Sequence[Step], candidate: Sequence[Step]) -> Divergence | None:
    """The first step at which the trajectories differ, or None when they are
    the same step for step (both end with an OUTCOME step)."""
    for i in range(min(len(baseline), len(candidate))):
        b, c = baseline[i], candidate[i]
        if b.signature != c.signature:
            return _describe(i, b, c, candidate)
    return None


def align(baseline: Sequence[Step], candidate: Sequence[Step]) -> list[dict[str, Any]]:
    """The two trajectories side by side (longest common subsequence of
    ``kind:name``): rows ``{baseline, candidate, match}`` with step indexes,
    where ``match`` is ``same``, ``changed``, ``baseline_only`` or
    ``candidate_only``."""
    b = [(s.kind, s.name) for s in baseline]
    c = [(s.kind, s.name) for s in candidate]
    lengths = [[0] * (len(c) + 1) for _ in range(len(b) + 1)]
    for i in range(len(b) - 1, -1, -1):
        for j in range(len(c) - 1, -1, -1):
            lengths[i][j] = (
                lengths[i + 1][j + 1] + 1 if b[i] == c[j] else max(lengths[i + 1][j], lengths[i][j + 1])
            )
    rows: list[dict[str, Any]] = []
    i = j = 0
    while i < len(b) and j < len(c):
        if b[i] == c[j]:
            same = baseline[i].signature == candidate[j].signature
            rows.append({"baseline": i + 1, "candidate": j + 1, "match": "same" if same else "changed"})
            i, j = i + 1, j + 1
        elif lengths[i + 1][j] >= lengths[i][j + 1]:
            rows.append({"baseline": i + 1, "candidate": None, "match": "baseline_only"})
            i += 1
        else:
            rows.append({"baseline": None, "candidate": j + 1, "match": "candidate_only"})
            j += 1
    rows.extend({"baseline": k + 1, "candidate": None, "match": "baseline_only"} for k in range(i, len(b)))
    rows.extend({"baseline": None, "candidate": k + 1, "match": "candidate_only"} for k in range(j, len(c)))
    return rows
