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

## Decision
* **One OpenAPI 3.1 document per service API**, written by hand in
  `packages/contracts/openapi/<service>.openapi.yaml` and versioned with the
  code. It describes the API as clients reach it — through the control plane,
  with its credentials, `Idempotency-Key` and edge errors — plus the other
  interfaces the service has: surfaces other parties call (the twin endpoint)
  and calls it makes (the agent adapter contract, as a webhook).
* **The document is held to the service in four ways** (`agenttwin_core.openapi_contract`):
  1. *Document:* it is valid OpenAPI 3.1 and follows the API conventions
     (operation ids and tags, the error responses of every operation, closed
     request bodies, no unused components).
  2. *Routes:* the routes the service serves and the documented operations
     are the same set, both ways.
  3. *Traffic:* the integration tests pass every response through the
     contract, **strictly** — an object that declares its properties may not
     carry undocumented ones, even through `allOf`; every request the service
     accepts (a documented parameter, field and value); and every call the
     worker makes to the agent, request and answer. Embedded documents marked
     `x-agenttwin-schema` are validated against their canonical JSON Schema. A
     contract walk requires a checked success response for every operation.
  4. *Compatibility:* `make contracts-check` compares each operation with a
     committed baseline. Requests break when an operation, parameter or body
     field disappears (the services reject unknown fields), something new is
     required, a type changes or an allowed value is removed. Responses break
     when a success status disappears, a required field is removed or made
     optional, or a type changes. Webhooks reverse the roles.
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
* Checking the clients (the web app's types, the SDKs) against the documents
  is a separate step; the Go services need the same traffic check in Go.
