# Threat model (extended phase by phase; completed in Phase 8)

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

## Controls implemented in Phase 1 (walking skeleton)

Each row names the control as built and the automated test that exercises it
against real PostgreSQL/RabbitMQ (Go integration tests) or the real browser
stack (Playwright).

| Threat | Control | Evidence |
|---|---|---|
| Token theft through XSS | The browser never holds a bearer token: the web BFF keeps the session in an `HttpOnly; SameSite=Lax` cookie (`Secure` behind TLS) and adds the `Authorization` header server-side. Trace content is rendered as escaped text, never as HTML. Nonce-based CSP (`strict-dynamic`, `frame-ancestors 'none'`). | `bff-route.test.ts` ("stores the session in an HttpOnly SameSite cookie and never returns the token", "marks the cookie Secure behind TLS"); e2e "the session cookie is HttpOnly…" (asserts `document.cookie` cannot read it) and every e2e test fails on CSP violations |
| Cross-site request forgery | Unsafe methods through the BFF require `Sec-Fetch-Site: same-origin` (fallback: `Origin` equals `Host`); rejected before the control plane is contacted. Login uses the same check. | `bff.test.ts`, `bff-route.test.ts` ("rejects cross-site writes before contacting the control plane", "rejects cross-site logins"); e2e POST with `Origin: http://evil.example` → 403 |
| BFF used to reach other upstream paths | Only `/api/v1/*` is forwarded; `..`, empty, very deep or very long paths are rejected; request bodies capped at 16 MiB; client cookies are not forwarded upstream. | `bff.test.ts` ("rejects very deep or very long paths"), `bff-route.test.ts` ("rejects path traversal", "filters headers") |
| Stale session after logout / expiry | Logout deletes the cookie; any upstream 401 clears it and sends the user to sign-in. Dev sessions expire after 12 h. | e2e "signing out ends the session"; `bff-route.test.ts` ("drops the session cookie…"); `TestSessionExpiry` |
| Cross-tenant data access (BOLA) | Organization comes from the verified credential; every SQL query is scoped by organization and accessible projects; unknown or foreign IDs answer 404, not 403. | `TestRBACAndTenantIsolation`, `TestRBACMatrix`, `TestExplorerFiltersPaginationAndIsolation` (list, facets, stats of another org or project are empty / 404), `TestRequireProjectHidesOtherProjects` |
| Forged service-to-service calls | Internal APIs require a 60 s HS256 JWT with audience = target service; client credentials are stripped when the control plane proxies. | `TestInternalAPIRequiresServiceToken`, `TestRequireInternalMiddleware`, `TestVerifyRejections`, `TestProxyForwardsWithInternalTokenAndStripsClientCredentials` |
| Stolen or revoked API key | Keys are stored as peppered hashes, scoped (`traces:write`, `read`, `ci`, `runtime:invoke`), expiring and revocable. Ingestion caches verification for 30 s, so **a revoked key is rejected within 30 s**; rejections are cached 10 s; a control-plane outage yields a retryable 503, never an accept. | `TestAPIKeyLifecycleAndScopes`, `TestIngestAuthentication`, `TestRevocationTakesEffectWithinTheCacheTTL`, `TestTransientFailuresAreNotCached` |
| Tenant spoofing through telemetry | Tenant and project come only from the verified API key, never from span or resource attributes; the collector batches per key so tenants are never merged. | `TestIngestAuthentication`, `TestContentPolicyIsEnforcedPerProject` |
| Hostile or oversized telemetry | Body and decompressed-size limits (gzip bombs), bounded ingest concurrency (503 → collector retries), attribute/identifier truncation with valid UTF-8, out-of-vocabulary enums normalized, a poison trace retried at most 10 times without blocking others, claim errors reported instead of silently dropped. | `TestGzipAndBombLimit`, `TestBackpressureAndProtobufAndGzip`, `TestTruncateKeepsValidUTF8`, `TestHostileAttributesAreBoundedAndNormalized`, `TestFinalizerIsolatesPoisonTraces`, `TestFinalizerReportsClaimErrors` |
| Sensitive content stored against policy | Content capture is off by default; the trace service re-applies the project's content mode (it never stores more than allowed, whatever the SDK sent); secrets are masked even in `full` mode; content has its own retention and is purged. | `TestOffDropsAllContent`, `TestRedactedRequiresClientSideRedaction`, `TestFullKeepsPIIButNeverSecrets`, `TestContentNeverLeaksIntoExtra`, `TestContentPolicyIsEnforcedPerProject`, `TestRetentionPurges`, `TestSharedRedactionFixture` (Go/Python parity) |
| Secrets in logs | Structured logger redacts sensitive attribute names; API keys never appear in SDK errors or `repr`. | `TestLoggerRedactsSensitiveAttributesAndAddsRequestID`; SDK `test_errors_never_leak_the_key` |
| Tampering with the audit log | Hash-chained audit entries; verification detects modification. | `TestAuditChainDetectsTampering` |
| Development settings in production | `APP_ENV=production` refuses dev authentication, demo bootstrap and `change-me` placeholder secrets; `make doctor` reports them. | `TestProductionRefusesDevelopmentAuth`, `TestProductionRejectsPlaceholderSecrets` |

## Residual risk (after Phase 1)

* **Revocation latency.** A revoked API key can still ingest for up to 30 s
  (cache TTL). Lowering `API_KEY_CACHE_TTL` trades control-plane load for
  latency; the cache must stay enabled because every OTLP batch is verified.
* **Web sessions are bearer tokens in a cookie.** A dev session token is valid
  until expiry (12 h); there is no server-side session revocation list yet.
  Production uses OIDC with short-lived tokens.
* **CSP allows inline style attributes** (`style-src-attr 'unsafe-inline'`) for
  the waterfall bar geometry. Scripts remain nonce-bound; style injection
  cannot execute code.
* To be completed in Phase 8 with the runtime gateway, SSRF, artifact and
  import surfaces.
