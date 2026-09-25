# ADR-0018 — One definition of a retry

* Status: accepted · Date: 2026-09-25

## Context
Retries matter twice: they are a reliability signal, and a retried *write*
without an idempotency key is a policy violation
(`retry_without_idempotency_key`). Three places decided what a retry is, and
they disagreed. The demo agent set the `attempt` attribute from the number of
earlier calls of the same tool, so version 1.2.4 re-reading an order to confirm
a refund was labeled a retry of the first read. The trace summary and the web
UI each used their own rule on top.

## Decision
A tool call is a **retry** when
1. its `attempt` attribute is greater than 1, or
2. the previous call of the same tool had the same arguments (argument hash)
   and failed — result `error`, `timeout`, `rate_limited`, `denied` or `invalid`.

Agents set `attempt` to the number of consecutive failed calls with the same
tool and arguments, plus one. A call after a success, or with different
arguments, is a new call.

The rule is implemented three times, deliberately identically: the
trace-service summary (Go), the web span helpers (TypeScript) and the demo
agent (Python). Each has tests, and the Phase 1 e2e pins the case that
exposed the disagreement: the confirming re-read shows "Retries 0".

## Consequences
* A verify-after-write read (the safe pattern ADR-0017's `outcomeVerified`
  rewards) no longer counts as a retry or triggers a violation.
* The rule lives in three languages until the evaluation service owns trace
  features (Phase 3); a change must update all three and their tests.

## Addendum (2026-09-25, Phase 3)
The `maxRetries` scenario expectation counted every identical repeat of a
call, so it still called 1.2.4's confirming re-read a retry — the
disagreement this ADR removed from traces had survived in the evaluator. The
rule now also lives in `agenttwin_core.evaluators.signals.retries`, applied
to the twin's records: the previous call of the same tool had the same
arguments and failed. The twin's transport faults (`dropped`, `partial`,
`malformed`) reach the agent as unusable answers and count as failures; a
definitive `not_found` does not. The evaluator's version is `1.1.0` (1.0.0
counted repeats), and the baseline/candidate comparison counts retries with
the same function, so a verdict and a comparison never count one run
differently.
