# AgentTwin contracts

Versioned, language-neutral contracts shared by Go, Python and TypeScript code.

* `events/envelope.v1.schema.json` — the common event envelope.
* `events/<type>.schema.json` — one payload schema per event type.
* `../scenario-schema/schemas/*.schema.json` — agent manifest, scenario, runtime
  policy and tool-twin definition schemas (user-authored YAML).

## Compatibility rules

* The event **type name carries the version** (`simulation.run_completed.v1`).
* **Additive** changes (new optional fields) are allowed within a version; every
  payload schema sets `additionalProperties: true` and every consumer ignores
  unknown fields (contract-tested).
* **Removing or renaming a required field, or changing its type, is breaking** and
  requires a new version (`.v2`) published side by side until consumers migrate.
  `make contracts-check` fails when a required field disappears from a published
  schema compared with the committed baseline (`events/.baseline.json`).

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
