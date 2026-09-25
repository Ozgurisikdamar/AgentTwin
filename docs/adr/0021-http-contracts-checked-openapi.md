# ADR-0021 — HTTP contracts: hand-written OpenAPI, checked against the running service

* Status: accepted · Date: 2026-09-25

## Context
The public API is REST/JSON documented with OpenAPI (§37), and every service
API needs contract coverage: the document checked, clients and server agreeing,
breaking changes detected (§60; ADR-0004 promised the same for internal calls).
Neither language generates a useful document from the code: the Python
handlers read their bodies themselves (bounded, strict), so FastAPI's generated
schema describes them as taking no input and returning anything; the Go
services have no generator at all. A generated document would also describe
whatever the code happens to do, not what is promised. A hand-written one
drifts silently unless something holds it to the service.

Three service APIs exist so far: the control plane (Go), the trace service
(Go, including its OTLP/HTTP receiver) and the simulation service (Python).

## Decision
* **One OpenAPI 3.1 document per service API**, written by hand in
  `packages/contracts/openapi/<service>.openapi.yaml` and versioned with the
  code. It describes the API as clients reach it — through the control plane,
  with its credentials, `Idempotency-Key` and edge errors — plus the other
  interfaces the service has: surfaces other parties call (the twin endpoint)
  and calls it makes (the agent adapter contract, as a webhook).
* **The document is held to the service in four ways**, by the same checker
  in both languages (`agenttwin_core.openapi_contract` in Python,
  `packages/gokit/openapicheck` in Go, with matching tests):
  1. *Document:* it is valid OpenAPI 3.1 and follows the API conventions
     (operation ids and tags, the error responses of every operation, closed
     request bodies, no unused components).
  2. *Routes:* the routes the service serves and the documented operations
     are the same set, both ways.
  3. *Traffic:* the integration tests pass every response through the
     contract, **strictly** — an object that declares its properties may not
     carry undocumented ones, even through `allOf`; every request the service
     accepts (a documented parameter, field and value); and every call the
     worker makes to the agent, request and answer. In Go the check is a
     server middleware (`Contract.Checking`) wrapped around the service in
     its integration suite, so no exchange escapes it. Embedded documents
     marked `x-agenttwin-schema` are validated against their canonical JSON
     Schema (`scenario.v1`), or against one of its definitions
     (`scenario.v1#/$defs/fault`). A coverage gate requires a checked success
     response for every operation.
     * *Strictness keeps meaning:* `unevaluatedProperties: false` closes an
       object that declares properties, but not an `allOf` member at its top
       level; `then`, `else` and `dependentSchemas` add to the object they
       sit in, and `if` and `not` are left untouched — closing them would
       invert the condition.
     * *Codings:* a gzip body is checked decoded; a coding the checker cannot
       undo is a violation, not a pass.
     * *UUIDs (RFC 9562):* case-insensitive where the service reads them (the
       parameters and body of a request made to it, the answer to a call it
       makes) and lower-case where it writes them; webhooks reverse the
       roles.
     * *Both sides:* an exchange reports its request and its response
       together, the request first — a fake that echoes what it was sent
       would otherwise blame the response for the request's mistake.
  4. *Compatibility:* `make contracts-check` compares each operation with a
     committed baseline. Requests break when an operation, parameter or body
     field disappears (the services reject unknown fields), something new is
     required, a type changes or an allowed value is removed. Responses break
     when a success status disappears, a required field is removed or made
     optional, a type changes or an embedded document changes its schema.
     Webhooks reverse the roles.
* **The clients are held to the same documents.**
  * *Web:* its API types are generated from the three documents
    (`apps/web/scripts/api-types.ts`, openapi-typescript), with each embedded
    document typed from the canonical JSON Schema the traffic check validates
    it against. A unit test fails when the committed types are not what the
    documents generate. The UI keeps its hand-written types (what it reads),
    and compile-time checks (`apps/web/src/lib/api/{control-plane,traces,simulation}.ts`)
    require each of them to accept every response the document allows for
    the operation that returns it (`Accepts`); request bodies
    (`satisfies BodyOf<op>`), query parameters (`queryOf<op>`), the trace
    explorer's URL filters (`Sends`: documented parameters only, in their URL
    form, covering every enum value) and the unit tests' fixtures are typed
    by the contract. A contract change that breaks an assumption of the UI
    does not compile, and names the field.
  * *Python:* the fakes that stand in for a service — the SDK's stub, the
    demo seed's fake API, the simulation service's fakes of the control
    plane and the trace service — answer with contract payloads
    (`agenttwin_core.api_fakes`), and `ExchangeChecker` holds every exchange
    to the contract of the service that owns the path: the request the client
    sent, the answer the fake gave.
* **Conventions the documents settled** (every service follows them):
  * A page's `next_cursor` is `null` on the last page, never an empty string.
  * A malformed path id answers `400 INVALID_PARAMETER` and a malformed filter
    `400 INVALID_FILTER`, both with `details.field`; neither reaches the
    database.
  * Enum values in filters and bodies are the canonical ones (`FAILURE`, not
    `failure`); anything else is a `400`, not a silently empty result.
  * Text limits count characters, not bytes.
  * The OTLP/HTTP receiver answers errors as a `google.rpc.Status`, encoded
    like the request (protobuf or JSON), `415` for a content type or coding
    it does not take, and reports partial success as `rejectedSpans`.
* **The promise stays open, the check is strict.** Responses may gain fields
  and enum values; clients ignore what they do not know. Strictness belongs to
  the test, so a field the service sends without documenting it fails the test
  that sees it. Request bodies are documented as closed because the services
  do reject unknown fields.
* FastAPI's generated `/openapi.json` is not served: it would contradict the
  contract.

## Consequences
* An undocumented response field fails an integration test, an undocumented
  route fails a unit test, and a breaking change fails `make contracts-check`
  unless the baseline is updated on purpose — in a reviewable diff.
* Writing the simulation contract found two defects: a twin's description was
  only returned in lists, and an agent's answer was stored with whatever types
  the agent sent, so a misbehaving agent could make the API serve a string where
  an integer is promised. Both are fixed and pinned by tests.
* Checking the clients found three more: the UI assumed a verdict's score is
  always a number (it is `null` when nothing was evaluated); evidence without
  a reference rendered an empty element, and the expected and actual values
  the evaluators record were never shown; and the SDK's and the demo seed's
  test fakes answered with shapes the service never sends, so those tests
  exercised an API that does not exist.
* Writing the control-plane document found eleven: the project list showed
  the gate policy to members without `settings.read`; rotating an API key
  dropped its expiry and could revive an expired key; a malformed path id
  reached the database; a project name could be set empty or unbounded; a
  repeated tool registration answered `201`; the audit list's last page had
  an empty-string cursor; revoking a key required a body; the manifest
  envelope ignored unknown fields; `/me` listed permissions as `null`;
  parameter errors named the field under another key; and the proxy
  forwarded a route (`/api/v1/artifacts`) no service serves.
* Writing the trace-service document found: a `NaN` or `Infinity` attribute
  failed the whole export (`503`); negative token counts and costs were
  stored; the `status`, `outcome` and `source` filters were not validated
  (`status=ok` matched by upper-casing, an unknown value returned an empty
  page instead of an error, and the outcome endpoint upper-cased what it was
  sent); an upper-case project id was refused to a scoped key; text limits
  counted bytes; expected/actual state accepted scalars; OTLP errors carried
  the HTTP status as their `code` (not a gRPC code) and were JSON even to a
  protobuf client; an unsupported content type or coding answered `400`
  instead of `415`.
* Holding the clients to those documents found: the agents page showed
  "0 tools" for every version (the version list carries its tools in the
  manifest, not in a `tools` field); a shared explorer link with a bare date
  or an unknown `source` ended in `400`; the simulation service's fakes of
  the control plane answered a manifest digest the control plane cannot
  produce and a `503` it does not answer; the compatibility check skipped
  `head`, `options` and `trace` operations. All fixed and pinned by tests.
