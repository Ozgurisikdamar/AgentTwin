# ADR-0005 — Embedded CEL policy engine instead of an external policy platform

* Status: accepted · Date: 2026-09-24

## Context
Runtime policies decide allow / allow_with_limits / require_approval / deny for tool
actions ("deny amount > 500", "require approval amount > 100", "max 1 call per trace").
They must be versioned, validated before activation, testable and auditable.

## Decision
Policies are CEL (Common Expression Language, `cel-go`) expressions evaluated
in-process by the runtime-gateway. A policy version is a list of ordered rules
`{name, when: <CEL bool>, effect, message}` plus a fail mode. CEL is non-Turing-complete,
side-effect free and has bounded evaluation cost (a cost limit is configured).
Activation requires successful compilation, type checking against the declared
activation, and passing the policy's own fixtures. Numeric comparisons are extracted
from the CEL AST to generate boundary test values (e.g. 99/100/101 for `> 100`).

## Upgrade trigger
OPA/Rego or a policy service when policies must be shared across many non-AgentTwin
enforcement points or need data-heavy decisions.

## Consequences
No extra infrastructure; policy latency is microseconds; the same evaluator is used by
`policy test`, activation checks and the live gateway.
