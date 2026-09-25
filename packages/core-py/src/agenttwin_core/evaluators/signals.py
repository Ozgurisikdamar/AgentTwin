"""Trajectory signals shared by the expectation evaluators and the
baseline/candidate comparison.

A verdict ("at most one retry") and a comparison ("the candidate retried
twice, the baseline never") must count the same run the same way, so each
signal has exactly one definition, here.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence

from agenttwin.hashing import canonical_json
from agenttwin_core.evaluators.model import ToolCall

__all__ = [
    "FAILED_STATUSES",
    "WRITE_RISKS",
    "agent_calls",
    "duplicate_side_effects",
    "escalations",
    "failed",
    "policy_violations",
    "retries",
]

# Results after which repeating the same call is a retry (ADR-0018): the five
# failures an agent can see in a tool span, plus the transport faults a twin
# injects, which reach the agent as an unusable answer. A definitive answer
# ("not_found") is not a failure to retry.
FAILED_STATUSES = frozenset(
    {"error", "timeout", "rate_limited", "denied", "invalid", "dropped", "partial", "malformed"}
)
WRITE_RISKS = frozenset({"WRITE_REVERSIBLE", "WRITE_IRREVERSIBLE", "EXECUTE", "ADMIN"})


def failed(call: ToolCall) -> bool:
    return call.status in FAILED_STATUSES


def agent_calls(
    calls: Iterable[ToolCall], tool: str | None = None, *, redeliveries: bool = False
) -> list[ToolCall]:
    """Calls the agent made (optionally of one tool). A network redelivery of a
    call (message duplication fault) is a side effect, not a decision of the
    agent, so it only counts where ``redeliveries`` is requested."""
    return [c for c in calls if (tool is None or c.tool == tool) and (redeliveries or not c.redelivered)]


def retries(calls: Sequence[ToolCall], tool: str | None = None) -> list[ToolCall]:
    """The calls that were retries (ADR-0018): the previous call of the same
    tool had the same arguments and failed. Re-reading after a success — the
    verify-after-write pattern — is a new call, not a retry."""
    last: dict[str, tuple[str, bool]] = {}
    out: list[ToolCall] = []
    for c in agent_calls(calls):
        key = canonical_json(dict(c.arguments))
        previous = last.get(c.tool)
        if previous is not None and previous == (key, True) and (tool is None or c.tool == tool):
            out.append(c)
        last[c.tool] = (key, failed(c))
    return out


def duplicate_side_effects(calls: Sequence[ToolCall], tool: str | None = None) -> dict[str, list[ToolCall]]:
    """Side effects applied more than once, by effect key (or by tool and
    arguments when the twin gave the effect no key). Redeliveries count: a
    duplicated message that applies a refund twice is a double refund."""
    groups: dict[str, list[ToolCall]] = defaultdict(list)
    for c in agent_calls(calls, tool, redeliveries=True):
        if c.mutated and not c.replayed:
            groups[c.effect_key or f"{c.tool}:{canonical_json(dict(c.arguments))}"].append(c)
    return {k: v for k, v in sorted(groups.items()) if len(v) > 1}


def policy_violations(calls: Sequence[ToolCall]) -> list[ToolCall]:
    return [c for c in agent_calls(calls) if c.policy_violation]


def escalations(calls: Sequence[ToolCall], tools: Iterable[str]) -> list[ToolCall]:
    """Escalations that went through (answered or applied)."""
    names = set(tools)
    return [c for c in calls if c.tool in names and (c.succeeded or c.mutated)]
