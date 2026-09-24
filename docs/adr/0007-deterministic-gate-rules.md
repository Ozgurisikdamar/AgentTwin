# ADR-0007 — Deterministic gate rules before learned risk prediction

* Status: accepted · Date: 2026-09-24

## Context
A release gate must be explainable. There is no labeled history of
"release → production incident" yet, so any learned risk model would be uncalibrated.

## Decision
The gate decision (PASS / WARN / BLOCK) is produced by explicit, versioned,
table-tested rules over evaluation evidence (critical scenario failures, policy
violations, duplicate irreversible side effects, missing mandatory results, cost and
latency deltas, coverage gaps). Every triggered rule carries machine-readable evidence
references. A secondary 0–100 **rule-based risk index** is shown for sorting only; its
formula and per-factor contributions are displayed and it is never called a probability.
LLM judges may block only if their calibration agreement meets the configured minimum.

## Future
`docs/future-ml.md` defines the feature set and label (release caused incident /
material regression) to collect now; a learned model is trained only after enough
labeled outcomes exist and even then remains advisory.
