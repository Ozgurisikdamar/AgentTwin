# Implementation board

Tracks the phases of the build specification (§127). Each phase leaves working,
tested software and is committed separately. Status is updated as work lands.

| Phase | Scope | Acceptance | Status |
|---|---|---|---|
| 0 | Architecture, ADRs, glossary, threat model skeleton, monorepo, contracts, gokit | Docs + schemas + Makefile + compose base + shared Go kit (unit + integration tests green) | done |
| 1 | Walking skeleton: auth dev flow, projects/agents/versions, trace-service, OTel collector, Python SDK, demo agent, web trace page | `make dev` → demo agent trace visible in browser | done |
| 2 | Scenarios, declarative stateful tool twin, fault injection, simulation runs + UI | happy path and timeout-after-mutation scenarios run | done |
| 3 | Evaluators (deterministic, trajectory, judge adapter), datasets, eval runs, baseline vs candidate | candidate regression detected | done |
| 4 | Graph, manifest/OpenAPI/MCP import, observed edges, change set, bounded blast radius, impacted selection | prompt/tool change selects refund scenarios with reasons | done |
| 5 | Release, gate rules, immutable evidence, CLI CI output, release UI | bad candidate BLOCKED | done |
| 6 | Regression miner: features, embeddings, grouping, taxonomy, inbox, promotion | demo failure → regression case → auto-included in next gate | done |
| 7 | Runtime gateway: CEL policies, approvals, idempotency, SSRF-safe proxy, audit | over-limit refund requires approval; tampered args rejected | done |
| 8 | Tenant isolation, SSRF, redaction, chaos, load, security, E2E, docs, screenshots | Definition of Done checklist | in progress |

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

## Phase 3 evidence (2026-09-25)

Acceptance — *candidate regression detected*: on a stack built from scratch,
the seeded evaluation of 1.3.0 against 1.2.4 finds it, and the Phase 3 e2e
repeats it through the UI. Commands run on the Phase 3 code, with their
results:

| Command | Result |
|---|---|
| `make reset` (drop volumes, `make dev`: build, start, wait healthy, seed) | stack healthy and seeded in 90 s: 4 manifests, tool twin `demo-co-support` v1, 9 scenarios, simulations of 1.2.4 (9/9 passed) and 1.3.0 (6 passed, 3 failed, 2 critical), dataset `refund-regression-suite` v1 (9 cases), 40 conversations, 18 verified outcomes reported |
| Seeded evaluation (`seed --evaluate 1.2.4:1.3.0`, seed 42) | **COMPLETED: 2 new critical failures** (`refund-timeout-after-mutation`, `refund-tool-success-lie`), **1 regressed** (`refund-happy-path`), 6 unchanged, 0 incomplete |
| `make doctor` | 0 failed, 2 warnings (development placeholder secrets; `graph-service.events` waiting for the Phase 4 consumer); the evaluation service and worker ready, 1 migration applied, the demo project has a completed evaluation run |
| `make e2e` | 16/16 Playwright tests against the fresh stack: Phase 1 (7), Phase 2 (5), Phase 3 (4) — 1.3.0 against 1.2.4 with the first divergence and a reviewer's verdict; 1.3.1 improves three cases on 1.3.0 and matches 1.2.4 on all nine; a dataset versioned to v3, evaluated and archived; a judge criterion calibrated from 20 labeled examples. Screenshots in `docs/screenshots/phase3-*.png` |
| `make lint` | gofmt clean, `go vet` clean, golangci-lint 0 issues, ruff + ruff format + mypy (strict, 82 files) clean, Prettier + ESLint + `tsc` clean |
| `make test` | Go: 131 tests + 81 subtests pass with real PostgreSQL + RabbitMQ (0 skipped); Python: 580 (core 147, simulation service 145, evaluation service 133, SDK 72, demo 37, contracts and document checks 46); web: 149 |
| `make contracts-check` | 14 event schemas and 4 API documents (72 operations) compatible with the baselines; the four generated web type files up to date |

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
| Wiring (compose `evaluation-service` + `evaluation-worker`, control plane `EVALUATION_SERVICE_URL`, image target, Prometheus, `make doctor`; SDK `datasets`/`create_dataset`/`add_dataset_cases`/`start_eval_run`/`wait_for_eval_run`; `make seed --evaluate 1.2.4:1.3.0` keeps the `refund-regression-suite` dataset and evaluates on it; three demo scenarios gain non-critical semantic expectations) | SDK 11 tests and seed 12 tests, every exchange checked against the evaluation contract; whole suite 578; `make reset` from scratch: seed exit 0 — evaluation COMPLETED, NEW_CRITICAL_FAILURE 2 (`refund-timeout-after-mutation`, `refund-tool-success-lie`), REGRESSED 1 (`refund-happy-path`), UNCHANGED 6; the judge grades `reply-admits-unconfirmed-refund` PASS for 1.2.4 and FAIL for 1.3.0; a second seed reuses dataset v1; `make doctor` 0 failed; `make e2e` 12/12 | — (wiring; the behaviour behind it is covered above) |
| Web UI (`/evaluations`, `/evaluations/new`, `/evaluations/{id}`, `/evaluations/{id}/cases/{scenario}`, `/datasets`, `/datasets/new`, `/datasets/{id}`, `/reviews`, `/judges`) — what the candidate breaks first, counts as filters, both sides' totals, slices, every case worst first, "Run again" on the same pinned suite and seed; the compared case with first divergence, expectations side by side, metric deltas, aligned trajectories and a reviewer's verdict; versioned datasets; the review queue; calibration from pasted human labels. The pages read the contract's own types and the tests' live payloads are typed by it | web 149 (evaluation helpers 15, evaluation components 10, datasets 8 + 2 parser, reviews and judges 5); `apps/web/e2e/phase3-evaluations.spec.ts` 4 tests against the running stack — `make e2e` 16/16 (Phase 1: 7, Phase 2: 5, Phase 3: 4), screenshots in `docs/screenshots/phase3-*.png` | 36 of 36 (helpers and components 15, datasets 10, reviews and judges 7, caches dropped when a run finishes or a case is reviewed 4); two survived at first (the create test never sent an invalid tag; the filter test's tag also matched the name), both closed |

Found on the way and fixed: the `maxRetries` expectation disagreed with
ADR-0018 (it counted a verify-after-write re-read as a retry) — evaluator
version 1.1.0, see the ADR's addendum. Design: ADR-0022.

The web work found three more: the contract documented a value change as
add/remove/replace (the service sends `added`/`removed`/`changed`) and a
side's status as free text that could be null (it is a case status or
`MISSING`) — both are enums now and the contract walk rejects anything
outside them; the compatibility check called every change of a field's JSON
types breaking — it now follows the direction of the data (a response may
narrow, a request may widen, an event may do neither). The e2e run found a
fourth: after an evaluation finished, the dataset page kept a copy cached
before it and said "not evaluated yet" — a finished run and a review now
drop the dataset's, the run list's and the review queue's cached copies.

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

## Phase 4 evidence (2026-09-25)

Acceptance: *a prompt or tool change selects the refund scenarios and says
why.* It holds on a stack built from scratch, through the API (the seed) and
through the UI (the Phase 4 e2e tests). Commands run on the Phase 4 code, with
their results:

| Command | Result |
|---|---|
| `make reset` (drop volumes, then `make dev`: build, start, wait until healthy, seed) | Stack healthy and seeded in 90 s. Seeded: 5 manifests (1.2.3 to 1.3.2); tool twin `demo-co-support` v1; 9 scenarios; 3 tool catalogs (OpenAPI `payments-api` r1 and `orders-api` r1, MCP `support-desk` r1, 3 tools each; the manifests' tools kept); 2 change sets; simulations of 1.2.4 (9/9 passed) and 1.3.0 (6 passed, 3 failed, 2 critical); dataset `refund-regression-suite` v1; the evaluation of 1.3.0 against 1.2.4 (2 new critical failures, 1 regressed, 6 unchanged); 40 conversations |
| Change impact, prompt change 1.2.4 → 1.3.0 | **complete**, **9 scenarios required**. The item: "prompt modified; changed lines mention get_refund_policy, lookup_order, refund_payment". **7 scenarios test `refund_payment`** (the six refund scenarios and `cross-tenant-order`). Each is linked by the graph ("tests tool refund_payment, which the change to the prompt reaches in 2 steps") and is close to the change, with text similarity 0.28 to 0.50. `refund-happy-path` is also linked through `get_refund_policy` and `lookup_order`. `malicious-retrieved-content` and `unauthorized-admin-tool` run because they are tagged `security`, and are linked only weakly, through the agent. Blast radius: prompt → support-refund-agent@1.3.0 → refund_payment → payments-api. Irreversible actions within reach: `refund_payment` and `export_customer_data` |
| Change impact, tool change 1.3.1 → 1.3.2 | **complete**, **9 scenarios required**. The item: "tool refund_payment: description changed; 2 schema changes (2 breaking)". The same 7 scenarios are required, each because it "tests tool refund_payment, which changed". Four of them are also close to the change, with text similarity 0.27 to 0.41. The two other security scenarios run because they are tagged `security`. Irreversible action: `refund_payment` |
| `make doctor` | 0 failed, 1 warning (development placeholder secrets). Services checked: the graph service ready, all migrations applied (control plane 3, graph 1, simulation 3), no event waiting without a consumer. `make doctor` now also checks the graph schema; see "Found on the way" below |
| `make e2e` | 20/20 Playwright tests against the fresh stack. Phase 1: 7, Phase 2: 5, Phase 3: 4, Phase 4: 4. The Phase 4 tests cover: the prompt change and its reasons; the required scenarios opened as a simulation of the candidate; the tool change; the graph opened on a change's blast radius, with a component's evidence, the evidence filter and the component search; an engineer's manual mapping round trip; a viewer's read-only view. Screenshots in `docs/screenshots/phase4-*.png` |
| `make lint` | gofmt clean; `go vet` clean; golangci-lint 0 issues; ruff, ruff format and mypy strict (84 files) clean; Prettier, ESLint and `tsc` clean |
| `make test` | Go: 245 tests and 141 subtests pass with real PostgreSQL and RabbitMQ (0 failed, 0 skipped). Python: 666 (core 194, simulation service 168, evaluation service 133, SDK 73, demo 49, contracts and document checks 49). Web: 214 (22 files) |
| `make contracts-check` | 14 event schemas and 5 API documents (86 operations) compatible with the baselines. The five generated web type files are up to date, including the new `graph.gen.ts` |

## Phase 4 progress (2026-09-25)

Pieces landed, each with its tests and the mutations they catch (same
method as Phase 3: a mutation is applied to the source, the tests run, the
source is restored; "caught" means the tests failed):

| Piece | Tests | Mutations caught |
|---|---|---|
| Graph domain and bounded blast radius (`graph-service/internal/graph`, ADR-0026) — components, edges, evidence sources and their confidence; up/down traversal with depth, node budget and version scope; deterministic scores, severities and the path to every affected component; scenarios, policies and evaluators linked with a reason | 17 traversal tests and 7 neighbourhood tests, including properties over random graphs of every kind and edge type, run over 20,000 graphs once (depth bounded, every path step an edge in its direction, the same result whatever order the store answers in, a deeper bound never loses a component, scores in range, no version outside the scope) | 27 of 27 — nine survived a first run: three rules proved unnecessary and were removed, the other six got tests |
| The graph from events, its API and contract (`ingest`, `api`, the consumer) — manifests, catalog imports, production traffic (counted), scenarios, policies and manual mappings; evidence per source and reference; one project's writes serialized; unmappable events parked | 11 ingest tests; 14 integration tests on PostgreSQL + the contract walk, every exchange checked against `graph-service.openapi.yaml`; route parity; the consumer's handlers held to the queue's bindings | 13 of 13. Live: the 196 events the running stack had queued for the graph consumed, none parked |
| The edge names a person's projects (ADR-0025) — found by the graph's tests: another organization's project id could be written to | proxy unit tests, an end-to-end integration test through the edge, the 100-project limit under 10 concurrent creations, the size of a 100-project token | 5 of 5. Live: another organization's owner gets `404` from the demo project's graph, datasets and scenarios |
| Change-set domain (`control-plane/internal/changes`, ADR-0027) — prompt with masked line diff and the tools the changed lines name, model and parameters, limits, tools (risk escalation as a new privilege, input-schema changes breaking or not per spec §118), retrieval sources, dependencies, code by file names, declared changes; seeds scoped to the candidate | 16 (demo manifests: 1.2.4 → 1.3.0 changes only the prompt, whose changed lines name `get_refund_policy`, `lookup_order`, `refund_payment`; a synthetic pair covering every kind; table tests of schema and text diffs; bounds on untrusted schema depth and size) | 25 of 25 — two survived a first run and got two more tests |
| Change sets stored and served — `release.write`, prompt diff only with `settings.read`, content-addressed, immutable, composite keys | 9 integration tests through the contract checker (content, repeat, visibility, validation, paging, tenancy, project keys, schema invariants, concurrency) and the migration rollback test | 16 of 16 |
| Tool catalogs read from OpenAPI 3.0/3.1 and MCP `tools/list` (`internal/catalog`, ADR-0028) — names, input schemas, risk (override, `x-agenttwin-risk`, method; MCP hints only when trusted), hostile documents bounded | 11, including hostile documents (external references, cycles, reference bombs, depth, size) | 19 of 19 |
| Catalog import API — registry outcomes (`kept_manifest`, `kept_other_source`), immutable revisions, `tool.catalog_imported.v1` | 6 integration tests through the contract checker (outcomes, revisions, JSON and YAML, MCP hints trusted only on request, tenancy, project keys, concurrency) | 15 of 15. Live: `TOOL refund_payment -CAN_MUTATE→ HTTP_API payments-api -DEPENDS_ON→ SERVICE payments-api` after an import through the edge |
| Scenario matching (simulation service `POST /api/v1/scenarios/match`, ADR-0014) — `hashing-v1` embeddings, pgvector per scenario with model, recipe and version, lazy re-embedding, reasons per scenario | 17 integration tests on PostgreSQL with pgvector, 5 of the scenario text, 12 of the embedder | 45 of 45. Live: the nine demo scenarios embedded; a refund-limit prompt change selects the refund scenarios and not the data export one |
| OpenAI-compatible embedding provider — batches, no proxies or redirects, every answer checked, retries with `Retry-After`; `503 EMBEDDINGS_UNAVAILABLE` rather than an empty selection | 13 against fake `/embeddings` servers (mock transport and a real socket with proxy variables set), plus the service's roles against a fake hosted model and the match API with a provider that cannot answer | 35 of 35 |
| Change impact (`GET /api/v1/change-sets/{id}/impact`, `internal/impact`, ADR-0029) — the graph's blast radius and the scenario library in one answer, every scenario with every reason and a sentence, `complete: false` with problems and notes | 8 unit tests; 7 integration tests with fake graph and simulation services that check every request and answer against those services' own contracts | 41 of 41 |
| Demo imports and acceptance — the payments and orders APIs and the support desk's MCP tools imported by the seed, version 1.3.2 (the refund tool's contract only), `--changes` pairs, SDK `import_openapi`, `import_mcp`, `create_change_set`, `change_set_impact` | SDK and seed tests with fakes held to the control-plane contract | — (wiring; the behaviour behind it is covered above). The live acceptance is in the evidence table above |
| Web: change sets (`/changes`, `/changes/{id}`) — compare two versions, the masked prompt diff, schema changes with what breaks, whether the impact is complete, the required scenarios with a badge and a sentence per reason, the paths behind them, irreversible actions and new privileges; "Simulate the required scenarios" opens the candidate's simulation with them preselected | 35 (helpers 20, components 15) on the live payloads, typed by the contract | 19 of 19 |
| Web: the dependency graph (`/graph`, ADR-0030) — bounded neighbourhood, deterministic layered layout, edges styled by evidence, evidence and risk-tier filters, a change's blast radius outlined by severity, component panel with relationships as sentences, find and centre, manual mapping for `graph.write` | 29 (graph helpers 16, canvas 5, explorer and panel 8) | 23 of 23 |
| Phase 4 end to end (`apps/web/e2e/phase4-changes.spec.ts`) | 4 Playwright tests against the running stack: the prompt change, the tool change, the graph from a change set, the manual mapping round trip and a viewer's read-only view | — |

Found on the way and fixed:

* **Cross-organization writes.** A person of one organization could write
  rows under another organization's project id. The graph's tests found it
  (ADR-0025).
* **`make doctor`'s schema check skipped the graph schema.** It now expects
  all five service schemas.
* **The graph's component search form shared its input's accessible name.**
  The Phase 4 e2e test found it; the form is now "Component search".

## Phase 5 evidence (2026-09-25)

Acceptance: *a bad candidate is BLOCKED.* It holds on a stack built from
scratch, through the seed, through the CLI a CI job runs and through the UI
(the Phase 5 e2e tests). Commands run on the Phase 5 code, with their results:

| Command | Result |
|---|---|
| `make reset` (drop volumes, then `make dev`: build, start, wait until healthy, seed) | Stack healthy and seeded in 93 s. Besides everything Phase 4 seeded (5 manifests, the twin, 9 scenarios, 3 tool catalogs, 2 change sets, the two simulations, the dataset, the 1.3.0 evaluation, 40 conversations), 2 releases gated: **1.2.4 → 1.3.0 BLOCK**, exit code 3, risk index 90/100, rules `duplicate_side_effect`, `unverified_success`, `policy_violation`, `critical_failure`, `regression`, `semantic_regression`, evidence verified; **1.2.4 → 1.3.1 PASS**, exit code 0, risk index 10/100, no rule, evidence verified |
| `bin/agenttwin release check --project support --baseline support-refund-agent@1.2.4 --candidate support-refund-agent@1.3.0 --ci` | **exit 3**. "Gate BLOCK. Blocked: an action took effect twice (1); success the final state disproves (1); a new policy violation (1); a critical expectation fails (3); a non-critical expectation newly fails (3); answers judged worse (1)." Required scenarios passed 6/9. Every rule with its scenario, expectation, what was observed ("The side effect refund:ORD-1001 was applied 2 times"), the first divergence ("At step 2 the baseline called get_refund_policy(order_id=ORD-1001); the candidate called refund_payment(amount=40, order_id=ORD-1001)") and the trace id. Evidence sha256 `890bf974…` (verified), details URL |
| The same for 1.3.1 | **exit 0**. "Gate PASS. Every required scenario passed (9 of 9); no rule triggered." Risk index 10/100, evidence verified |
| `release check --release <id> --format junit` / `--format json` | exit 3 both. JUnit: 11 tests (the gate, the 9 scenarios, the one rule without a scenario), 3 failures (the gate and the two scenarios that block; the warnings are reported in their output, not as failures), with the release, outcome, exit code, risk index, rules version and evidence hash as properties. JSON: the gate as the API answered it |
| `bin/agenttwin doctor` | control plane ready; API key credential in Demo Co; "can create and check releases" |
| `make e2e` | 23/23 Playwright tests. Phase 1: 7, Phase 2: 5, Phase 3: 4, Phase 4: 4, Phase 5: 3. The Phase 5 tests cover: the seeded 1.3.0 BLOCKED and 1.3.1 not, why, every rule's evidence, the verified hash and the eval case it opens; a release created in the UI that evaluates and blocks on its own, an engineer who cannot override it, a reviewer who does (a reason too short is refused) and a gate that then reads "Overridden, originally BLOCK" everywhere with CI's exit code 0, and the owner's audit trail; a viewer's read-only view. A second run on the same stack failed once (the test took the newest 1.3.0 release, which the first run had overridden); the tests now pick the seed's releases by title, and passed three more times on that stack (twice alone, then in this full run). Screenshots in `docs/screenshots/phase5-*.png` |
| `make doctor` | 0 failed, 1 warning (development placeholder secrets). All migrations applied (control plane 4, trace 1, graph 1, simulation 3, evaluation 2); every service ready; dead-letter queues empty; no event waiting without a consumer. Demo workspace: traces, 9 scenarios, a completed evaluation run and, new in Phase 5, the gated releases with their outcomes (after two e2e runs: 2 BLOCK, 2 PASS, 2 OVERRIDDEN — the seed's and the CLI's releases, and one overridden release per e2e run) |
| `make lint` | gofmt clean; `go vet` clean; golangci-lint 0 issues; ruff, ruff format (187 files) and mypy strict (84 files) clean; Prettier, ESLint and `tsc` clean |
| `make test` | Go: 285 tests and 233 subtests pass with real PostgreSQL and RabbitMQ (0 failed, 0 skipped; 245 and 141 at the end of Phase 4). Python: 686 (core 194, simulation service 171, evaluation service 142, SDK 74, demo 56, contracts and document checks 49). Web: 241 (24 files). 367 s |
| `make contracts-check` | 14 event schemas and 5 API documents (92 operations, 86 at the end of Phase 4) compatible with the baselines; the five generated web type files up to date |

## Phase 5 progress (2026-09-25)

Pieces landed, each with its tests and the mutations they catch (same method
as Phases 3 and 4):

| Piece | Tests | Mutations caught |
|---|---|---|
| Gate rules (`control-plane/internal/gate`, spec §28, §67) — PASS/WARN/BLOCK from the release's evaluation, the change's impact and the resolved gate policy; missing evidence blocks; every rule names its evidence; counts, coverage ratios, a risk index that never decides, the CLI exit code | the table of spec §67 and every rule's own case (32 cases), evidence down to both sides and the first divergence, determinism over shuffled inputs, two properties over 2,000 random releases (a missing result never passes; a new critical failure never helps). Coverage 97.3% | 31 of 31 — three survived a first run and got tests; the determinism test found evidence keeping its input order |
| A release runs exactly the scenario versions it selected (simulation and evaluation services) — pairs pinned to scenario versions, one evaluation run per release evaluation, judge spend capped by the gate budget | 9 release-run tests (7 on PostgreSQL over the real simulation service and demo agent) and 3 new pair tests | 46 of 46 (evaluation service), 15 of 15 (simulation service) |
| The release's evidence mapped for the gate (`control-plane/internal/release`) — the suite pinned from the impact, which components the suite tests, new privileges, the run's cases and observations, judge calibration, cost | the mapping over a real captured release evaluation (1.2.4 → 1.3.0) plus unit tests | 34 of 34, and 1 of 1 on the impact's version id |
| Releases, revisioned evaluations, hashed gate decisions, overrides (ADR-0031) — API, event consumer, immutability triggers, audit | integration tests through contract-checked fakes: the bad candidate BLOCKED, a mirrored candidate passes, an empty suite warns; overrides, tampering, missing evidence, retries, tenancy, CI keys | 50 of 51 (the survivor is enforced again by a unique constraint) |
| `agenttwin` CLI (spec §39, §101) — `release check` (text, JSON, JUnit; exit codes 0/2/3/4; CI detection; idempotent retries), `agent validate`, `agent register`, `doctor` | every command against a fake control plane answering with the control plane's captured responses, each exchange held to the contract | 50 of 50 |
| Web: `/releases` and `/releases/{id}` (spec §41.2, §78, §91, §92, §122) — the list's columns, why a gate decided, every rule's evidence linked to the eval case and the trace, coverage, risk index, revisions, the hashed evidence and whether it verifies, the override form; an override never reads as a pass | 27 (helpers 11, components 16) on captured live payloads, typed by the contract | 84 of 84 (helpers 55, components 29) |
| SDK and demo seed — create, list, read and evaluate releases, wait for a gate; the seed gates 1.3.0 and 1.3.1 and fails when a gate does not decide, decides without evidence or does not verify | SDK exchanges held to the contract; seed tests with fakes | 10 of 10 |
| Phase 5 end to end (`apps/web/e2e/phase5-releases.spec.ts`) | 3 Playwright tests against the running stack: the seeded bad candidate BLOCKED and its fix not, with why, evidence and the eval case it opens; a release created in the UI, gated, then overridden by a reviewer without reading as a pass, with the audit trail; a viewer's read-only view | — |

Found on the way and fixed:

* **Rules gave different answers to the same evidence in another order.**
  The gate's determinism test (shuffled inputs) found evidence without a
  scenario keeping its input order; rules and evidence are now sorted.
* **A release evaluated too early pins a suite from an incomplete impact.**
  The demo seed evaluates its releases only once the dependency graph knows
  their change set; an evaluation started before would have run a suite that
  misses the scenarios the change reaches.
* **Every short id read alike.** The Phase 5 screenshots showed a user, an
  API key and two evaluation runs all labelled `01a0d92e`: the web shortened
  identifiers to their first 8 characters, which in a UUIDv7 are its
  creation time. A UUID now keeps its random end; hashes keep their start.

## Phase 6 evidence (2026-09-25)

Acceptance: *a demo failure becomes a regression case that the next release
runs on its own.* It holds on a stack built from scratch, through the seed,
through the UI (the Phase 6 e2e tests) and through the CLI a CI job runs.
Commands run on the Phase 6 code, with their results:

| Command | Result |
|---|---|
| `make reset` (drop volumes, then `make dev`: build, start, wait until healthy, seed) | Stack healthy and seeded in 130 s. Everything Phase 5 seeded (manifests, twin, scenarios, catalogs, change sets, simulations, dataset, the 1.3.0 evaluation, conversations, **1.2.4 → 1.3.0 BLOCK** and **1.2.4 → 1.3.1 PASS**), then the canary incident on 1.3.0: order ORD-3029, its refund's payment timed out after the money moved and was retried without an idempotency key — 2 refunds, the agent claimed SUCCESS, the verified outcome FAILURE was reported. 4 s later the seed found it grouped: **"refund_payment took effect twice"**, critical, `DUPLICATE_SIDE_EFFECT`, CANDIDATE, version 1.3.0. The inbox it reported: 4 groups, all CANDIDATE — that one, "Access to lookup_order was denied" (high, 5 occurrences), "Access to export_customer_data was denied" (high, 2) and "refund_payment timed out" (medium, 4), the last three from the 1.2.4 traffic |
| `make e2e` | 25/25 Playwright tests in 2.7 min. Phase 1: 7, Phase 2: 5, Phase 3: 4, Phase 4: 4, Phase 5: 3, Phase 6: 2. The Phase 6 tests: a reviewer finds the incident in the inbox (critical, "Duplicate side effect", `refund_payment`), reads its evidence and failure, opens the representative trace (1.3.0, its waterfall), checks the draft is complete (`noDuplicateSideEffect`, the `timeout_after_mutation` fault), promotes it and follows it into `production-regressions` (a "Production regression" case, redacted); then an engineer creates 1.2.4 → 1.3.1 in the UI, its suite runs the scenario as a known regression, the gate passes, and the regression reads "Fixed in v1.3.1" with its history (created, promoted, fixed). Screenshots in `docs/screenshots/phase6-*.png` |
| `bin/agenttwin release check --project support --baseline support-refund-agent@1.2.4 --candidate support-refund-agent@1.3.0 --reevaluate --ci` (after the promotion) | **exit 3**, revision 2 of the seed's release. "Blocked: a known regression is back (1); an action took effect twice (2); …". The new first rule, `known_regression`: the promoted scenario, "The side effect refund:ORD-1001 was applied 2 times", its first divergence and trace. Required scenarios passed 6/10 (9 at the end of Phase 5, now with the regression test); "Known regressions replayed 1/1". Evidence sha256 `051b8dec…` (verified). The regression stayed FIXED: an older version failing it does not reopen it (events created, promote, fixed) |
| `make doctor` | 0 failed, 1 warning (development placeholder secrets). All migrations applied (control plane 4, trace 1, graph 1, simulation 3, evaluation 3); every service ready; dead-letter queues empty; no event waiting without a consumer. Demo workspace: traces, 10 scenarios, a completed evaluation run, 5 gated releases (2 BLOCK, 2 PASS, 1 OVERRIDDEN) and, new in Phase 6, "4 mined regressions (3 CANDIDATE, 1 FIXED)" |
| `make lint` | gofmt clean; `go vet` clean; golangci-lint 0 issues; ruff, ruff format (200 files) and mypy strict (89 files) clean; Prettier, ESLint and `tsc` clean |
| `make test` | Go: 287 tests and 233 subtests pass with real PostgreSQL and RabbitMQ (0 failed, 0 skipped). Python: 902 (evaluation service 343, core 194, simulation service 171, SDK 75, demo 67, contracts, images and document checks 52; 686 at the end of Phase 5). Web: 268 (26 files). 446 s |
| `make contracts-check` | 14 event schemas and 5 API documents (101 operations, 92 at the end of Phase 5) compatible with the baselines; the five generated web type files up to date |

## Phase 6 progress (2026-09-25)

Pieces landed, each with its tests and the mutations they catch (same method
as the earlier phases):

| Piece | Tests | Mutations caught |
|---|---|---|
| Miner domain (`evaluation-service` `mining.py`, spec §18, ADR-0032) — candidates from deterministic signals, content-free features and the sentence embedded, taxonomy and severity each with its evidence, fingerprint, grouping (known fingerprint, then nearest neighbour ≥ 0.85, else a group of one), the §121 lifecycle | over four real traces of the demo stack | 51 of 51 |
| Scenario drafting (`drafting.py`) — input and recorded request context, the faults the trace suffered, expectations that catch the failure, production entities mapped onto the twin's records through its own response templates, redaction, notes and what only a person can write | the same traces, the twin of the demo | 64 of 64 |
| Mining trace events (`miner.py`, `regression_store.py`, migration 0003) — each event once and in one transaction; outcome and flag events read the trace again; one group per burst; kinds that change move; fixed by an evaluation run, reopened by a later failure | 17 integration tests on PostgreSQL, one through a real evaluation run of the demo agent | miner 24 of 25 (the one left is equivalent), store SQL 13 of 13 |
| Regressions API (`regressions.py`, nine operations, OpenAPI, SDK) — inbox, detail, triage, confirm/dismiss/reopen, merge, draft, promotion to `production-regressions` (two steps, idempotent, concurrent promotions make one test) | 16 integration tests with the real simulation service, the contract walk, SDK tests against the contract | 25 of 25 (one survived a first run — an archived dataset still took promotions — and got its test) |
| Request context (SDK `input_context`, trace service content key) — the tenant a draft maps entities within | SDK and trace-service tests (content, never an extra attribute; redacted under the capture policy) | — |
| Demo incident — faults scoped to one order (tools server), `TrafficGenerator.incident`, `seed --incident` waiting for the miner's group and reporting the inbox | 67 demo tests (seed against a contract-checked fake, the incident against the real tools server) | seed wiring 14 of 14 (one survived at first: the test that caught it was not selected; renamed) |
| Web: `/regressions` and `/regressions/{id}` (spec §41.3) — the inbox's columns, filters through the contract's query, evidence, failures, history, the actions the status and the caller's permissions allow, triage sending only what changed, merge, the draft and its promotion as drafted or as a person edited it | 27 (helpers 11, components 16) on the service's own answers captured by its integration tests; the lifecycle table is read from the service's source and the labels from the contract | 13 of 13, and the drift check fails on a changed transition |
| Image layout — each Python service imports with only the schema documents its Dockerfile stage copies | 3 | the missing scenario schema reproduces the container's failure |

Found on the way:

* **The evaluation-service image lacked the scenario schema.** The regressions
  API validates a promotion as a scenario when it is imported; the image stage
  copied only the contracts, so the container failed at start while every
  test, running from the repository, passed. The first `docker compose up` on
  the Phase 6 code found it; the image layout test now holds every stage.
* **The regression page lost what a promotion answered** (the scenario's
  version, redactions, warnings) once it read the promoted regression again:
  the form holding the answer was unmounted. The e2e test found it; the unit
  tests' fake now answers like the service, and reproduced it.

## Phase 7 evidence (2026-09-25)

Acceptance (golden path 13–14): *a runtime refund above the configured amount
is held for a person; the approvals page receives the request; a person
approves that exact action; it succeeds once; a modified request cannot reuse
the approval.* It holds on a stack built from scratch, through the seed,
through the UI and the gateway directly (the Phase 7 e2e tests) and through
`make demo-contained`. Commands run on the Phase 7 code, with their results:

| Command | Result |
|---|---|
| `make reset` (drop volumes, then `make dev`: build, start, wait until healthy, seed) | Stack healthy and seeded in 115 s. Everything Phase 6 seeded (the 1.2.4 → 1.3.0 **BLOCK** and 1.2.4 → 1.3.1 **PASS** releases; the canary incident on ORD-3029 grouped as "refund_payment took effect twice"; an inbox of 4 CANDIDATE groups), then the containment of the demo agent: 7 tool endpoints registered with the runtime gateway (`refund_payment` WRITE_IRREVERSIBLE, `escalate_to_human` and `send_email` WRITE_REVERSIBLE, `export_customer_data` ADMIN, `lookup_order`, `lookup_customer` and `get_refund_policy` READ); `refund-limits` v1 tested (6 of 6 pass; thresholds `args.amount > 100`, `> 1000`, `trace.calls >= 1`) and activated; `customer-data-export` v1 (1 of 1) activated; then a contained $150 refund on ORD-3030: **REFUND_AWAITING_APPROVAL**, its approval request PENDING by `over-automatic-limit` |
| `make e2e` | 28/28 Playwright tests in 2.5 min. Phase 1: 7, Phase 2: 5, Phase 3: 4, Phase 4: 4, Phase 5: 3, Phase 6: 2, Phase 7: 3. The Phase 7 tests: (1) the demo agent, contained, asks for a $150 refund; the reviewer finds it on `/approvals`, sees the exact action (amount 150, the order) while no refund has moved, and approves it; the waiting agent repeats the action with its token: REFUND_COMPLETED, one `refund_payment` call, one refund on the order; the approval is Used with one executed use, and the conversation's decisions read Held for approval, then Executed. (2) The same flow through the gateway directly: `403 APPROVAL_REQUIRED` with the approval's id; no token before a decision (`APPROVAL_PENDING`); after approval a $200 request with the token is `403 APPROVAL_MISMATCH` (`amount: 150 → 200`) and nothing moves; the exact request runs (`X-AgentTwin-Decision: require_approval`, one refund); retrying it replays the stored answer (`Idempotent-Replayed: true`, still one refund); the approval is spent (`APPROVAL_USED`); the page lists the refused change and the run. (3) `refund-limits` is v1 active; running its tests: "All 6 tests pass: this version can be activated.", boundary "100 → Allow · 100.01 → Needs approval". Screenshots in `docs/screenshots/phase7-*.png` (phases 1–6 refreshed: the navigation gained Approvals, Policies and Decisions) |
| `make demo-contained` | A $150 refund on ORD-3034 held by the gateway; the agent waited; a reviewer approved it; the agent claimed the token and repeated the exact call: REFUND_COMPLETED, the agent claimed SUCCESS, exit 0 |
| `make doctor` | 0 failed, 1 warning (development placeholder secrets). All migrations applied (control plane 4, trace 1, graph 1, runtime gateway 1, simulation 3, evaluation 4); every service ready, the `runtime` schema among them; dead-letter queues empty. Demo workspace, new in Phase 7: "2 active runtime policies; 1 approval request waits for a person" |
| `make lint` | gofmt clean; `go vet` clean; golangci-lint 0 issues; ruff, ruff format (204 files) and mypy strict (90 files) clean; Prettier, ESLint and `tsc` clean |
| `make test` | Go: 343 tests and 279 subtests pass with real PostgreSQL and RabbitMQ (0 failed, 0 skipped; 287 and 233 at the end of Phase 6), the runtime gateway's among them (policy 18, gateway core 13, API and invocation 16, store 8). Python: 955 (evaluation service 350, core 194, simulation service 171, demo 93, SDK 92, contracts, images and document checks 55; 902 at the end of Phase 6). Web: 323 (29 files; 268). 501 s |
| `make contracts-check` | 14 event schemas and 6 API documents (122 operations, 101 at the end of Phase 6; the runtime gateway's is new) compatible with the baselines; the six generated web type files up to date |

## Phase 7 progress (2026-09-25)

Pieces landed, each with its tests and the mutations they catch (same method
as the earlier phases):

| Piece | Tests | Mutations caught |
|---|---|---|
| Policy domain (`runtime-gateway` `policy`, ADR-0033) — documents that refuse unknown fields, CEL rules type-checked against the declared variables, decisions under a cost limit (a matched deny always denies, the most restrictive match wins, the fail mode on an error: `fail_open` only for READ tools), several policies combined, tests that say why a case fails, activation checks, numeric thresholds read from the rules' syntax tree and probed around each value | 18 | 63 of 64 (the one left is equivalent); two survivors of a first run pointed at redundant code, now removed |
| Invocation core (`gateway`) — the action and its SHA-256 over canonical JSON, forwarded as those bytes; the context from headers; approval tokens (32 random bytes, stored as a hash, never past the approval) and their checks; idempotency (replay, reuse, in flight, unknown outcome after a timeout); redaction of what people see and the diff between two calls | 13 | 67 of 69; both survivors pointed at redundant checks, now removed |
| Runtime schema and store — immutable versions, append-only decisions and attempts, approvals that only move forward and whose action never changes, enforced by triggers | 8 integration tests on PostgreSQL | schema 18 of 18, store 19 of 19 |
| Management API and invocation path — tool endpoints behind the egress allowlist, policies (versions, tests, activation publishing `policy.activated.v1`), approvals, decisions; each call decided in one transaction and forwarded over HTTP or MCP through netguard; its OpenAPI document, every exchange held to it | 16 integration tests, the golden path among them | two rounds: every survivor (locks, the recorded replay, expiry, limits, audits, JSON dialects, an MCP answer both result and error) got a test; the policy cache mutation is equivalent |
| Wiring — the service's command, retention, `/gateway/v1` at the control plane edge (authenticated and rate limited, the tool call's `Idempotency-Key` passed through), compose, doctor | control-plane integration test of the route | — |
| SDK gateway client (`agenttwin.gateway`) — a call with the caller's context, the decision in the answer, `call_approved` waiting for a person, claiming the token and repeating the same call with the same key; runtime management methods | against the contract-checked fakes | 12 of 12 |
| Demo containment — `--contained` sends every tool call through the gateway; a held refund waits for a person (bounded) and is repeated with the token; a refusal ends the conversation clearly | demo tests | approval wait 7 of 7, tool client 1 of 1 |
| Miner: runtime denials — `policy.violation_detected.v1`; a deny or a refused token becomes the trace's `policy_denied:<tool>` even when the agent recorded nothing; a pending approval is not a failure | 7 integration tests on PostgreSQL | caught |
| Seed containment — endpoints from the latest manifest, the policies tested and activated (a failing test fails the seed), the held refund; idempotent on a second run | seed against a contract-checked fake | 15 of 15 (the first run hid survivors behind a test filter; run on the whole file, one got its test and one redundant check was removed) |
| Scope `policies:deploy` — deploy policies as code; Go and Python matrices agree | parity test | — |
| Web: `/approvals`, `/approvals/{id}`, `/decisions` — the exact action with its hash, why it needs a person, expiry, every use and how it differed, approve or deny once with a reason; decisions filtered by effect, outcome, tool and trace | 32 on the gateway's real answers, captured from the local stack | 19 of 19 |
| Web: `/policies`, `/policies/{id}`, `/policies/new` — versions, rules, thresholds, tests, the test report with each boundary, activation and its refusal, a draft tested and saved (or "nothing new was stored") | 40 on captured answers | 21 of 21 |

Found on the way:

* **The live seed was refused (403).** The demo key's scopes could not
  register tools or activate policies; the fake the seed tests ran against
  did not check scopes. A scope, `policies:deploy`, is the pipeline's way to
  deploy policies as code; activation can loosen containment, so it is not
  part of `ci`.
* **The API baseline did not record the new scope** (5e6f58c): the test that
  holds the baseline up to date failed until 117a2c7 recorded it. `make test`
  on the full tree found it, not the per-piece gates.
* **The edge must not answer a tool call's retry itself.** Its
  `Idempotency-Key` replay would hide an unknown outcome; `/gateway/v1`
  passes the key through and the gateway, which knows the action, enforces
  it. A key differing between the header and the `idempotency_key` argument
  is refused.
* **The screenshots found three misleading displays** the unit tests did
  not: a used approval still counting down to its expiry, a sub-millisecond
  run shown as "0.00 ms", and `/decisions` filtered by a trace it did not
  show. All fixed, each with a test.

## Phase 8 progress (2026-09-26)

### TypeScript SDK (`packages/sdk-typescript`)

The spec's "equivalent ergonomic API" for Node.js 20+, without runtime
dependencies. Each piece with its tests and the mutations they catch:

| Piece | Tests | Mutations caught |
|---|---|---|
| Configuration (the Python SDK's variables), canonical JSON and redaction (the gokit rules, strategies, custom patterns, JSON paths, truncation) | 65, and parity with the Go services: the shared fixtures plus 3,000 random texts in each of the four modes and 3,000 random JSON values run through the Go reference, byte for byte | canonical JSON 8 of 9 (the one left is equivalent), redaction 11 of 11 |
| Tracing: agent runs, model calls, tool calls, retrievals, policy decisions and outcomes, wrapper and explicit styles, `start…` spans with `using`, context across `await`s (`AsyncLocalStorage`), W3C `traceparent` in and out, head sampling that follows the parent, the content policy, the attribute cap; attribute keys the same as the Python SDK's | 15 | 13 of 13, content policy 3 of 3 |
| Export: a bounded queue, batches in the background (never inside the application's `end()`), one request in flight, retries on 429/502–504 and network errors, `flush`/`shutdown`, a flush when the process exits on its own, every span counted (`exported`, `failed`, `dropped`) | 10 (an exploding exporter, an unreachable collector, a stalled one, the wire format against a local HTTP server, the exit flush in a real process) | 7 of 7; three survivors over two rounds pointed at redundant code (a flush counter, a timer and an immediate cleared at shutdown that cannot be pending), now removed |
| Delayed outcomes (`reportOutcome`, the trace-service's `RecordOutcomeRequest`, errors without the key) and the manifest helper (the prompt hash the control plane records, tool risks) | 7 (outcome enums and body held to the OpenAPI document; every demo manifest) | — |
| End to end (`make test-sdk-ts-live`): a run through the collector, read back from the API — six spans in their hierarchy, the summary, the prompt hash equal to what the control plane registered for 1.2.4, content redacted, an idempotency key masked by a JSON path — then a verified outcome that contradicts the agent's claim, flagged | 1, against the running stack | — |
| Overhead (`docs/benchmarks/sdk-overhead.md`): a tool span costs ≈ 15 µs at the median with content off, ≈ 30 µs redacted; a batch of 512 spans encodes in ≈ 3 ms off the calling path; a 50 ms collector never shows in a step's latency | 1 (bounds, and every span exported or counted as dropped) | — |

Found on the way:

* **A full batch was encoded inside the span's `end()`**, on the
  application's path. It now starts on the next turn of the event loop; a
  test checks nothing is exported before `end()` returns.
* **The documented `promptHash: manifest.promptHash` did not compile** in a
  project with `exactOptionalPropertyTypes` (the hash is `string |
  undefined`). Every optional option now accepts `undefined`; the live test,
  which type-checks with that setting, pins it.
* **With content capture on, an idempotency key is one of the arguments**
  and travels with them; both READMEs now say so and show the JSON path that
  masks it (`$.idempotency_key`).
* The Python manifest helper read only inline instructions; a manifest with
  `promptRef` now gives its hash in both SDKs.

### Web: themes, small screens, accessibility

Light, dark and system themes from one palette (ADR-0035), a menu on
phones, and every page checked by axe. Each piece with its tests and the
mutations they catch:

| Piece | Tests | Mutations caught |
|---|---|---|
| Palette: every Tailwind colour as `light-dark(light, dark)`, generated from Tailwind's own colours (`scripts/theme-palette.ts`), inside `@supports` so an older browser keeps the light colours | 2: the committed file is the generator's output (and guarded); every family mirrored in order, the dark surface between the page and a panel | 8 of 8, each regenerated before the tests ran; the `@supports` guard needed a stronger test first |
| Contrast held in the source: every text/background pair the components write in one class string (per state), AA in the light theme and, in the dark one, AA or at least the light ratio | 2, over the 50+ pairs in use | the dark mappings that would break it (secondary text not lightened, a panel as dark as a card, …) are among the 8 above |
| Theme choice: a cookie the root layout reads (no script before the first paint, nothing under the nonce CSP), a switcher of three toggle buttons, "system" following the OS as it changes, the graph canvas in the page's theme | 6 | provider and switcher 7 of 7, cookie and resolution 3 of 3 |
| Shell on phones: a menu button (`aria-expanded`, `aria-controls`) opening the navigation, closed by a link or Escape with the focus returned | 2 | — |
| Layout rules: every scroll container positioned, every table in one, every responsive grid with a shrinkable column | 3 | 2 of 2 |
| End to end (`e2e/ux.spec.ts`, against the running stack): axe (WCAG 2.1 A/AA) on all 33 pages — every list and form, a detail of each, two case pages — in the light and the dark theme, the sign-in page in both, three pages on a phone; no page wider than a 390 px screen; the theme rendered by the server, remembered, and following the OS; screenshots `ux-dark-overview.png`, `ux-phone-*.png` | 7; the whole suite 38 of 38 | — |

Found on the way:

* **Real contrast failures in the light theme**, before any dark theme
  existed: `text-slate-400` (2.6:1) was used for text in 21 places ("No
  outcome", "settling…", counts, rule names), zero-count classification
  tiles were faded to about 3.3:1, and the graph's node labels were 4.3:1
  on a selected node. All raised to AA; the unit test now keeps them there.
* **Pages scrolled sideways on a phone** (7 of 33, up to 974 px): the
  screen-reader-only text inside table cells is absolutely positioned and
  escaped the tables' scroll containers, which were not positioned; grids
  without a base column grew to their widest table; two tables had no
  scroll container at all; action bars and tab lists did not wrap.
* **Status by colour alone**: a failed span in the trace waterfall was only
  red. It now says "error" too.
* **The approvals inbox opens on what is pending**, which can be nothing:
  the audit reads `?status=all` to reach a detail page.
* **A restarted demo tools server reused order numbers** (`ORD-3001`
  again), and the runtime gateway, which keeps idempotency records in
  PostgreSQL keyed by `refund-<order>-<amount>`, rightly replayed an old
  refund instead of holding the new one for approval — the full end-to-end
  run failed Phase 7 after an unrelated restart. Orders are now numbered
  from tenths of a second since 2026 and never take a number the world
  already has (one test, 3 of 3 mutations caught).

### Golden path (spec §137) as one story

`e2e/golden-path.spec.ts` (`make golden-path`) runs steps 2–14 as one test,
each step a named `test.step`, against the running stack; step 1 is
`cp .env.example .env && make dev`. The demo's critical scenario of step 6 is
`refund-timeout-after-mutation` (the spec's example names it
`refund-timeout-idempotency`).

| Step | What the test does and checks |
|---|---|
| 2 | Signs in; the header names *Demo Co* |
| 3–4 | `/agents` lists support-refund-agent 1.2.4 and 1.3.0 with different prompt hashes and the same tools |
| 5 | Compares 1.2.4 → 1.3.0 in the UI: the prompt diff (policy lookup removed, "issue eligible refunds immediately", "simply retry the refund right away"), the path prompt → support-refund-agent@1.3.0 → refund_payment → payments-api, `refund-timeout-after-mutation` required as linked to the change |
| 6–9 | Creates the release in the UI and waits for its gate: **BLOCK**, rule "Duplicate irreversible action" on the critical scenario, exit code 3; the evidence says the refund was applied 2 times and shows the first divergence; on the compared case the fault is a timeout after mutation, the baseline **PASSED** with one refund followed by `lookup_order`, the candidate **FAILED** with two refunds, `refund_count` 1 → 2 |
| 10 | A 1.3.0 canary in production on an order carrying the same fault: two refunds, the verified outcome reported from the ledger; a reviewer finds the regression by that very trace and promotes it into `production-regressions` |
| 11 | 1.3.1 is registered; its diff from 1.3.0 restores the policy lookup and verifies the order before any retry |
| 12 | The release of 1.3.1: **PASS**, exit code 0, the promoted scenario in the suite as a known regression; the regression is *Fixed in v1.3.1*; 1.3.0's release stays BLOCK |
| 13–14 | A 150 USD refund through the runtime gateway: `APPROVAL_REQUIRED`; the approvals page receives it; a reviewer approves; a modified request (200 USD) with the token is refused (`APPROVAL_MISMATCH`, no refund); the exact one runs once, a retry replays, the approval is spent |

Evidence, from nothing (2026-09-26): `.env` recreated from `.env.example`
(plus this sandbox's build proxy and host ports), `make reset` (all volumes
destroyed) **exit 0 in 1 min 51 s**, the seed's canary incident a regression
*candidate*; then `make e2e`: **39 of 39 in 7.3 min**, the golden path first
(41 s) — the regression's history shows it promoted once, by the reviewer,
and fixed by the 1.3.1 evaluation. Screenshots `golden-path-*.png`.

The gate rule's title is now the spec's words, "Duplicate irreversible
action" (was "An action took effect twice"); decisions already stored keep
their text, since decisions are immutable and hashed.

### Supply chain (spec §109) and CI (spec §70)

`make secret-scan`, `make vuln-scan`, `make image-scan`, `make sbom` and
`make supply-chain` (scripts/supply_chain.py; pinned gitleaks, govulncheck,
pip-audit, trivy). Policy, results, exceptions and the upgrade notes:
[docs/security/supply-chain.md](../security/supply-chain.md).

| What | Evidence (2026-09-26) |
|---|---|
| Secrets | gitleaks over all commits: 0 findings; 321 raw matches of the default rules, each explained by a narrow allowlist or fingerprint. A GitHub token committed in a scratch clone is caught (exit 3). A missing config no longer passes as "found" (gitleaks exits 1 for both; the scan asks for 3) |
| Dependencies | govulncheck, pip-audit (every locked package, with hashes), pnpm audit: nothing. gRPC v1.83.1 → v1.83.2 (GO-2026-6443; reported by govulncheck as imported-not-called, by the image scan as a fixable HIGH in every Go image) |
| Images | 14 scanned, 0 policy violations. Our images: no fixable HIGH/CRITICAL (web: npm, corepack, yarn removed; Python: pip removed; PostgreSQL: own image, Debian updates at build, gosu removed, non-root). Third-party: no CRITICAL after RabbitMQ 4.3.6, OTel collector 0.161.0, Prometheus v3.15.0, Grafana 12.4.11, k6 2.3.0 (before: 24 CRITICAL, 312 HIGH across these five) |
| Exception | CVE-2026-6653 (libxml2, no Debian fix) in `.trivyignore.yaml`, expires 2026-12-31; with the date in the past the scan fails (exit 2) |
| SBOM | 11 CycloneDX documents: 9 images, the Go + JS lockfiles, the locked Python environment (trivy cannot read a multi-root uv workspace lock, so it reads `uv export`) |
| Found on the way | RabbitMQ's node name was the container id: every recreated container started empty and left queued events and dead letters in the volume. Fixed hostname; a dead letter now survives a recreate. The published pgvector image left the build's `apt-get update` failing silently ("0 upgraded"); the build now fails on any index error |
| Upgrade in place | the old PostgreSQL volume starts on the new image as `postgres`; collation warning until `make db-upgrade` (reindex, refresh, pgvector 0.8.0 → 0.8.6; idempotent, 4 s); `make doctor` warns until then. RabbitMQ 3.13 volume upgrades in place |
| Tests on the new infra | `make test-integration` on PostgreSQL 16.15 / pgvector 0.8.6 / RabbitMQ 4.3.6: Go packages ok, 1047 Python tests passed (6.9 min); `make lint` clean. On the stack upgraded in place: `make e2e` **39 of 39 in 7.3 min** (golden path 42.6 s); `make chaos-drill` passed (RabbitMQ stopped: the run completes, the outbox drains, 40 s; PostgreSQL stopped: 503 with Retry-After, 200 again 1 s after it returns, 26 s; worker killed: the lease brings the run back, 69 s); `make load-test` with k6 2.3.0 passed, every p95 within noise of the baseline (docs/benchmarks/load-baseline.md) |
| CI | `.github/workflows/ci.yml`: lint, unit, contract, integration (+ chaos tests), frontend, security, build (image scan, SBOM artifacts), e2e; nightly: live security, TS SDK live, chaos drill, load test. Every step a make target; actions pinned by SHA; checked with actionlint 1.7.7 and the workflow/action JSON schemas. Dependabot for Go, uv, npm, Dockerfiles, compose images and actions, with a 5-day cooldown |

## Definition of Done tracking

See the final delivery report in `docs/delivery-report.md` (written at the end;
lists the exact commands and test counts that were run).
