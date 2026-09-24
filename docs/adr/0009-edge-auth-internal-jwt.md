# ADR-0009 — Edge authentication at the control-plane + internal service JWT

* Status: accepted · Date: 2026-09-24

## Context
Six services, one public API. Authentication and RBAC must be consistent, and a single
bug must not expose another tenant's data.

## Decision
* The control-plane is the only public API entry. It authenticates
  * **OIDC** bearer tokens in production (`AUTH_MODE=oidc`, JWKS discovery; any
    OIDC provider — Keycloak, Entra ID, Okta),
  * a **dev session token** in local development (`AUTH_MODE=dev`, seeded users,
    HS256; the endpoint does not exist when `AUTH_MODE=oidc`; startup refuses
    `AUTH_MODE=dev` with `APP_ENV=production`),
  * **project API keys** (`atk_<prefix>_<secret>`, SHA-256 HMAC with a server pepper at rest,
    shown once, scoped, revocable, expiring, last-used tracking).
* It forwards requests to owning services with a 60-second **internal JWT**
  (`aud`=service, claims: org, project scope, role, actor, request id).
* Every service verifies the internal JWT and scopes every query by organization
  (and project where applicable). Tenant-isolation tests run against each service.
* The browser holds only an HttpOnly cookie; the Next.js server attaches the token.

We do not build an identity provider. Keycloak is not bundled locally because the dev
flow keeps onboarding to a single command; the OIDC path is covered by tests that use a
local JWKS test server.

## Consequences
RBAC is implemented once. Internal services cannot be called without a token even on
the internal network. PostgreSQL row-level security is a documented next step
(defense in depth), not a V1 dependency.
