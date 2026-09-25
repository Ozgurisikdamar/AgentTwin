# AgentTwin contracts

Versioned, language-neutral contracts shared by Go, Python and TypeScript code.

* `events/envelope.v1.schema.json` — the common event envelope.
* `events/<type>.schema.json` — one payload schema per event type.
* `../scenario-schema/schemas/*.schema.json` — agent manifest, scenario, runtime
  policy and tool-twin definition schemas (user-authored YAML).
* `openapi/<service>.openapi.yaml` — the HTTP API of a service (OpenAPI 3.1,
  written by hand, checked against the service; see below).

## Compatibility rules

* The event **type name carries the version** (`simulation.run_completed.v1`).
* **Additive** changes (new optional fields) are allowed within a version; every
  payload schema sets `additionalProperties: true` and every consumer ignores
  unknown fields (contract-tested).
* **Removing or renaming a required field, or changing its type, is breaking** and
  requires a new version (`.v2`) published side by side until consumers migrate.
  `make contracts-check` fails when a required field disappears from a published
  schema compared with the committed baseline (`events/.baseline.json`); the
  same command checks the HTTP APIs (below).

## HTTP APIs (OpenAPI)

| Document | Operations | Covers |
|---|---|---|
| `openapi/simulation-service.openapi.yaml` | 17 | twins, scenarios, simulation runs (public, via the control plane); the twin endpoint agents call; the agent adapter contract (`webhooks.agentRun`) |

Each document describes the API as clients reach it: through the control plane
with its credentials (`Authorization: Bearer`, `X-AgentTwin-Api-Key`),
`Idempotency-Key` on mutations and the edge's errors. The document is held to
the service in four ways (ADR-0021):

1. **Document** — valid OpenAPI 3.1 that follows the API conventions (the
   service's `test_contract.py`).
2. **Routes** — the service's routes and the documented operations are the same
   set, both ways.
3. **Traffic** — the integration tests check every response strictly
   (undocumented fields fail), every request the service accepts and every call
   it makes to a webhook; a contract walk requires a checked success response
   for every operation (`agenttwin_core.openapi_contract`).
4. **Compatibility** — `make contracts-check` compares every operation with the
   committed `openapi/.baseline.json`:

| Part | Breaking | Compatible |
|---|---|---|
| Request (parameters, body) | operation, parameter or body field removed (bodies are strict: unknown fields are rejected); new required parameter or field; body newly required; type changed; allowed enum value removed | new optional parameter or field; new enum value |
| Response (success statuses) | success status removed; required field removed or made optional; type changed; an embedded document (`x-agenttwin-schema`) changes its schema or loses it | new field; new enum value; new status; a field becomes an embedded document |
| Webhook | the same rules with the roles reversed: the service sends the request and reads the answer (ignoring unknown fields) | |

Clients must ignore response fields and enum values they do not know. Error
responses (`{"error": {"code", "message", "request_id", "details"?}}`) are
documented but not versioned by shape: their `code` values are the contract.

The clients are held to the documents too (ADR-0021): the web app's API types
are generated from them (`make gen-api` writes
`apps/web/src/lib/api/*.gen.ts`; a unit test and `make contracts-check` fail
when they are stale), its hand-written types must accept every documented
response at compile time, and the Python SDK's and the demo seed's test fakes
answer with contract payloads and check every exchange
(`agenttwin_core.api_fakes`).

To change an API: edit the document with the code, run the service's tests
(route parity and traffic), `make gen-api`, then `make contracts-check`. A
breaking change needs a new operation or version; after a reviewed,
compatible change record the baseline with
`python scripts/contracts_check.py --update`.

## Canonical JSON and content hashes

Prompts, manifests, tool schemas, arguments and evidence are identified by the
SHA-256 of their canonical JSON form. Go (`packages/gokit/hashx`) and Python
(`agenttwin.hashing`) produce byte-identical output and are both tested against
`fixtures/canonical-json.json`:

* object keys sorted by UTF-8 code units, no insignificant whitespace, no HTML
  escaping, strings as UTF-8;
* integral numbers below 1e21 without fraction or exponent (`1.0` → `1`),
  `-0` and `0.0` → `0`, other numbers in the shortest round-trip form;
* NaN and infinities are rejected.

The encoding is a fixed point: canonicalizing canonical JSON returns the same
bytes (Go fuzz targets and Python Hypothesis tests).

## Event catalog

Exchange: `agenttwin.events` (topic, durable). Routing key = event type.

| Event | Producer | Consumers | Outbox | Purpose |
|---|---|---|---|---|
| `agent.version_registered.v1` | control-plane | graph-service | ✓ | Declared manifest edges (agent → prompt/model/tools/dependencies). |
| `tool.catalog_imported.v1` | control-plane | graph-service | ✓ | OpenAPI / MCP / manual tool import → API/service nodes. |
| `trace.ingested.v1` | trace-service | graph-service, evaluation-service | ✓ | Observed edges; regression-miner candidate detection. |
| `trace.outcome_recorded.v1` | trace-service | evaluation-service | ✓ | Failed / contradicted outcomes become regression candidates. |
| `trace.flagged.v1` | trace-service | evaluation-service | ✓ | Manual incident / negative-feedback flag. |
| `scenario.upserted.v1` | simulation-service | graph-service | ✓ | SCENARIO nodes + `TESTED_BY` edges for impacted-scenario selection. |
| `policy.activated.v1` | runtime-gateway | graph-service | ✓ | POLICY nodes + `GUARDED_BY` edges (policy changes enter blast radius). |
| `policy.violation_detected.v1` | runtime-gateway | evaluation-service | ✓ | Runtime denials become regression signals. |
| `evaluation.run_requested.v1` | control-plane | evaluation-service | ✓ | Start a release evaluation for a selected suite. |
| `simulation.run_requested.v1` | simulation-service | simulation-service (worker) | ✓ | A run was created (API, including the evaluation service's requests); wakes a worker. The run itself is in the database: a lost event delays it by one poll, never loses it. |
| `simulation.run_completed.v1` | simulation-service | evaluation-service | ✓ | Run finished (COMPLETED / FAILED / CANCELLED). |
| `evaluation.run_completed.v1` | evaluation-service | control-plane | ✓ | Comparison ready → compute gate. |
| `audit.recorded.v1` | trace, evaluation, simulation, runtime | control-plane | ✓ | Governance actions of non-edge services land in the central audit log. |

Every published event has at least one consumer; an event without a consumer is
not published (an unconsumed queue silently accumulates messages).

## Delivery semantics

* At-least-once. Publisher confirms on every publish.
* Each consumer queue `<service>.events` is bound to the routing keys it handles,
  dead-letters to `<service>.events.retry` (TTL, then back to the exchange) and,
  after the maximum attempt count (`x-death`), is moved to `<service>.events.dlq`.
* Consumers record `(consumer, event_id)` in `processed_event` inside the same
  transaction as their state change, so redelivery is a no-op.
* Poison messages (invalid envelope / payload schema) go straight to the DLQ; they
  are never retried.
