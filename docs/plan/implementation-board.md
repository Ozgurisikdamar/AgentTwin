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

Next: the clients checked against the document (web types, Python SDK), then
the control-plane and trace-service documents with the same traffic check in Go.

## Definition of Done tracking

See the final delivery report in `docs/delivery-report.md` (written at the end;
lists the exact commands and test counts that were run).
