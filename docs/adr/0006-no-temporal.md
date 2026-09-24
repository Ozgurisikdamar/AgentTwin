# ADR-0006 — No Temporal in V1

* Status: accepted · Date: 2026-09-24

## Context
Simulation and evaluation runs take seconds to minutes. Approvals pause a single
runtime action, not a long workflow.

## Decision
Runs are persisted state machines (`QUEUED → PREPARING → RUNNING → EVALUATING →
COMPLETED | FAILED | CANCELLED`) driven by RabbitMQ messages. Workers claim runs
with a lease (`lease_until`) and heartbeat; an expired lease makes the run
re-claimable or, after max attempts, FAILED. Invalid transitions are rejected in code
and by a guarded SQL update. Cancellation is a flag checked between cases.

## Upgrade trigger
Temporal when workflows routinely live for hours/days, human steps must pause/resume
across deployments, or retry/resume logic becomes hard to maintain with this model.

## Consequences
A crashed worker never leaves an ambiguous success: completion is a single
transaction that writes results and the terminal state together.
