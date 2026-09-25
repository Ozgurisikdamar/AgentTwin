# ADR-0023 — The baseline and the candidate run as one pinned pair

* Status: accepted · Date: 2026-09-25

## Context
A baseline/candidate comparison (spec §27) is only meaningful when the agent
version is the one thing that differs between the two sides: "run identical
scenario initial states for baseline/candidate". Two independent
`POST /api/v1/simulations` calls cannot promise that. Between them a scenario
can get a new version, a twin can be re-registered, or the default random
seed can differ — and a fault that fires on one side and not the other would
read as a regression of the agent. Phase 2 had anticipated evaluation runs
with `eval_run_id` and `side` fields on the public run request, which let any
caller attach runs to an evaluation, one side at a time.

## Decision
**One internal operation creates both runs.** The evaluation service calls
`POST /internal/v1/simulation-pairs` on the simulation service (internal token,
service principals only) with the project, the evaluation run id, the agent,
the baseline and candidate versions, the selection (scenario names or tags),
an optional seed and the requesting user. The simulation service:

* resolves both agent versions (`AGENT_VERSION_NOT_FOUND` names the side);
* resolves the selection **once** — scenario versions and the twin each runs
  against — and builds the cases of both runs from that one plan, with the
  same case seeds (derived from one run seed);
* writes the pair row, both runs, their cases and both
  `simulation.run_requested.v1` events in **one transaction**;
* records each run's side and its counterpart in the run's pinning, so either
  run names the other.

The agent version is then the only difference; both versions may even be the
same, which measures how repeatable a version is.

**One pair per evaluation run.** The pair row is keyed by the evaluation run id
and written first in the transaction; a concurrent request waits on the key
and then finds the pair. A repeat of the request (the evaluation worker
retrying after a crash) answers the existing pair with `200` and
`created: false`; a different request for the same evaluation run — told apart
by a hash of the request, where the order and duplicates of the selection do
not count — is `409 PAIR_CONFLICT`. A refused request claims nothing.

**Public runs are single runs.** `POST /api/v1/simulations` keeps `eval_run_id`
and `side` in its request schema (removing request fields is breaking,
`packages/contracts/README.md`), but refuses them unless they are unset (`side`
`SINGLE`) with `400 INVALID_PARAMETER`: only the pair operation creates the runs
of an evaluation.

## Consequences
* The comparison (ADR-0022's judges, the case classes of the evaluation
  service) reads two runs that pinned the same suite, and can say so: the
  pinned scenario list is equal on both sides.
* The evaluation service needs no lock of its own to stay idempotent; the pair
  key is the lock.
* Pairs live in the simulation service (`simulation_pair`); the evaluation
  service keeps the two run ids it was given.
