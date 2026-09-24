# Threat model (skeleton — completed in Phase 8)

Scope: the AgentTwin platform itself and the runtime gateway that sits in front of
customer tools.

## Assets
* Tenant data: traces, redacted content, outcomes, scenarios, datasets, failures.
* Governance data: gate decisions, overrides, approvals, audit log.
* Credentials: API keys (hashed), session/OIDC tokens, internal service secret,
  provider API keys (environment/secret manager only).
* Control over real side effects: the runtime gateway forwards tool actions.

## Trust boundaries
1. Browser ↔ Next.js BFF ↔ control-plane (public edge).
2. Agent SDK ↔ OTel collector ↔ trace-service (untrusted telemetry content).
3. control-plane ↔ internal services (internal JWT).
4. Simulation worker ↔ agent-under-test endpoint (untrusted agent behavior).
5. Runtime gateway ↔ customer tools / arbitrary URLs (outbound, SSRF risk).
6. Imported artifacts: scenario YAML, OpenAPI documents, MCP metadata (untrusted input).

## Actors
Anonymous internet user · authenticated tenant user (per role) · compromised API key ·
malicious agent output / retrieved document / tool result · malicious MCP server ·
insider with DB access.

## Attack paths → mitigations (to be expanded)
| Attack | Mitigation | Test |
|---|---|---|
| Cross-tenant read by ID guessing (BOLA) | org scoping in every query + internal JWT + UUIDs | tenant isolation suites per service |
| Stolen API key | hashed at rest, scoped, revocable, expiring, last-used | security tests |
| Approval replay / argument tampering | single-use token bound to action hash | runtime-gateway tests |
| SSRF via tool URL | allowlist, private-range block, redirect re-validation, dial-time IP check | ssrf tests |
| Prompt injection via trace content rendered in UI | no HTML rendering of content, escaped text only | XSS tests |
| Malicious YAML/OpenAPI/MCP | safe loaders, size/depth caps, no code execution | parser tests + fuzz |
| Poison queue message | schema validation, bounded retry, parking DLQ | queue tests |
| Path traversal in artifacts | content-addressed keys, path normalization | artifact tests |
| Secret leakage into logs/traces | client-side redaction, log field allowlist | redaction property tests |

## Residual risk
To be completed.
