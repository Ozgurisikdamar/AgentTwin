# Threat model

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

## Attack paths → mitigations

The last column names the item of [`scripts/security-tests.yaml`](../../scripts/security-tests.yaml)
that lists the tests (`uv run python scripts/spec_tests.py security --list`).

| Attack | Mitigation | Tests (manifest item) |
|---|---|---|
| Cross-tenant read by ID guessing (BOLA) | org scoping in every query, internal JWT naming the caller's projects, UUIDs; foreign ids answer 404 | `bola`, `cross-tenant-reads` |
| Stolen API key | hashed at rest, scoped, revocable, expiring, last-used | `api-key-scopes`, `expired-revoked-key` |
| Approval replay / argument tampering | single-use token bound to the action hash and the agent | `approval-replay`, `approval-tampering` |
| SSRF via tool URL | allowlist, private-range and metadata block, redirect re-validation, dial-time IP check | `ssrf` |
| SQL injection | parameterized queries only; text no column can store refused at the edge | `sql-injection` |
| Prompt injection via trace content rendered in UI | no HTML rendering of content, escaped text only, nonce CSP | `xss-trace-content`, `malicious-markdown` |
| Prompt injection into the agent or the judge | escalation instead of obedience in the demo agent; release suites run the injection scenarios; judge delimiters cannot be forged | `prompt-injection` |
| Malicious YAML/OpenAPI/MCP | safe loaders, size/depth caps, no fetching, no code execution, annotations are claims | `malicious-openapi`, `malicious-mcp`, `oversized-payload` |
| Poison queue message | schema validation, bounded retry, parking DLQ | `poison-message` |
| Path traversal in artifacts | nothing stored by name; names that become paths or keys refuse traversal | `path-traversal` |
| Secret leakage into logs/traces | client-side redaction, server re-redaction, log field redaction | `secret-redaction` |

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
| Cross-tenant write by naming another organization's project id | A service cannot tell which organization a project belongs to: the edge names a person's projects in the internal token, never "all projects" (ADR-0025); an organization has at most 100 projects so the token stays bounded; the graph's keys include the organization. | `TestTheEdgeNamesOnlyTheCallersProjects`, `TestAPersonsTokenNamesTheirOrganizationsProjects`, `TestNoProjectListNoForwarding`, `TestAnOrganizationHasBoundedProjects`, `TestTokensNameABoundedNumberOfProjects`, graph `TestProjectsAndComponentsOfOthersAreInvisible` |
| Malicious OpenAPI document or MCP tool list (import) | Untrusted input (spec §111): the control plane fetches nothing — a `$ref` outside the document is recorded, never followed, and an MCP server is never contacted; cycles end in a marker, reference bombs stop at a node bound; depth, size (2 MiB document, 64 KiB per schema), text and counts are bounded; control characters are removed; examples (often real data) are dropped; server URLs lose credentials and query strings. | catalog `TestHostileDocumentsAreBounded`, `TestAWideReferenceBombEndsQuickly`, `TestOpenAPIOperationsBecomeTools`, control-plane `TestImportRequestsAreValidated` |
| An imported description understates risk ("read-only" `DELETE`, `readOnlyHint` on a destructive tool) | MCP annotations are claims: recorded with the risk they suggest, applied only with `trust_annotations`; otherwise a tool is `WRITE_IRREVERSIBLE`. A write method recorded as `READ` is warned about. Every entry says where its risk came from. An import never overwrites a tool an agent manifest declares, nor one another source defines. | catalog `TestMCPToolsBecomeEntries`, `TestTrustedAnnotationsAndOverrides`; control-plane `TestAnMCPImportRecordsHintsAndTrustsThemOnlyWhenAsked`, `TestAnImportDoesNotTakeAnotherSourcesTool` |
| Prompt text disclosed through a change set's diff | The diff is redacted like captured content and shown only to callers with `settings.read`, as a version's prompt text is; others see that the prompt changed and which tools it names. | control-plane `TestAPromptDiffIsShownOnlyToWhoMayReadPrompts`, changes `TestSecretsInAPromptDiffAreMasked` |
| Similarity search crossing tenants, or echoing what it was asked | A scenario's embedding row carries its organization and project under a foreign key to the scenario's own, so a vector cannot be stored under another tenant; a match compares only vectors of the caller's project, of the model, dimension and text recipe in use, of latest and active scenarios. The answer names a query by its id and never repeats its text (it may be a prompt the caller could not otherwise read). | simulation `test_the_schema_keeps_an_embedding_in_its_scenarios_tenant`, `test_only_current_vectors_of_the_project_are_compared`, `test_a_change_selects_scenarios_and_says_why`, `test_access_and_validation` |
| A change impact asking other services beyond the caller's rights | The control plane asks the graph and simulation services with the caller's own identity narrowed to the change set's project (never a service identity, never every project); it needs read access; a key of another project or without `read` is refused. Query text sent for similarity is the stored, masked diff, and the answer never repeats it. A service that fails makes the impact `complete: false` with the reason, never a silent partial suite. | control-plane `TestAChangeSetsImpactSelectsScenariosAndSaysWhy`, `TestTheImpactStaysInItsProject`, `TestAViewerReadsTheImpactWithoutThePromptText`, `TestAnImpactWithoutAServiceIsIncompleteNotAnError`, `TestAnImpactCutShortSaysSo` |
| Forged service-to-service calls | Internal APIs require a 60 s HS256 JWT with audience = target service; client credentials are stripped when the control plane proxies. | `TestInternalAPIRequiresServiceToken`, `TestRequireInternalMiddleware`, `TestVerifyRejections`, `TestProxyForwardsWithInternalTokenAndStripsClientCredentials` |
| Stolen or revoked API key | Keys are stored as peppered hashes, scoped (`traces:write`, `read`, `ci`, `runtime:invoke`, `policies:deploy`), expiring and revocable. Ingestion caches verification for 30 s, so **a revoked key is rejected within 30 s**; rejections are cached 10 s; a control-plane outage yields a retryable 503, never an accept. | `TestAPIKeyLifecycleAndScopes`, `TestIngestAuthentication`, `TestRevocationTakesEffectWithinTheCacheTTL`, `TestTransientFailuresAreNotCached` |
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
* Phases 2–7 added their controls with the features (simulation, evaluation,
  graph, releases, regressions, runtime gateway); Phase 8 maps every item of
  spec §63 to its tests below.

## Security tests (spec §63)

Every item of §63 is covered by named, automated tests. The mapping is
machine-readable, [`scripts/security-tests.yaml`](../../scripts/security-tests.yaml),
and cannot rot silently: `scripts/tests/test_spec_manifests.py` (part of
`make test`) fails when an item is missing or a named test no longer exists.

```bash
make test-security        # every §63 test, against real PostgreSQL + RabbitMQ
make test-security-live   # the same plus the browser checks on a running stack (make dev)
uv run python scripts/spec_tests.py security --list   # the items and their tests
```

Integration tests cannot skip there (`AGENTTWIN_REQUIRE_INTEGRATION=1`): an
item whose test would skip fails instead.

## Controls added in Phase 8

| Threat | Control | Evidence |
|---|---|---|
| SQL injection | Every query is parameterized (pgx, asyncpg; no string-built SQL). Checked from outside: every GET operation of the six API contracts, with every path and query parameter set in turn to 13 payloads (tautology, stacked statement, `UNION`, `pg_sleep`, `LIKE` wildcards, JSON operators, path traversal, CRLF, 10 000 characters, NUL, invalid UTF-8, overlong encoding, lone surrogate) answers without a 5xx, a leaked database error or a delay; the demo project is intact afterwards. | e2e `security.spec.ts` ("every read endpoint answers injection payloads…": 58 operations, 2 093 requests) |
| Text a database cannot store (NUL, invalid UTF-8, lone surrogate) | Found by that walk: 44 filters of all six services failed inside PostgreSQL with a 500. Every service now refuses such text in the path, the query and JSON bodies with 400 `INVALID_TEXT`, before routing (Go `httpx.ValidText` in the standard handler, a NUL in a body before decoding; Python the same in `agenttwin_core.web`); agent manifests and gateway arguments, read outside the common decoder, check it too. Telemetry is cleaned instead of refused (U+FFFD), because one span with a NUL used to fail the insert of its whole batch and the collector finally dropped it, other traces included. | `TestValidTextRefusesWhatNoColumnStores`, `TestDecodeJSON`, `TestHasNUL`, `TestTheStandardHandlerRefusesUnstorableText`, `test_text_no_column_stores_is_refused_in_path_and_query`, `test_text_no_column_stores_is_refused_in_bodies`, `TestManifestTextMustBeStorable`, `TestArgumentsWithANulAreRefusedBeforeADecision`, `TestTextNoColumnCanStoreIsReplaced` (+ both OTLP fuzzers), `TestTextNoColumnStoresIsCleanedNotRefused`; e2e "every write endpoint refuses a NUL in its body before acting on it" |
| XSS through trace content, Markdown or a stored URL | Content is React text, never markup: no `dangerouslySetInnerHTML`, `innerHTML`, `eval` or Markdown/HTML renderer in the web app; a stored URL (override ticket, CI run) becomes a link only when it is `http(s)`, and opens with `rel="noopener noreferrer"`. | `untrusted-content.test.tsx`; e2e "hostile text in a trace is shown as text and never runs" (an agent run whose input carries `<script>`, `onerror`, `javascript:` links and a template expression: shown character for character, nothing runs, no dialog, no CSP violation) |
| An expired API key | Refused everywhere once `expires_at` passes: the public API (401, without echoing the key), internal verification (`valid: false`) and rotation (409 `API_KEY_EXPIRED`). | `TestAnExpiredKeyIsRefusedEverywhere` |
| Denial of service through an infrastructure outage (a database or broker gone, a crashing twin or evaluator, a provider flooding 429s) | Outages are waited out boundedly and visibly (503 + `Retry-After`, the outbox, deferred events, run leases); defects fail at once; nothing an outage or a crash leaves undecided is ever judged a PASS (ADR-0034). | `make test-chaos` (`scripts/chaos-tests.yaml`, spec §64: 10 items), `make chaos-drill` against the running stack |
| A 500 without a request id (Python services) | An unhandled error is answered inside the request-id middleware, so the body and the `X-Request-Id` header carry the same id and the standard headers. | `test_unhandled_errors_do_not_leak` |

## Residual risk (Phase 8)

* **Invalid UTF-8 in OTLP protobuf.** The protobuf decoder refuses a batch
  whose string fields are not UTF-8 (400, which the collector does not retry);
  NUL characters, which are valid UTF-8, are cleaned. The Python SDK cannot
  produce such bytes; an SDK in another language could.
* **Path traversal in artifact names** has no surface: the platform stores no
  files by name (evidence and reports are rows and generated downloads), and
  the BFF and the gateway normalize paths (see the tests in the manifest).
