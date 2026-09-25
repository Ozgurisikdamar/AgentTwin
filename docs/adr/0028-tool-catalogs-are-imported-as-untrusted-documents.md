# ADR-0028 — Tool catalogs are imported from documents in the request, as untrusted input

* Status: accepted · Date: 2026-09-25

## Context
An agent's tools are usually operations of APIs the organization already
describes: OpenAPI documents, or the `tools/list` result of an MCP server
(spec §20.2, §20.3). Importing them serves two purposes:

* it fills the tool registry, and
* it tells the graph which API and service stand behind each tool, so that a
  change to `refund_payment` reaches the payments API.

These documents are written by other teams, or served by third-party MCP
servers. They can be very large or recursive, they can reference remote files,
and they can understate a tool's risk (spec §33).

## Decision
* **The control plane contacts nothing.** The document is part of the request,
  as an object or as YAML or JSON text up to 2 MiB. A `$ref` that points
  outside the document is recorded but not followed. Server URLs are stored
  without credentials or query strings, and an MCP server's URL is stored
  without credentials.
* **The document is bounded.**
  - Cycles end in a marker.
  - Reference chains stop at depth 16.
  - A schema is limited to 20,000 nodes, depth 40 and 64 KiB.
  - Descriptions are capped at 2,000 characters.
  - A catalog has at most 500 entries.
  - Examples are dropped.

  Unused overrides and name mappings are reported. When two entries collide,
  one is skipped, with the reason.
* **Every operation becomes an entry.** Its input schema holds the parameters
  by name and the JSON request body as `body`. Credentials such as
  `Authorization` and cookies belong to the runtime and are not arguments.
  The tool name is chosen by the first rule that applies:
  1. the importer's name mapping;
  2. `x-agenttwin-tool`;
  3. the `operationId`, converted to snake_case;
  4. the method and path.
* **Risk is never understated silently.** Risk is taken, in order, from:
  1. the importer's override;
  2. the document's `x-agenttwin-risk`;
  3. the HTTP method: `GET`, `HEAD`, `OPTIONS` and `TRACE` read; any other
     method is `WRITE_IRREVERSIBLE`.

  A write method recorded as `READ` is flagged. The annotations an MCP
  server gives about itself are recorded, together with the risk they
  suggest. They set risk only when the importer explicitly trusts them;
  otherwise an MCP tool is `WRITE_IRREVERSIBLE`, like any tool of unknown
  risk.
* **The registry keeps what someone else owns.** An import never overwrites:
  - a tool that an agent manifest declares (`kept_manifest`), or
  - a tool that another import or a person defines (`kept_other_source`).

  A name mapping can import such a tool under another name. Otherwise the
  import creates the tool, or a new version of it when its definition
  changed.
* **Catalogs are immutable revisions.**
  - Each import of a named source is a new revision.
  - An import equal to the latest revision stores nothing and answers `200`.
  - Imports of one source are serialized, so revision numbers have no gaps.
  - An update trigger refuses changes to a stored catalog.
  - Importing requires `agent.write`; reading a catalog requires read access
    to the project.
* **The graph learns from the event.** Each new revision emits
  `tool.catalog_imported.v1`. The event lists every tool of the revision,
  including the ones the registry kept, because the link between the tool and
  its API is a fact either way. The graph service then links:
  - each tool to its `HTTP_API` or `MCP_SERVER`, with `OPENAPI` or `MCP`
    evidence;
  - the API to its service.

## Consequences
* The demo's payments API names `refund_payment` (`x-agenttwin-tool`). After
  the import the graph reads: TOOL `refund_payment` -CAN_MUTATE→ HTTP_API
  `payments-api` -DEPENDS_ON→ SERVICE `payments-api`. The manifest's
  definition of the tool is unchanged.
* A hostile or malformed document can fail an import, or be skipped in part
  with reasons. It cannot make the control plane fetch anything, run away
  with memory or time, or quietly lower a tool's risk.
* Fetching a document from a URL, or asking a live MCP server for its tools,
  is left out of V1. When it is added, it belongs behind the runtime
  gateway's SSRF-safe egress (Phase 7), not in the control plane.
