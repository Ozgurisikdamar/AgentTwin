# ADR-0032 — Regressions are mined from deterministic signals, grouped by similarity and promoted by a person

* Status: accepted · Date: 2026-09-25

## Context
Spec §18 makes the regression miner a central feature: a failing production
trace should become a permanent test that every later release runs (§3.4).
The pipeline it asks for is: redact, extract features, match known failures,
group by similarity, suggest a taxonomy and a severity, have a person review,
promote to a regression case.

Most of the plumbing already exists:

* **Trace service.** It finalizes every trace with a summary: tools, errors,
  violations, outcome, retry and step counts, failing tool, error type,
  tool-sequence sketch. It also derives deterministic signals from it:
  `duplicate_side_effect`, `contradiction`, `timeout_after_mutation`,
  `loop_detected`, `tool_error`, `outcome_failure` and more. These travel on
  `trace.ingested.v1`. A later verified outcome travels on
  `trace.outcome_recorded.v1`, and a person's flag on `trace.flagged.v1`.
  All three are already bound to the evaluation service's queue, and the
  service drops them.
* **Release gate.** Change impact selects scenarios whose metadata `source`
  is `production_regression` when the gate policy includes known regressions
  (the default). The release suite makes them mandatory, and the gate blocks
  on the `known_regression` rule when one fails again (ADR-0029, ADR-0031).
* **Datasets** already take cases from production. A case must be redacted
  and names the trace it came from.

The miner is the missing piece between them.

## Decision
* **The evaluation service mines.** It owns datasets and the evaluation of
  cases, so a regression case is one of its records. It consumes the three
  trace events from its existing queue. Each event is processed once
  (`processed_event`), in one transaction with its effects.
  - `trace.ingested.v1` carries the finalized trace's summary and signals.
  - `trace.outcome_recorded.v1` and `trace.flagged.v1` say something new
    about a trace, so the miner reads its detail again from the trace
    service, the authority on it. A flag that arrives before the trace is
    finalized is kept (`regression_flag`) and joins the trace's ingestion.
    If the trace service cannot be reached, the event is retried; a trace it
    no longer knows (retention) is let go.
  - Mining for one project's agent is serialized (an advisory lock), so two
    failures of a new kind that arrive together make one group.
* **Candidates come from deterministic signals, never from a model.** A
  production trace (not a simulation) becomes a candidate when any of these
  holds:
  - its outcome failed or was contradicted;
  - it took the same irreversible action twice;
  - it retried a write without an idempotency key;
  - it looped;
  - a tool failed;
  - a policy denied an action;
  - it timed out after a mutation;
  - a person flagged it (incident, negative feedback, manual).

  An unverified outcome alone is not enough: most production traces are
  unverified, and flooding the inbox would make it useless. A later outcome
  or flag updates the candidate, or creates it.
* **Features are stored apart from content.** A candidate keeps only
  structured features (§44): agent, version, environment, outcome, failing
  tool, error type, retry and step counts, violations, signals, the
  tool-sequence sketch, prompt hash, model and whether the claimed outcome was
  contradicted. It never keeps the conversation. The text it embeds is a
  sentence built from those features, for example "refund_payment timed out
  after a mutation; retried without an idempotency key; took an irreversible
  action twice; claimed success the final state disproved". So the embedding
  holds no customer data either.
* **Grouping, in this order:**
  1. **Known failure.** The fingerprint is a hash of the agent, the suggested
     taxonomy, the failing tool, the error type and the kinds of violation.
     A candidate whose fingerprint matches an existing group joins it.
  2. **Similarity.** Otherwise the candidate joins the group of its nearest
     neighbour among the same agent's candidates in the same project, if the
     cosine similarity is at least `REGRESSION_SIMILARITY_THRESHOLD` (0.85 by
     default). Vectors come from the configured embedder (`hashing-v1` by
     default, ADR-0014) and are stored in pgvector.
  3. **Neither.** The candidate becomes a group of one. It stays one:
     nothing is forced into a cluster (§18).

  A fingerprint that joined a group by similarity is mapped to it, so the
  next failure like it is a known one.

  What is learnt later can change a trace's kind (its outcome turns out to
  be contradicted, say). Its occurrence then moves to the group of its new
  kind, found the same way; a trace whose failure is corrected away (a
  success after all) leaves its group. A group left empty is deleted if
  nobody acted on it (no status change, triage, assignment or merge);
  otherwise it stays.

  A group shows its representative occurrence (the first, until it leaves;
  then the worst and earliest): its label, title, component and evidence.
  Its counts, first and last seen, versions, environments and severity (the
  worst of its occurrences) are derived from its occurrences. A label or a
  severity a person set is kept.

  Density clustering (HDBSCAN) is not used in V1. A project should have at
  least 500 candidates and 50 groups reviewed by people before a density
  method can be compared with nearest-neighbour grouping on real data.
  Below that, a threshold is as good and can be explained in one sentence.
* **Taxonomy and severity are suggested by rules, each with its evidence.**
  The rules map signals and violations to the §18 taxonomy:
  - duplicate irreversible action → `DUPLICATE_SIDE_EFFECT`;
  - contradicted success → `HALLUCINATED_SUCCESS`;
  - retry without an idempotency key → `RETRY_SAFETY`;
  - loop → `LOOP`;
  - denied cross-tenant access → `AUTHORIZATION`;
  - policy denial → `POLICY_VIOLATION`;
  - timeout → `TIMEOUT`;
  - other tool errors → `TOOL_ERROR_HANDLING`;
  - anything else → `UNKNOWN`.

  One is primary and the others are secondary. A duplicated irreversible
  action or a contradicted irreversible action is critical. A person can
  change the taxonomy, the severity and custom tags.
* **A person promotes; the service drafts.** A reviewer (`regression.promote`:
  owners, admins, reviewers) promotes a group. Engineers triage it: confirm,
  dismiss, merge, assign, change severity (`review.write`).

  The draft is derived deterministically from the group's representative
  trace, when asked, and never stored beforehand:
  - **Input.** The input message, and the request context the agent recorded
    (ADR-0011: tenant, customer).
  - **Faults.** The faults the trace suffered. A timeout after a mutation on
    call 1 of `refund_payment` becomes that fault.
  - **Expectations.** Those that catch the failure: no duplicate side effect
    on the tool; success backed by the state; the state the verified outcome
    expected.
  - **Twin entities.** A production entity, such as an order created
    yesterday, does not exist in the tool twin. The draft reads the entity
    back through the twin's own response templates, from what each read tool
    returned. It maps the entity to the twin record of the same tenant that
    agrees on the most observed fields and renames it in the input and the
    expectations. Every mapping is listed with its agreeing and differing
    fields.
  - **Missing content.** If the trace's content is gone (content mode
    `off`, or purged by retention), the draft says so and the person writes
    the input.

  The person may edit the draft before promoting it. The text is redacted
  again with the SDK's redactor, and the regression case records that.
* **Promotion writes in two steps, idempotently.**
  1. The scenario is created in the simulation service with `source:
     production_regression`, `sourceTraceId` and `generated.reviewed: true`.
  2. One local transaction adds it to the project's
     `production-regressions` dataset as a new version, links the group to
     the scenario and the dataset version, marks the group `PROMOTED` and
     writes the audit entry.

  If the transaction fails after the scenario exists, a retry finds the
  scenario by name and completes the second step. Every later release of the
  agent runs the scenario, with no extra wiring: the impact selects known
  regressions, and the gate makes them mandatory.
* **Lifecycle (§121).** A group moves through `CANDIDATE`, `CONFIRMED`,
  `PROMOTED`, `FIXED`, `DISMISSED` and `REOPENED`.
  - **Fixed.** A promoted (or reopened) group becomes `FIXED` when an
    evaluation run's candidate passes its scenario, in the transaction that
    completes the run. The version and the run are recorded, and the
    scenario stays in the suite.
  - **Reopened.** A new production failure joining a fixed group, from the
    fixed version or a later one, reopens it. What is learnt later about a
    failure already counted does not.
  - **Dismissed.** A dismissed group keeps counting new occurrences but
    stays dismissed, so known noise stays quiet.

  Every transition is recorded with its actor and reason.
* **Tenancy.** Every read and write is scoped to one project. Nearest
  neighbours are searched only among the same project's and agent's vectors,
  so regression embeddings never cross tenants (§35).

## Consequences
* A production failure reaches a future gate in three steps: the miner
  groups it, a person reviews and promotes it, and the next release runs it.
* The inbox shows groups, not traces. A burst of identical failures is one
  row with a count and first and last seen.
* The miner needs no model and no key. A hosted embedder improves grouping
  without changing the design.
* The draft is only as good as the trace: content must be captured
  (`redacted` or `full`) for the input to be recovered. The entity mapping
  is a documented heuristic that the person checks, not a guarantee.
* The `regression.candidate_created.v1` event of spec §36 is not published
  yet: an event is published only when something consumes it (contracts
  README). It is defined with its first consumer (notifications, Phase 8);
  until then new groups appear in the inbox.
