# Implementation board

Tracks the phases of the build specification (§127). Each phase leaves working,
tested software and is committed separately. Status is updated as work lands.

| Phase | Scope | Acceptance | Status |
|---|---|---|---|
| 0 | Architecture, ADRs, glossary, threat model skeleton, monorepo, contracts, gokit | Docs + schemas + Makefile + compose base + shared Go kit (unit + integration tests green) | done |
| 1 | Walking skeleton: auth dev flow, projects/agents/versions, trace-service, OTel collector, Python SDK, demo agent, web trace page | `make dev` → demo agent trace visible in browser | done |
| 2 | Scenarios, declarative stateful tool twin, fault injection, simulation runs + UI | happy path and timeout-after-mutation scenarios run | done |
| 3 | Evaluators (deterministic, trajectory, judge adapter), datasets, eval runs, baseline vs candidate | candidate regression detected | pending |
| 4 | Graph, manifest/OpenAPI/MCP import, observed edges, change set, bounded blast radius, impacted selection | prompt/tool change selects refund scenarios with reasons | pending |
| 5 | Release, gate rules, immutable evidence, CLI CI output, release UI | bad candidate BLOCKED | pending |
| 6 | Regression miner: features, embeddings, grouping, taxonomy, inbox, promotion | demo failure → regression case → auto-included in next gate | pending |
| 7 | Runtime gateway: CEL policies, approvals, idempotency, SSRF-safe proxy, audit | over-limit refund requires approval; tampered args rejected | pending |
| 8 | Tenant isolation, SSRF, redaction, chaos, load, security, E2E, docs, screenshots | Definition of Done checklist | pending |

## Phase 1 evidence (2026-09-24)

Commands run on the Phase 1 commit, with their results:

| Command | Result |
|---|---|
| `make reset` (drop volumes, `make dev`: build, start, wait healthy, seed) | stack healthy in 89 s; 4 manifests registered, 40 conversations, 18 verified outcomes reported |
| `make demo` | one refund conversation through the demo agent; trace link printed |
| `make doctor` | 0 failed, 2 warnings (development placeholder secrets; events queued for the not-yet-built graph and evaluation services) |
| `make e2e` | 5/5 Playwright tests against the fresh stack (screenshots in `docs/screenshots/phase1-*.png`) |
| `make lint` | gofmt clean, `go vet` clean, golangci-lint 0 issues, ruff + ruff format + mypy (strict) clean, Prettier + ESLint + `tsc` clean |
| `make test` | Go: 101 tests + 81 subtests pass with real PostgreSQL + RabbitMQ (0 skipped); Python: 97 (SDK 67, demo 19, contracts check 11); web: 59 |
| `go test -race ./packages/... ./services/...` (integration) | all packages pass |
| `make contracts-check` | 14 event schemas compatible with the baseline (Phase 1 changes verified additive against Phase 0) |
| SDK overhead benchmark | tool span p50 ≈ 43 µs (content off), ≈ 91 µs (redacted) — `docs/benchmarks/sdk-overhead.md` |

## Phase 2 evidence (2026-09-25)

Commands run on the Phase 2 code, with their results:

| Command | Result |
|---|---|
| `make reset` (drop volumes, `make dev`: build, start, wait healthy, seed) | stack healthy and seeded in 100 s: 4 manifests, tool twin `demo-co-support` v1, 9 scenarios, 40 conversations, 18 verified outcomes reported |
| Seeded simulations (`seed --simulate 1.2.4,1.3.0`, seed 42) | **1.2.4: 9/9 passed**, including `refund-happy-path` and `refund-timeout-after-mutation`. **1.3.0: 6 passed, 3 failed** (`refund-timeout-after-mutation`: duplicate side effect + state mismatch; `refund-tool-success-lie`: hallucinated success; `refund-happy-path`: order violation), 2 critical scenarios failed |
| `make doctor` | 0 failed, 2 warnings (development placeholder secrets; events queued for the not-yet-built graph and evaluation services) |
| `make e2e` | 10/10 Playwright tests against the fresh stack: Phase 1 (5) and Phase 2 (5) — screenshots in `docs/screenshots/phase2-*.png` |
| `make lint` | gofmt clean, `go vet` clean, golangci-lint 0 issues, ruff + ruff format + mypy (strict, 62 files) clean, Prettier + ESLint + `tsc` clean |
| `make test` | Go: 102 tests + 81 subtests pass with real PostgreSQL + RabbitMQ (0 skipped); Python: 365 (core 125, simulation service 130, SDK 68, demo 31, contracts check 11); web: 101 |
| `make contracts-check` | 14 event schemas compatible with the baseline |

Phase 2 e2e coverage (`apps/web/e2e/phase2-simulations.spec.ts`): a regressed
version fails where it trusts a tool's false success, with the evidence down to
the tool call, the injected fault and the state diff, and a rerun with the same
seed reproduces the verdict; the known-good version passes every scenario; an
engineer authors, versions, runs, cancels and archives a scenario; a viewer is
read-only in the UI and gets 403 from the API.

## API contracts (2026-09-25)

`packages/contracts/openapi/simulation-service.openapi.yaml` — 17 operations
(14 public, 2 twin runtime, 1 webhook: the agent adapter contract) — is held to
the service by four checks (ADR-0021):

| Check | Result |
|---|---|
| Document | valid OpenAPI 3.1 (openapi-spec-validator 0.8.5); convention and unused-component checks pass |
| Routes | the 16 routes the service serves are exactly the 16 documented path operations, both ways |
| Traffic | the 20 simulation integration tests check every response strictly, every accepted request and every worker → agent call; the contract walk covers all 17 operations |
| Compatibility | `make contracts-check`: 14 event schemas and 1 API document (17 operations) compatible with the baselines |
| Mutation proofs | an undocumented response field (`tool_count`), query parameter (`name`) or agent request field (`run_context.case_id`), and an undocumented route (`capabilities`), each fail the tests; four breaking edits (request field removed, response field made optional, type changed, new required parameter) give 12 `BREAKING` lines, two compatible edits none |

Found and fixed while writing it: a twin's description was only returned in
lists; an agent's answer was stored with whatever types it sent; every
response the edge proxied carried its five headers twice (`X-Request-Id` and
the security headers) — a Go test reproduces it and fails without the fix.

Gates on this state: `make lint` clean (mypy strict, 63 files); `make test` —
Go 103 tests + 81 subtests with real PostgreSQL + RabbitMQ (0 skipped), Python
399 (core 134, simulation service 140, SDK 68, demo 31, contracts check 26),
web 101; `make dev` healthy and seeded (1.2.4 9/9; 1.3.0 6/9, 2 critical);
`make doctor` 0 failed; `make e2e` 10/10.

### Clients held to the contract (2026-09-25)

| Check | Result |
|---|---|
| Web types | `apps/web/src/lib/api/simulation.gen.ts` generated from the document (openapi-typescript 7.13.0); embedded documents (`x-agenttwin-schema`) typed from `scenario.v1` / `twin.v1`; a unit test and `make contracts-check` fail when it is stale |
| Web, compile time | every hand-written response type accepts the documented response of its operation (15 checks); request bodies, query parameters and the unit tests' fixtures are typed by the contract |
| Python fakes | the SDK's and the demo seed's test fakes answer with contract payloads (`agenttwin_core.api_fakes`) and check every exchange; the seed's run exercises `registerTwin`, `saveScenario`, `startSimulation`, `getSimulation` |
| Mutation proofs | UI type reading `verdict.score` as never null, an unknown body field (`priority`), an undocumented query parameter (`order`) and a fixture with a status the service never sends each fail `tsc`; a contract change (`reason` made optional) fails the drift test, then — regenerated — `tsc` names the field, and `make contracts-check` reports it; an SDK request with a renamed field and the demo fake's old partial twin answer each fail their tests |

Found and fixed: the UI read a verdict's score as always a number (`null`
when nothing was evaluated); evidence without a reference rendered an empty
element and the evaluators' expected and actual values were never shown; the
SDK's and the demo seed's fakes answered with shapes the service never sends.
The contract now documents exactly which expectation a result is for
(`id`, `type`, `critical` and four optional fields) and types a case's fault
rules by `scenario.v1#/$defs/fault`.

Gates on this state: `make lint` clean (mypy strict, 64 files); `make test` —
Go 103 tests + 81 subtests with real PostgreSQL + RabbitMQ (0 skipped), Python
409 (core 138, simulation service 140, SDK 71, demo 31, contracts check 29),
web 106; `make contracts-check` (events, API document, generated types);
`make dev` healthy and seeded (1.2.4 9/9; 1.3.0 6/9, 2 critical); `make doctor`
0 failed; `make e2e` 10/10.

### Control-plane and trace-service contracts (2026-09-25)

`control-plane.openapi.yaml` (29 operations: auth, projects, environments,
API keys, agents and versions, tools, audit, and the internal lookups the other
services call) and `trace-service.openapi.yaml` (8: the OTLP/HTTP export and
the list, facets, detail, delete, outcome, flag and stats routes) close the
Phase 1 gap: every service API now has a document, held to its service the
same four ways.

| Check | Result |
|---|---|
| Document | the three documents are valid OpenAPI 3.1 and follow the shared conventions (`scripts/tests/test_api_documents.py`, 11 tests: error responses per operation, `429` and the edge's `502`/`503`, `Idempotency-Key` with `409`/`422` on every authenticated mutation, closed request bodies, operation ids unique across documents, no unused components) |
| Routes | control plane: the routes it serves are exactly its 29 documented operations, both ways (the rest of `/api/v1/` is forwarded to the services that document it); trace service: its 8 operations, both ways |
| Traffic | a Go port of the checker (`packages/gokit/openapicheck`) wraps each Go service in its integration suite (`Contract.Checking`): every exchange is checked, strictly, and a full run fails when an operation never answered successfully (`TestAPIContractWalk` reaches the rest) |
| Compatibility | `make contracts-check`: 14 event schemas and 3 API documents (54 operations) compatible with the baselines; the three generated web type files up to date |
| Clients | web types generated from all three documents; compile-time checks for every type the UI reads, the explorer's URL filters (`Sends`: documented parameters, URL form, every enum value) and the fixtures; the Python fakes of all three services answer with contract payloads and `ExchangeChecker` holds each exchange to the document that owns its path |
| Mutation proofs | Go: an undocumented response field (`total` on the project list: two integration tests name `/total`), an undocumented route (`GET /api/v1/capabilities`: "30 routes served, 29 documented"), enum filters that accept any value (the checker names each off-contract parameter the service accepted: `status=ok`, `outcome=GREAT`, `source=prod`…), a strictified `if` (inverts the condition), an upper-case UUID written by the service ("not a canonical (lower-case) UUID"), case-sensitive project access, and the checker reporting only one side of an exchange — each fails a test. Web: an undocumented `source` value, a dropped `source`, an undocumented `page_size`, `tools` read from version list items, `me.user` read as required and a widened `next_cursor` each fail `tsc`. Python: the SDK stub's old manifest answer, the worker's off-contract `verification_source`, an echoed request blamed on the response, and five document conventions (open body, reused operation id, missing `Idempotency-Key`, unused component, missing `502`) each fail their tests |

Found and fixed in the services: the project list showed the gate policy to
members without `settings.read`; rotating an API key dropped its expiry and
could revive an expired key; a malformed path id reached the database; a
project name could be set empty or unbounded; a repeated tool registration
answered `201`; the audit list's last page had an empty-string cursor; revoking
a key required a body; the manifest envelope ignored unknown fields; `/me`
listed permissions as `null`; parameter errors named the field under another
key; the proxy forwarded `/api/v1/artifacts`, which no service serves; a `NaN`
or `Infinity` span attribute failed the whole OTLP export; negative token
counts and costs were stored; the `status`, `outcome` and `source` filters were
not validated; an upper-case project id was refused to a scoped key; text
limits counted bytes; expected/actual state accepted scalars; OTLP errors
carried the HTTP status as their code, were JSON even to a protobuf client, and
an unsupported content type or coding answered `400` instead of `415`; the
trace service's outcome and flag bodies were documented as open although the
service rejects unknown fields.

Found and fixed in the clients and checks: the agents page showed "0 tools"
for every version; a shared explorer link with a bare date or an unknown
`source` ended in `400`; the simulation service's fakes of the control plane
answered a manifest digest the control plane cannot produce and a `503` it
does not answer; the compatibility check skipped `head`, `options` and `trace`
operations.

Gates on this state: `make lint` clean (golangci-lint 0 issues, mypy strict 64
files, ESLint, `tsc`); `make test` — Go 131 tests + 81 subtests with real
PostgreSQL + RabbitMQ (0 failed, 0 skipped), Python 425 (core 144, simulation
service 138, SDK 71, demo 31, repository checks 41), web 110 (13 files);
`make contracts-check` (events, 3 API documents, generated types); `make dev`
healthy and seeded (1.2.4 9/9; 1.3.0 6/9, 2 critical); `make doctor` 0 failed;
`make e2e` 12/12 (Phase 1: 7, Phase 2: 5).

## Phase 3 progress (2026-09-25)

Pieces landed so far, each with its tests and the mutations they catch
(a mutation is applied to the source, the service's tests run, the source is
restored; "caught" means the tests failed):

| Piece | Tests | Mutations caught |
|---|---|---|
| Comparison and first divergence (`agenttwin_evaluation.comparison`, `.trajectory`) — case classes, metric deltas, slices, lockstep first divergence with plain-language impact | 33 (comparison 16, trajectory 17), property tests included | all mutations tried on the classification and divergence rules; three that first survived (effect ignored in the step signature, a repeated write called a double effect when nothing applied, a check made earlier reported as skipped) were closed with tests |
| Judges (`.judges`) — Anthropic (forced `record_verdict` tool) and OpenAI-compatible (strict JSON schema) adapters replayed through their wire formats, verdict validation, quotes held to the material shown, retries with `retry-after`, truncation, the deterministic fake | 26 | 13 of 13 (e.g. unsupported quotes accepted, 429 not retried, `retry-after` ignored or uncapped, verdict tool not forced, refusal or truncation not detected, prices swapped, delimiters forgeable) |
| Semantic grading (`.semantic`) — label *and* threshold, judge errors as `ERROR`/`EVALUATION_ERROR`, no-answer fails without a call, budget, cache, judge selection; every result valid against the simulation contract's `ExpectationResult` | 24 | 12 of 12 (e.g. score alone decides, threshold exclusive, a judge error becoming FAIL with score 0, budget or unknown-cost calls not counted, redeliveries shown to the judge) |
| Calibration (`.calibration`) — agreement, Cohen's kappa, confusion matrix, judge errors as disagreements, thresholds (20 examples, 80%, 0.6) | 14, property test included | 5 of 5 |
| Simulation pairs (simulation service `POST /internal/v1/simulation-pairs`, ADR-0023) — the baseline and candidate runs in one transaction over one pinned suite; one pair per evaluation run (a repeat answers `200`, a different request `409 PAIR_CONFLICT`, five concurrent requests make one pair); service callers only; public runs are single runs | 7 integration tests on PostgreSQL, and the contract walk checks the operation's `201`, `200`, `403` and `409` against the document | 9 of 9 — one survived at first (the candidate's cases could get their own seeds while both runs still *pinned* one suite); closed by comparing the stored cases of both sides |
| Datasets (evaluation service `evaluation` schema, `/api/v1/datasets`) — versioned, immutable case lists naming the project's scenarios (checked with the simulation service), production cases only when redacted, changes serialized on the dataset row (nine concurrent adds make nine versions, none lost), archiving, each case's latest completed result; every change announced as `audit.recorded.v1` with before/after hashes | 10 integration tests on PostgreSQL, every exchange checked against the new `evaluation-service.openapi.yaml` (and the service's calls against the simulation contract); route parity | 22 of 22 (e.g. unredacted production cases accepted, scenarios unchecked, unchanged cases re-attributed, archived datasets changed, changes unserialized, LIKE wildcards unescaped, failed runs counted as results, other organizations served, the audit naming the service) |
| Evaluation runs (`/api/v1/eval-runs`, `agenttwin_evaluation.worker`, ADR-0024) — queued, prepared (the pair), waiting without a lease (checked when due, woken by `simulation.run_completed.v1`, bounded by a deadline), evaluated (every case of both sides read, semantic expectations judged with budget and cache, compared, summarized, `evaluation.run_completed.v1` in the completing transaction); cancellation, lease recovery, a dataset pinning the suite | 12 integration tests over the **real** simulation service and demo agent on PostgreSQL — the golden path reproduces the Phase 2 live check (1.2.4 → 1.3.0, seed 42: two new critical failures, one regression, six unchanged; first divergence at step 2 on `refund_payment`); every exchange checked against the evaluation, simulation and trace contracts | 21 of 21 — one survived at first (a baseline's completion event found its run only through the event's eval run id); closed by a test with a producer that does not name the run |
| Human review and judge calibration (`/api/v1/eval-runs/{id}/cases/{scenario}/reviews`, `/api/v1/reviews`, `/api/v1/judges`, `agenttwin_evaluation.reviewing`) — a person's verdict replaces one expectation's result and the case and run are classified again from the stored snapshots (latest review counts, audited, the run's own `agentRun` finding not overridable); the review queue (judge errors, skips, critical verdicts of an uncalibrated criterion), newest run first; calibrations per project, criterion and judge identity — the latest completed one counts — with a kept lease and a bounded retry; ERRORED cases whose only gap is a skipped semantic expectation are judged | 6 integration tests over the real simulation service and demo agent + 4 rule tests; the contract walk covers all 17 operations (`uncovered() == []`); a drift test holds the document's criteria to the judge's | 32 of 32 — three survived at first (a stored review of the run finding applied, one case's reviews applied to another, ERRORED verdicts not recomputed); closed by rule tests and a two-case review test. The drift test also found a real bug: `rubric` was listed twice |

Found on the way and fixed: the `maxRetries` expectation disagreed with
ADR-0018 (it counted a verify-after-write re-read as a retry) — evaluator
version 1.1.0, see the ADR's addendum. Design: ADR-0022.

Live check of the comparison on the Phase 2 stack (1.2.4 against 1.3.0, seed
42): `refund-timeout-after-mutation` and `refund-tool-success-lie` are new
critical failures, `refund-happy-path` regressed (policy check skipped), six
cases unchanged; the timeout case first diverges at step 2, where the
candidate calls the irreversible `refund_payment` without the policy lookup
the baseline made there.

Gates on this state: `make lint-py` clean (ruff, ruff format, mypy strict 79
files); `make lint-web` clean; Python 559 passed on the test infrastructure
(real PostgreSQL and RabbitMQ) — evaluation service 121, simulation service
145; `make contracts-check`: 14 event schemas and 4 API documents (66
operations) compatible with the baselines, generated web types up to date.

## Definition of Done tracking

See the final delivery report in `docs/delivery-report.md` (written at the end;
lists the exact commands and test counts that were run).
