# ADR-0019 — Idempotency at the edge, one key per user action

* Status: accepted · Date: 2026-09-25

## Context
Starting a simulation, saving a scenario version or archiving it must not
happen twice because a button was double-clicked or a request was retried after
a timeout. The specification requires `Idempotency-Key` on mutating APIs. The
server side has to be uniform across services, and the clients have to choose
keys in a way that actually prevents duplicates without replaying stale results.

## Decision
**Server — once, at the edge.** The control plane's middleware handles
`Idempotency-Key` for every mutating request, including those proxied to
internal services, so no service reimplements it:
* keys are 8–128 characters of `[A-Za-z0-9._:-]`, scoped to the principal
  (organization and actor);
* the request fingerprint is the method, the request URI and the body;
* a repeat of the same request returns the stored response with
  `Idempotent-Replayed: true`;
* the same key with a different request is rejected (422
  `IDEMPOTENCY_KEY_REUSED`); a key whose first request is still running gets
  409 `IDEMPOTENCY_IN_PROGRESS`;
* 2xx and 4xx responses are stored; a 5xx or a lost response abandons the key,
  so a retry may execute;
* records expire after 24 hours.

**Clients — one key per user action.** The web app generates a random key
(`crypto.getRandomValues`) for each action and
* keeps it while the outcome is unknown (network error, 5xx), so retrying the
  same action can never execute it twice;
* replaces it after a definite answer (success or 4xx), so the next action —
  even with an identical body — is a new one.

Keys derived from the content are rejected as a design: saving A, then B, then
A again would replay the first save instead of creating a version. The Python
SDK passes caller-chosen keys through (`start_simulation(idempotency_key=…)`).

## Consequences
* Double submits and blind retries are harmless everywhere behind the edge.
* Internal service-to-service calls do not go through the edge and rely on
  their own idempotency (outbox, `processed_event`, run state checks).
