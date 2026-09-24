# ADR-0011 — Framework-neutral agent adapter and a labeled deterministic planner

* Status: accepted · Date: 2026-09-24

## Context
Simulations must run the customer's agent against tool twins. CI must be deterministic
and must not require LLM credentials.

## Decision
* **Agent adapter contract**: `POST {agent_endpoint}/run` with the input, the twin
  tool base URL and run context; the agent calls tools over HTTP. Tool calls are
  recorded **by the twin**, not self-reported by the agent, so trajectory evidence
  cannot be forged by the agent under test. An in-process adapter exists for tests.
* **Demo agent models**: `ScriptedPlannerModel`, a deterministic stand-in for an LLM that
  follows the directives written in the agent's system prompt (e.g. "Always call
  get_refund_policy before refund_payment"). It is labeled `deterministic-fake` in run
  records and the UI. An Anthropic adapter implements the same interface for real runs.
* Changing the candidate's system prompt therefore changes behavior deterministically,
  which makes the release-gate demo reproducible.

## Consequences
The demo proves the pipeline (change → blast radius → simulation → divergence → gate),
not the quality of a particular LLM. Real-model runs are opt-in and marked as
nondeterministic in the run's pinning report.
