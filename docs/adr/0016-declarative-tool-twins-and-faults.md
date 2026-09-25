# ADR-0016 — Declarative, stateful tool twins with seeded fault injection

* Status: accepted · Date: 2026-09-25

## Context
Simulations run the real agent against stand-ins for its tools (§24, §25, §85).
The stand-in has to behave like the dependency *including the parts that go
wrong*: a refund that times out after the money moved is a different failure
from one that times out before, and "the provider said succeeded but recorded
nothing" is a third. The verdict must rest on what actually happened to the
tool state, so the stand-in has to be stateful, and a failing run must replay
bit-for-bit when someone investigates it.

## Decision
* **A twin is a document, not code.** `TwinDefinition` (twin.v1 JSON Schema)
  declares tools (name, risk tier, argument schema) and a handler per tool:
  `read` / `mutate` (conditions and effects on a JSON state), `static`, `echo`,
  `recorded` fixtures, or `custom` — a named adapter registered in the service,
  never code carried by the document. Loading rejects unknown handler kinds,
  invalid templates and state over 1 MiB before anything runs.
* **Per-case isolated state.** Each case starts from the twin's fixture state
  merged with the scenario's `spec.state` (JSON merge patch). No state is shared
  between cases or runs.
* **The twin records every call itself**: arguments, reply, HTTP status, fault,
  the state changes it caused, whether it mutated, whether it was a replay.
  Trajectory evidence therefore cannot be forged by the agent under test
  (ADR-0011).
* **Deterministic engine.** Same definition, initial state, calls and fault
  rules → same replies, records and final state. Generated ids come from the
  call sequence; the clock is fixed (`2026-01-01T00:00:00Z`, overridable with
  `input.context.now`).
* **Idempotency is modeled.** A mutation repeated with the same idempotency key
  replays the first reply without a second effect — the property a safe agent
  relies on after a timeout.
* **Faults are rules** `{target, when, behavior}`. `when` combines `callNumber`,
  `callNumbers`, `firstN`, `everyCall`, `argsMatch` and `probability`; the first
  matching rule wins. Probabilistic draws come from
  `hash(seed, rule, tool, call number)`, so the same seed injects the same
  faults regardless of timing.
* **Each fault type states what the dependency did and what the caller sees**
  (the table in `twin/faults.py`), e.g. `timeout_after_mutation` = state changed,
  caller sees 504; `success_without_mutation` = state unchanged, caller sees
  success; `message_duplication` = delivered twice, the second is a replay when
  the call carries an idempotency key.
* **Delays model the dependency, not the twin:** the case lock is released
  before sleeping, capped at 30 s (`max_fault_delay_ms`).

## Consequences
* Scenarios express production-shaped failures precisely, and the expectation
  "success must be backed by state" (`outcomeVerified`) can tell a lie from a
  success.
* A seed plus pinned versions (ADR-0017) reproduces a run exactly; the UI's
  "Run again" relies on it and the Phase 2 e2e checks it.
* Behavior that the declarative handlers cannot express needs a reviewed adapter
  in the service. That is deliberate: twins are user-authored input (§112) and
  must never execute.
