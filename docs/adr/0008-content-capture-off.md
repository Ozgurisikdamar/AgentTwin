# ADR-0008 — Content capture off by default

* Status: accepted · Date: 2026-09-24

## Context
Agent traces can contain customer PII, secrets and business data. Server-side
redaction alone is insufficient for sensitive environments.

## Decision
* `captureContent=false` is the default in both SDKs and in project settings. Only
  metadata (names, hashes, counts, timings, statuses) is exported.
* `redacted` mode exports content after **client-side** redaction (email, phone, JWT,
  API keys/bearer tokens, card-like numbers, custom regex, JSON-path rules) with
  `drop`, `mask` or `hash` strategies.
* `full` must be enabled explicitly per project and environment.
* Tool arguments are always hashed (`agenttwin.tool.args_hash`) so equality can be
  compared without content.
* The trace-service enforces the project setting again (drops content it is not
  allowed to store) and truncates oversized values.

## Consequences
Some features (production replay input, semantic judging of real outputs) require
`redacted` or `full` mode; the UI explains which features are degraded and why.
