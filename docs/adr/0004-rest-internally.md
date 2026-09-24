# ADR-0004 — REST/JSON between services instead of gRPC

* Status: accepted · Date: 2026-09-24

## Context
Internal synchronous calls are few (blast radius, scenario selection, fetching run
results, API-key verification) and low-volume. Two languages (Go, Python) must
interoperate; the public API is already REST/JSON with OpenAPI.

## Decision
Internal calls are REST/JSON under `/internal/v1/*`, authenticated with the internal
service JWT, with explicit timeouts, bounded retries for idempotent calls and
cancellation propagation. Contracts are documented in OpenAPI and covered by
contract tests.

## Upgrade trigger
gRPC when an internal path becomes latency-critical at high QPS or needs streaming.

## Consequences
One serialization format, one tooling set, debuggable with curl.
