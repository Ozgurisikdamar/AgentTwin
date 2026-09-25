# ADR-0029 — Change impact is computed when asked, explains every scenario, and is never silently partial

* Status: accepted · Date: 2026-09-25

## Context
Spec §22 defines the impacted suite of a change as the union of four sets:

1. the scenarios the dependency graph links to the changed components;
2. the known production regressions;
3. the scenarios semantically close to what changed;
4. the scenarios the gate policy always runs.

The knowledge is split across services. The control plane owns the change
set (ADR-0027). The graph service owns the blast radius (ADR-0026). The
simulation service owns the scenario library and its embeddings (ADR-0014).

The Phase 4 acceptance asks that a prompt or a tool change select the refund
scenarios and explain why. A suite missing a scenario because one service was
down would look like a smaller, cheaper change. That is the kind of silent
failure a release gate exists to prevent.

## Decision
* **One read, computed on each request.** `GET
  /api/v1/change-sets/{id}/impact` makes two calls. The graph and the
  scenario library keep changing after a change set is stored, so the impact
  is not stored with it. The release gate (Phase 5) pins the suite it ran.
  1. It asks the graph service for the blast radius of the change set's
     seeds, entered at the candidate version, at the policy's maximum depth.
  2. It asks the simulation service (`POST /api/v1/scenarios/match`) for one
     answer covering:
     - the scenarios the graph linked, by name;
     - the policy's always-run tags;
     - the known production regressions, if the policy includes them;
     - similarity queries built from the change items.
* **The services act for the caller, in one project.** Both calls carry the
  caller's own identity, narrowed to the change set's project. The impact
  requires read access and exposes no more than the caller could read
  directly. A viewer gets the same scenarios and reasons without any prompt
  text.
* **Similarity queries are built from what changed, bounded.** The queries
  are:
  - the changed prompt lines, as stored (masked);
  - a tool's description;
  - retrieval sources;
  - declared changes;
  - changed file names.

  There are at most 50 queries of 8,000 bytes each, cut on a character
  boundary. A scenario is similar when its cosine similarity is at least
  0.25, with at most 10 scenarios per query. The answer names the embedding
  model. Under `hashing-v1` the UI says "text similarity", never "semantic"
  (ADR-0014). A similarity names its query by id and never repeats the
  query's text.
* **Every scenario carries every reason, and a sentence for each:**
  - "tests tool refund_payment, which the change to the prompt reaches in 2
    steps";
  - "its description is close to the change of the prompt (similarity
    0.45)";
  - "always runs (tagged security)";
  - "a known production regression".

  A graph link has a strength. A weak link (only through the agent, or
  through a tool the change does not name) is stated only when there is no
  stronger one, but the reasons list keeps every link. Scenarios are ordered
  by severity, then by name, so the same inputs give the same list. The
  impact also lists:
  - the most affected components, with their paths;
  - the irreversible actions;
  - the new privileges.
* **Incomplete is said, not hidden.** Any of the following gives `complete:
  false`:
  - a service that is not configured, cannot be reached or fails;
  - an embedding provider that cannot answer (the simulation service's
    `503 EMBEDDINGS_UNAVAILABLE`);
  - a blast radius cut at its node bound;
  - a match answer cut at its bound.

  The impact then lists what it does know and adds:
  - `problems`, naming each service and its error code;
  - `notes`, saying what is missing (for example: "Without the dependency
    graph, scenarios linked to the changed components are missing").

  A change with no seeds the graph knows is not sent to the graph service;
  its empty answer is exact, not a failure.

## Consequences
* On the demo, the prompt rewrite 1.2.4 → 1.3.0 reaches prompt →
  support-refund-agent@1.3.0 → refund_payment → payments-api. It requires
  nine scenarios. Seven are refund scenarios, linked through the tools the
  changed lines name, with text similarity 0.28–0.50. The two security
  scenarios run by tag and are linked weakly through the agent. The tool
  change 1.3.1 → 1.3.2 requires the same seven, because "tests tool
  refund_payment, which changed", and the security two.
* The gate (Phase 5) must treat `complete: false` as incomplete evidence. It
  must never run a smaller suite because a service was down. When it runs,
  it pins the selected suite.
* The impact is recomputed on each read, so it changes as the graph and the
  scenario library grow. That is intended, for a change set under review.
  The immutable record is the gate's evidence, not this answer.
* Switching to a hosted embedding model is a configuration change. Stored
  vectors of another model are recomputed lazily (ADR-0014).
