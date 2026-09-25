# ADR-0026 — The dependency graph keeps its evidence per source and is walked level by level

* Status: accepted · Date: 2026-09-25

## Context
A change is safe to ship only when we know what it can reach: the tools an
agent version uses, the APIs, services and databases behind them, and the
scenarios, policies and evaluators attached to any of them (spec §19, §22).
Several producers describe one project's graph: agent manifests, imported
OpenAPI and MCP catalogs, production traffic, scenarios, activated policies and
people. They disagree, arrive in any order and are trusted to different degrees.
ADR-0001 puts the graph in PostgreSQL adjacency tables with a bounded traversal.
This ADR records how.

## Decision
* **Components and relationships.** A component is `(organization, project,
  kind, key)`. The kinds are the specification's list plus `RETRIEVAL_SOURCE`:
  a knowledge base the agent reads is neither a service it calls nor a dataset
  it is tested on. The relationships are the specification's plus
  `VERSION_OF`, which groups an agent's versions under the agent without
  making them depend on each other. An edge reads "from TYPE to": a version
  `USES` a tool, a tool `WRITES` a database, a tool is `TESTED_BY` a scenario.
  There is one edge per `(from, type, to)`.
* **Evidence is a row per source and reference.** `edge_evidence` holds a row
  per source and reference: the manifest version, the imported document, the
  scenario, the policy, the manual mapping, or production traffic (one counted
  row, not one per trace). A newer declaration from the same source and
  reference replaces the older one. An edge left without evidence is deleted.
  The edge's `sources` and `confidence` are derived from its evidence in the
  same transaction.
* **Confidence reflects how the edge is known.**

  | Sources | Confidence | Why |
  |---|---|---|
  | `MANIFEST`, `MANUAL`, `SCENARIO`, `OBSERVED` | 1.0 | declared by an author, or seen in traffic |
  | `OPENAPI` | 0.9 | describes an interface, not how the agent uses it |
  | `MCP` | 0.8 | the server's own tool list is untrusted input (spec §33) |
  | `INFERRED` | 0.5 | never shown as certain |

  An edge's confidence is the highest among its sources. It is "certain"
  unless its only evidence is inferred.
* **Writes to one project are serialized.** A transaction advisory lock per
  project, and components written in a fixed order, keep concurrent consumers
  from deadlocking or disagreeing on an agent's latest version. The latest
  version follows registration time, not the order events arrive. An event
  that cannot be mapped is parked, not retried. A store failure is retried.
* **The traversal is breadth-first, in Go, one query per level and
  direction**, not a recursive CTE as ADR-0001 sketched. The walk carries
  state per path:
  - whether it is going up (who depends on the component) or down (what the
    component acts on);
  - whether the link is direct or indirect;
  - the tools a prompt's changed lines mention;
  - which agent versions it may enter.

  That state is awkward to express in SQL and easy to test in Go. A
  traversal costs a number of queries bounded by its depth, not by the size
  of the graph. The pure package (`graph`) reads through a `Reader`
  interface, so the same code runs against an in-memory graph in property
  tests (20,000 random graphs) and against PostgreSQL in integration tests.
* **Bounds.**
  - Depth is 4 by default and capped at 8. The node budget is 500; past it
    the result is marked `truncated`.
  - A cycle is visited once per direction.
  - Only the scoped agent version is entered, or the latest version of each
    agent when no scope is given.
  - A view for the UI has depth 2 by default (at most 4) and 150 components
    by default (at most 500). There is no whole-graph load.
* **Scoring is deterministic.** Each hop's strength is the parent's strength
  × the edge's confidence × the edge type's weight: 1.0 for uses, writes,
  can-mutate and retrieves-from; 0.9 for calls, publishes, depends-on and
  consumes; 0.6 for reads. The strength is multiplied by 0.6 when a link is
  indirect. The score is strength × 0.9^depth × what is declared about the
  component: a tool's risk tier, or a dependency's criticality. Severity
  buckets the score (≥ 0.75 critical, ≥ 0.5 high, ≥ 0.3 medium). Every
  affected component lists the factors and the path that produced it.
* **Some influence passes through the whole agent.** An agent version reached
  from its prompt, model or retrieval source acts differently through every
  tool it uses. One reached from a tool does not. A prompt change links the
  tools its changed lines name directly; the agent's other tools are reached
  as indirect and weigh less. A scenario that tests the whole agent is linked
  only through the agent, more weakly than a test of an affected tool.
* **Components outlive their edges.** Removing a mapping deletes the edges
  whose evidence it was, not the components. A component's history
  (`first_seen_at`, attributes other producers set) belongs to the project.
  The UI counts only components the view reaches.

## Consequences
* The graph can say why each edge exists and how far to trust it. The UI
  styles an edge by its evidence: observed, declared or inferred. The web
  page's filters (observed-only, declared-only) are exact.
* A blast radius costs at most `depth × 2` level queries plus one query for
  linked tests, whatever the project's size. The property tests pin the
  guarantees: depth bounded, every path step an edge in its direction, the
  same result whatever order the store answers in, a deeper bound never
  loses a component, scores in range, no agent version outside the scope.
* Orphan components stay in the project's totals until a later cleanup job.
  That job is a deliberate addition, not a side effect of editing a mapping.
* The environment a relationship holds in (spec §41.7) is not modelled yet.
  A relationship is known for the project. Environment-scoped evidence is a
  later column of `edge_evidence`.
* A graph database stays an upgrade trigger (ADR-0001): if graph
  algorithms, not bounded walks, become central.
