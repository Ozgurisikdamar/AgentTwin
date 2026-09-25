"""Fault injection (spec §25, §85): ``FaultInjectingTwin(base_twin, rules)``.

A rule targets a tool (or ``*``) and fires when *all* of its ``when``
conditions hold (no ``when`` means every call)::

    callNumber: 2          the 2nd call of the target tool in the case
    callNumbers: [1, 3]
    firstN: 2              calls 1 and 2
    everyCall: true
    argsMatch: {order_id: ORD-1}   the arguments contain these values
    probability: 0.3       a seeded, reproducible draw per call

The first matching rule wins. Probabilistic rules draw from a pseudo-random
number derived from ``(seed, rule, tool, call number)``, so a scenario run
with the same seed injects the same faults regardless of call timing.

Behavior semantics (what the dependency did, what the caller sees):

==========================  ==================  ===================================
type                        state changed?      the caller sees
==========================  ==================  ===================================
timeout_before_mutation     no                  504 TIMEOUT (after ``delayMs``)
timeout_after_mutation      yes                 504 TIMEOUT (after ``delayMs``)
http_429 / rate_limit       no                  429 RATE_LIMITED + Retry-After
http_500                    no                  500 UPSTREAM_ERROR
http_503                    no                  503 UNAVAILABLE
transient_unavailable       no                  503 + Retry-After (retryable)
permanent_unavailable       no                  503 PERMANENTLY_UNAVAILABLE
non_retryable_error         no                  422 NON_RETRYABLE_ERROR
auth_denied                 no                  403 AUTH_DENIED
dropped_connection          no                  connection closed, no response
partial_response            yes                 status and half the body, then EOF
malformed_json              yes                 200 with a truncated JSON body
delay                       yes                 the normal reply after ``delayMs``
stale_response              yes (writes)        a reply computed from stale state
duplicate_response          yes                 the previous reply of the tool
semantic_bad_response       yes                 200 with ``body`` as the result
inconsistent_state          first effect only   500 INCONSISTENT_STATE
success_without_mutation    no                  the success reply
message_duplication         applied twice*      the first reply
==========================  ==================  ===================================

\\* the second delivery is a replay (no second effect) when the call carries
an idempotency key: exactly what an idempotency-safe agent relies on.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from agenttwin_simulation.twin.engine import (
    CallContext,
    DeclarativeTwin,
    FaultBehavior,
    Invocation,
    subset_match,
)
from agenttwin_simulation.twin.templates import check_template

__all__ = ["FAULT_TYPES", "FaultInjectingTwin", "FaultRule", "fault_problems", "parse_faults"]

FAULT_TYPES = (
    "timeout_before_mutation",
    "timeout_after_mutation",
    "http_429",
    "http_500",
    "http_503",
    "malformed_json",
    "semantic_bad_response",
    "delay",
    "dropped_connection",
    "partial_response",
    "stale_response",
    "duplicate_response",
    "auth_denied",
    "rate_limit",
    "transient_unavailable",
    "permanent_unavailable",
    "inconsistent_state",
    "success_without_mutation",
    "message_duplication",
    "non_retryable_error",
)


@dataclass(frozen=True)
class FaultRule:
    index: int
    target: str
    behavior: FaultBehavior
    call_number: int | None = None
    call_numbers: frozenset[int] | None = None
    first_n: int | None = None
    every_call: bool = False
    args_match: Mapping[str, Any] | None = None
    probability: float | None = None

    def applies(self, tool: str, arguments: Mapping[str, Any], call_number: int, seed: int) -> bool:
        if self.target not in ("*", tool):
            return False
        if self.call_number is not None and call_number != self.call_number:
            return False
        if self.call_numbers is not None and call_number not in self.call_numbers:
            return False
        if self.first_n is not None and call_number > self.first_n:
            return False
        if self.args_match is not None and not subset_match(self.args_match, arguments):
            return False
        if self.probability is not None:
            draw = random.Random(f"{seed}|{self.index}|{tool}|{call_number}").random()
            if draw >= self.probability:
                return False
        return True

    def to_json(self) -> dict[str, Any]:
        return {"index": self.index, "target": self.target, "type": self.behavior.type}


def parse_faults(raw: Sequence[Mapping[str, Any]] | None) -> list[FaultRule]:
    """Rules from a scenario's ``spec.faults`` (already schema-validated)."""
    rules = []
    for i, r in enumerate(raw or []):
        when = r.get("when") or {}
        b = r["behavior"]
        nums = when.get("callNumbers")
        rules.append(
            FaultRule(
                index=i,
                target=str(r["target"]),
                behavior=FaultBehavior(
                    type=str(b["type"]),
                    delay_ms=int(b["delayMs"]) if b.get("delayMs") is not None else None,
                    retry_after_s=float(b["retryAfterSeconds"])
                    if b.get("retryAfterSeconds") is not None
                    else None,
                    body=b.get("body"),
                    has_body="body" in b,
                    message=b.get("message"),
                ),
                call_number=when.get("callNumber"),
                call_numbers=frozenset(int(n) for n in nums) if nums is not None else None,
                first_n=when.get("firstN"),
                every_call=bool(when.get("everyCall", False)),
                args_match=when.get("argsMatch"),
                probability=float(when["probability"]) if when.get("probability") is not None else None,
            )
        )
    return rules


def fault_problems(raw: Sequence[Mapping[str, Any]] | None, tools: Sequence[str] | None = None) -> list[str]:
    """Semantic problems of fault rules (the JSON schema checks the shape)."""
    problems = []
    known = set(tools) if tools is not None else None
    for i, r in enumerate(raw or []):
        where = f"spec.faults[{i}]"
        target = str(r.get("target"))
        if known is not None and target != "*" and target not in known:
            problems.append(f"{where}.target: the twin has no tool {target!r}")
        behavior = r.get("behavior") or {}
        kind = behavior.get("type")
        if kind == "semantic_bad_response" and "body" not in behavior:
            problems.append(f"{where}.behavior: semantic_bad_response needs a body (the bad result)")
        if "body" in behavior and (problem := check_template(behavior["body"])):
            problems.append(f"{where}.behavior.body: {problem}")
    return problems


class FaultInjectingTwin:
    """Wraps a twin and injects faults into matching calls."""

    def __init__(self, base: DeclarativeTwin, rules: Sequence[FaultRule], seed: int) -> None:
        self.base = base
        self.rules = list(rules)
        self.seed = seed

    async def reset(self, initial_state: Mapping[str, Any] | None = None) -> None:
        await self.base.reset(initial_state)

    async def snapshot(self) -> dict[str, Any]:
        return await self.base.snapshot()

    def select(self, tool: str, arguments: Mapping[str, Any], call_number: int) -> FaultRule | None:
        return next((r for r in self.rules if r.applies(tool, arguments, call_number, self.seed)), None)

    async def invoke(self, tool_name: str, arguments: Mapping[str, Any], context: CallContext) -> Invocation:
        rule = self.select(tool_name, arguments, self.base.next_call_number(tool_name))
        return await self.base.invoke(tool_name, arguments, context, fault=rule.behavior if rule else None)
