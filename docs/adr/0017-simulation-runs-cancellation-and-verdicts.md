# ADR-0017 — Simulation runs: database queue, per-case credentials, cancellation and verdicts

* Status: accepted · Date: 2026-09-25

## Context
A simulation run executes many cases, each of which calls a customer agent
that may be slow, crash or misbehave (§23, §86). Workers die, events get lost,
users cancel. Whatever happens, a run must either finish with verdicts that
mean something or say clearly that it did not — never a silent pass.

## Decision
* **The database is the queue** (ADR-0006): a worker claims a `QUEUED` run with
  `FOR UPDATE SKIP LOCKED` and holds a renewable lease (60 s). The
  `simulation.run_requested.v1` event only wakes workers early; a lost event
  costs one poll interval (2 s). A janitor re-queues runs whose lease expired;
  after 3 attempts the run is `FAILED` with the reason.
* **Guarded lifecycle** `QUEUED → PREPARING → RUNNING → EVALUATING →
  COMPLETED | FAILED | CANCELLED`; every transition is checked against the
  current state and recorded in `simulation_run_transition` (the UI's
  lifecycle).
* **Per-case capability tokens.** Each case gets a fresh twin state and a random
  token (only its SHA-256 is stored) that the agent uses for the twin endpoint.
  The token is revoked when the case closes, *before* evaluation, so a late or
  replayed agent call cannot change what is being judged. Calls of one case are
  serialized by a row lock, in the order the twin records them.
* **Pinning for reproduction.** The run records the seed (random when not
  given), each case's seed (the scenario's own `spec.seed`, else derived from
  `sha256(run seed : scenario name)`), the agent version's manifest and prompt
  hashes and model, every scenario version and spec hash, every twin version and
  spec hash, the evaluator versions and the engine version.
* **Cancellation** is a request: `POST …/cancel` sets `cancel_requested`
  (202; 200 when the run is already final). While an agent call is in flight
  the worker checks every second; it then cancels the call, marks that case
  `CANCELLED` and every pending case too. An agent that finishes *after* the
  cancellation produced no verdict (the twin refused its calls from then on).
  A cancellation acknowledged before completion wins over `COMPLETED`: the final
  transition re-checks the flag under the run's lock.
* **Case verdicts are fail-safe:**
  `FAILED` if any expectation failed; otherwise `ERRORED` if one could not be
  evaluated, a critical expectation was skipped, nothing could be evaluated or
  the scenario has no expectations; otherwise `PASSED`.
* **Two different "critical" counts, named apart in the UI:** a case counts its
  failed *critical expectations*; a run counts its failed *critical-severity
  scenarios* (what blocks a release).
* **Verified outcomes flow back to the trace.** For each case the worker posts
  the verified outcome to the trace-service (the agent's own trace of that
  case). A trace that is not ingested yet is retried with a growing delay
  (3 s × attempt, capped at 60 s, up to 40 attempts).
* Run requests and completions are written through the transactional outbox
  (`simulation.run_requested.v1`, `simulation.run_completed.v1`).

## Consequences
* A crashed worker or lost event delays a run; it never loses it or passes it.
* Cancelled and errored cases are visible as such and never counted as passes.
* The per-case token bounds what a misbehaving agent can do to one case's
  state. Agent endpoints come from operator configuration
  (`SIMULATION_AGENT_ENDPOINTS`), never from a request, and the client still
  treats them as untrusted: no redirects, no proxy environment, bounded time
  and response size.
