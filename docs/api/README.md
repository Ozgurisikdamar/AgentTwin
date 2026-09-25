# AgentTwin API

The public API is served by the control plane under `/api/v1` (locally
`http://localhost:8080/api/v1`). It forwards each request to the service that
owns the resource; the contracts are OpenAPI 3.1 documents in
[`packages/contracts/openapi`](../../packages/contracts/openapi):

| Document | What it covers |
|---|---|
| [`simulation-service.openapi.yaml`](../../packages/contracts/openapi/simulation-service.openapi.yaml) | tool twins, scenarios, simulation runs and their cases; the twin endpoint agents call; the agent adapter contract |

Each document is checked against its service on every test run (ADR-0021), so
it describes what the service does, not what it once did. Open it in any
OpenAPI viewer, or generate a client from it: the web app's TypeScript types
are generated this way (`make gen-api`).

## Authentication

Send one credential per request:

* `Authorization: Bearer <token>` — a session token, an OIDC access token or an
  API key;
* `X-AgentTwin-Api-Key: <key>` — a project API key.

Locally, the demo users log in without a password:

```bash
TOKEN=$(curl -s -X POST http://localhost:8080/api/v1/auth/dev/login \
  -H 'Content-Type: application/json' -d '{"email":"owner@demo.agenttwin.dev"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')
curl -s "http://localhost:8080/api/v1/simulations?limit=1" -H "Authorization: Bearer $TOKEN"
```

A user who belongs to several organizations selects one with
`X-AgentTwin-Org: <organization id>`.

## Conventions

* **Errors** are `{"error": {"code", "message", "request_id", "details"?}}`. The
  `code` is stable and machine-readable; the `message` is for people. Every
  response carries `X-Request-Id`, which equals `error.request_id`:

  ```json
  {"error": {"code": "INVALID_PARAMETER", "message": "run_id must be a UUID.",
             "request_id": "94e9a93b01c61337f06053cc3ea90fba", "details": {"field": "run_id"}}}
  ```
* **Unknown fields in request bodies are rejected** (`400 INVALID_REQUEST`).
  Responses may gain fields and enum values: ignore what you do not know.
* **Resources outside your projects answer `404`**, never `403`.
* **Idempotency.** Send `Idempotency-Key` (8–128 characters of
  `[A-Za-z0-9._:-]`) on `POST`/`PUT`/`PATCH`/`DELETE`, one key per user action.
  A repeat returns the stored response with `Idempotent-Replayed: true`; the
  same key with a different request answers `422 IDEMPOTENCY_KEY_REUSED`; while
  the first request runs, `409 IDEMPOTENCY_IN_PROGRESS`. Keep the key when the
  outcome is unknown (network error, `5xx`) and retry with it (ADR-0019).
* **Pagination.** Lists answer `{"items": [...], "next_cursor": "..." | null}`;
  pass `cursor=<next_cursor>` for the next page, `limit` (1–200, default 50)
  for its size.
* **Rate limits** answer `429 RATE_LIMITED` with `Retry-After`.
* **Asynchronous work** answers `202` with the created resource; poll it (for
  example `GET /api/v1/simulations/{run_id}` until its `status` is final).
