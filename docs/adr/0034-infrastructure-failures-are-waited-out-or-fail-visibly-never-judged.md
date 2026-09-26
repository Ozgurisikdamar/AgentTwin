# ADR-0034 — Infrastructure failures are waited out or fail visibly, and are never judged

* Status: accepted · Date: 2026-09-26

## Context
Spec §64 lists the failures AgentTwin must survive: RabbitMQ unavailable,
duplicate and delayed messages, database connection loss, a worker crash in
the middle of a run, a provider's 429, timeout or malformed JSON, a tool
twin crash and an evaluator exception. For each: no silent corruption,
bounded retries, a visible status, recovered or clearly failed, and never a
false PASS — "infrastructure uncertainty must fail safe".

Injecting each failure (a TCP proxy that cuts or silences the real
PostgreSQL and RabbitMQ of the test infrastructure, not mocks) showed the
earlier behaviour missed that bar in a dozen places. Among them:

* a readiness probe hung 30 s during a network partition, and a consumer
  reconnecting after an outage leaked a connection per attempt;
* a database outage answered 500 (a bug, as far as a caller can tell) after
  waiting up to 10 s per request, and the Python services took up to a minute
  to serve again once it was back;
* a consumer whose database was gone for ten seconds used up its five
  attempts and parked the event in a dead-letter queue, where nobody reads it;
* a simulation run was FAILED by a database blip; an evaluation run hit by
  a bug stayed EVALUATING and was retried for no reason;
* a tool twin that crashed answered the agent 500, and the case was then
  judged as if the twin had behaved: an agent that handled the error
  gracefully could pass;
* an older policy, scenario or catalog declaration arriving after a newer
  one replaced it in the dependency graph; a late ingestion snapshot of a
  trace withdrew a failure an outcome recorded afterwards had revealed.

## Decision
Every failure is classified as one of three kinds, and each kind has one
behaviour everywhere.

**1. An outage (the database or the broker cannot be reached): wait it out,
visibly and boundedly.**
* HTTP: a request that meets an unreachable database answers `503
  UNAVAILABLE` with `Retry-After: 2`, within seconds — never a 500, never a
  hang. Go classifies with `db.IsUnavailable` (connection errors, network
  errors, the `08`/`57P01-3`/`53300` SQLSTATEs; never a cancelled context),
  Python with `is_unavailable`. Waits are bounded: connecting takes at most
  5 s; the Python pool stops making callers wait 10 s once it knows the
  database is unreachable (1 s) and reconnects with a backoff capped at 2 s,
  so it serves again within seconds of the database's return, without a
  restart.
* Events: publishing goes through the transactional outbox, so an event
  waits there (`agenttwin_outbox_backlog`) while the broker is down and is
  delivered once it is back. Consumers reconnect with a backoff of 0.5 s to
  10 s that starts over once they consume again; readiness answers within
  its own timeout during a partition.
* A consumer whose handler meets an unreachable database **defers** the
  event through the retry queue instead of spending an attempt: attempts
  count failures of the event, deferrals count the outage (the
  `x-agenttwin-deferred` header). After an hour of deferrals the event is
  parked, saying the database stayed unavailable.
* A worker that loses its database mid-run leaves the run to its lease (the
  janitor puts it back in the queue) instead of failing it. A simulation
  worker that cannot watch its case any more also cancels the agent call.

**2. A defect (a bug in our code, a crash of the twin or an evaluator):
fail at once and say so; never retry, never judge.**
* A worker hit by an unexpected exception fails its run with `internal error
  (<Type>)`; the details go to the log with the request or run id.
* A tool twin that cannot answer a call answers `500 INTERNAL` (with the
  request id), changes nothing, and records the failure on the case before
  the agent gets the answer. The worker then errors the case (`TWIN_ERROR`)
  and skips its expectations: the agent reacted to a failure no scenario
  asked for, so its behaviour proves nothing either way.
* An evaluator that raises becomes an `ERROR` result for its expectation
  (`EVALUATOR_ERROR`); any ERROR makes the case ERRORED; the other
  expectations are still judged.

**3. A dependency that answers badly (a provider's 429, timeout or garbage;
an agent that hangs or answers something that is not a result): bounded
retries where retrying can help, then a clear failure.**
* Providers (judges, embeddings): `Retry-After` is honoured as seconds or an
  HTTP date, clamped to [0, 10 s]; an unreadable header falls back to the
  caller's backoff; attempts are bounded and the error names its kind
  (`rate_limited`, `timeout`, `unavailable`). A malformed answer is not
  retried and never becomes a score.
* Agents: a hung agent costs its case the case timeout (`AGENT_TIMEOUT`,
  FAIL); HTML, truncated JSON, the wrong shape or a 5xx is `AGENT_ERROR`
  (FAIL). The run completes; nothing passes.

**Delivery is at least once and in any order**, so every consumer is
idempotent (a `processed_event` row or a natural key in the same
transaction) and order-independent where order matters:
* a declaration (a policy activation, a scenario version, a tool catalog)
  records when it was made (`graph.declaration`); one older than the
  declaration already applied is ignored whole;
* an ingestion event carries a snapshot of the trace, which is only trusted
  to say "nothing to do": a mining that would create or change an occurrence
  reads the trace again (it is finalized once announced, so what is read is
  never older than the snapshot);
* the audit log keeps arrival order with the time each event happened; the
  hash chain is serialized per organization.

## Consequences
* `make test-chaos` runs, item by item, the tests that inject each failure of
  §64 (`scripts/chaos-tests.yaml`); a missing or renamed test fails the
  manifest check. `make chaos-drill` does the same to the running stack.
* A 503 from AgentTwin always means "retry later"; a 500 always means a bug,
  with a request id to find it.
* A database outage longer than the consumers' hour of deferrals parks
  events. They stay in the dead-letter queue (and in `make doctor`) for an
  operator to replay; the runbook says how.
* A twin failure is recorded with a few quick retries while its database is
  unreachable. An outage long enough to defeat them is also seen by the
  worker, which polls the same database every second while a case runs and
  abandons the case to its lease — so the case is run again rather than
  judged.
* Ordering guarantees are per use, not global: consumers still run with a
  prefetch of four, and a new consumer must decide whether it needs one.
