# ADR-0024 — Evaluation runs wait for their simulations without a lease

* Status: accepted · Date: 2026-09-25

## Context
An evaluation run (spec §27) spends almost all of its life waiting: its two
simulations run for minutes, its own work — starting them, then reading,
judging and comparing every case — takes seconds. The job state machine
(ADR-0006) gives an active run a lease that a worker renews; a run whose
lease expires goes back to the queue, at most `max_run_attempts` times. Held
through the wait, a lease either has to be renewed by a worker doing nothing
(one stuck coroutine per waiting run) or it expires and burns attempts on a
run that is fine.

## Decision
* **Only the active steps hold a lease.** `QUEUED → PREPARING` (claim, lease)
  starts the pair; `PREPARING → RUNNING` *releases* the lease and sets
  `next_check_at` and `wait_deadline`; `RUNNING → EVALUATING` takes a lease
  again (any worker); `EVALUATING → COMPLETED` releases it. The janitor only
  requeues runs whose lease expired, so a waiting run is never requeued.
* **A waiting run is checked when due.** Workers take due runs with
  `FOR UPDATE SKIP LOCKED` and move them to their next check, so two workers
  never check the same run. `simulation.run_completed.v1` makes the run due at
  once (found by its evaluation run id or either simulation run id); polling
  is the fallback, never the only path.
* **Every step is repeatable.** The pair is keyed by the evaluation run
  (ADR-0023), so a retried `PREPARING` gets the same simulations; judge
  verdicts are cached by everything that shaped them (ADR-0022); case results
  are replaced whole in the transaction that completes the run and writes
  `evaluation.run_completed.v1`. A worker that lost its lease cannot end the
  run: terminal transitions of active steps are guarded by the lease owner.
* **Waiting is bounded.** Past `EVALUATION_MAX_WAIT` both simulations are
  cancelled and the run fails with that reason; a cancellation request is
  carried out by the next check (the simulations are cancelled too).
* **The result can be read back without the simulation service.** Each case
  stores the comparison and, per side, the simulation's case detail (without
  the scenario document), the merged expectation results and the judge
  verdicts — what a review needs to re-grade a case.

## Consequences
* A dead worker costs at most one lease period of delay, and only while it
  was preparing or evaluating.
* A simulation cancelled by someone else fails the evaluation ("the candidate
  simulation was cancelled"); a simulation that *failed* is still compared —
  its cases are `INCOMPLETE`, which says more than a failed run would.
* The judge budget is shared by the cases evaluated concurrently
  (`EVALUATION_FETCH_CONCURRENCY`): it can be exceeded by at most the calls
  in flight when it runs out, and the run reports it as exhausted.
* Stored snapshots grow with the suite (bounded by 500 cases per run).
