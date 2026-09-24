# ADR-0013 — No Redis in V1

* Status: accepted · Date: 2026-09-24

## Decision
Caches (API-key verification, JWKS) are bounded in-process TTL caches. Rate limiting is
per-instance token buckets keyed by API key / IP. Sessions are stateless tokens.

## Upgrade trigger
Redis when a measured requirement appears: cross-instance rate limiting with multiple
control-plane replicas, shared cache hit-rate problems, or session revocation lists
that must be global within seconds.
