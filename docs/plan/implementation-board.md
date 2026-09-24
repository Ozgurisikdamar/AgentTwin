# Implementation board

Tracks the phases of the build specification (§127). Each phase leaves working,
tested software and is committed separately. Status is updated as work lands.

| Phase | Scope | Acceptance | Status |
|---|---|---|---|
| 0 | Architecture, ADRs, glossary, threat model skeleton, monorepo, contracts, gokit | Docs + schemas + Makefile + compose base + shared Go kit (unit + integration tests green) | done |
| 1 | Walking skeleton: auth dev flow, projects/agents/versions, trace-service, OTel collector, Python SDK, demo agent, web trace page | `make dev` → demo agent trace visible in browser | pending |
| 2 | Scenarios, declarative stateful tool twin, fault injection, simulation runs + UI | happy path and timeout-after-mutation scenarios run | pending |
| 3 | Evaluators (deterministic, trajectory, judge adapter), datasets, eval runs, baseline vs candidate | candidate regression detected | pending |
| 4 | Graph, manifest/OpenAPI/MCP import, observed edges, change set, bounded blast radius, impacted selection | prompt/tool change selects refund scenarios with reasons | pending |
| 5 | Release, gate rules, immutable evidence, CLI CI output, release UI | bad candidate BLOCKED | pending |
| 6 | Regression miner: features, embeddings, grouping, taxonomy, inbox, promotion | demo failure → regression case → auto-included in next gate | pending |
| 7 | Runtime gateway: CEL policies, approvals, idempotency, SSRF-safe proxy, audit | over-limit refund requires approval; tampered args rejected | pending |
| 8 | Tenant isolation, SSRF, redaction, chaos, load, security, E2E, docs, screenshots | Definition of Done checklist | pending |

## Definition of Done tracking

See the final delivery report in `docs/delivery-report.md` (written at the end;
lists the exact commands and test counts that were run).
