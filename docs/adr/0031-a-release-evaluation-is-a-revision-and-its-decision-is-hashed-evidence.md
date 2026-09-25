# ADR-0031 — A release evaluation is a revision, and its decision is hashed evidence

* Status: accepted · Date: 2026-09-25

## Context
A release asks whether a candidate version of an agent may replace its
baseline (spec §28, §38). The answer rests on several things that change
over time, independently of the release:

* the project's gate policy
* the change's impact: the graph and the scenario library move on
  (ADR-0029)
* the scenarios, which gain new versions
* the evaluation service's run

A CI job asks for the answer and may ask again. Weeks later, an auditor must
see exactly what the gate decided, on what, and whether someone let a blocked
release through (spec §91, §92). The gate must never turn history into a
false pass.

## Decision
* **A release is immutable.** It names the change set between baseline and
  candidate, the candidate commit and the CI run. An update trigger refuses
  any change.
* **Each evaluation is a revision, and records what it rests on when
  requested:**
  - the gate policy, with every default resolved
  - the change set's impact, as computed then
  - the suite pinned from it: each scenario at the version the impact
    selected (`latest_version_id`)
  - the candidate's tools

  A trigger lets a revision move only once, from `EVALUATING` to `DECIDED`,
  recording its run. Re-evaluating creates a new revision; earlier ones keep
  what they decided.
* **The evaluation service runs the pinned suite** (`evaluation.run_requested.v1`,
  ADR-0023). It gets both versions, the suite, the policy's budget and the
  release's seed: the first four bytes of `sha256("release-seed:" + release_id)`.
  Every revision of a release runs the same seeded faults, so a rerun is a rerun.
  The evaluation service creates one run per release evaluation (a unique
  index), so a redelivered request is harmless.
* **The gate decides when the run is reported** (`evaluation.run_completed.v1`).
  The control plane reads:
  - the run
  - every compared case, a few at a time
  - the judge's calibrations in the project

  It reads them for the release's project only, as a service principal. The
  pure `release` package maps them to the gate's input, and the `gate`
  package applies the rules of spec §28.
* **Missing evidence is never a pass.** Each of these is an incomplete
  `BLOCK` with a sentence that says why:
  - a run the evaluation service does not know
  - a run that belongs to another release evaluation
  - a run that failed or was cancelled
  - an evaluation service that is not configured
  - a suite whose scenarios the library could not confirm

  A service that cannot answer now is retried: the event is redelivered and
  the revision stays `EVALUATING`. A case the service no longer has keeps its
  statuses and loses its expectations. A suite with nothing to run is decided
  at once (an empty suite is a coverage warning).
* **The decision is stored once, with its input and their hash.** The
  decision holds every rule that fired, with its evidence, the counts,
  coverage and the risk index. It is stored with the complete gate input and
  `evidence_sha256`, the SHA-256 of the canonical JSON of both. Canonical
  JSON is the same form the audit chain uses, so the stored copies hash the
  same. An update trigger refuses any change. Reading a gate recomputes the
  hash and reports `evidence_verified`. A decision tampered with behind the
  database's back is then visible, not trusted.
* **An override is a separate record, and never changes the decision**
  (spec §92). It records who, why (10 to 2000 characters), an optional
  ticket and an optional expiry (at most 90 days). It applies to one
  decision:
  - only the latest revision's
  - only a `WARN` or a `BLOCK`
  - once

  A new evaluation is gated anew. While it applies, the gate reports
  `effective_outcome: OVERRIDDEN` and CI exit code 0, next to the decision's
  own outcome and `original_outcome`. When it expires, the gate blocks
  again and still says it was overridden. It requires `release.override`
  (owners, administrators, reviewers). Reviewers may override only when the
  project's gate policy allows it.
* **CI reads one field.** `exit_code` is 0 for a pass, 2 for a warning and 3
  for a block (spec §39), 0 while an override applies, and `null` while
  pending. `ci_fails` says whether CI should fail: always on a block, and on
  a warning when the policy says so.

## Consequences
* The gate at release time can be shown as it was, whatever changed since
  (spec §122). Old revisions keep the scenario versions, the policy and the
  impact they ran with.
* Every step is audited: created, evaluate, gate decided, override with its
  reason.
* The event consumer is idempotent: a redelivered completion finds the
  revision decided and reads nothing.
* An in-flight evaluation does not stop a new one. A CI job that restarts
  gets a new revision, and the older one still decides when its run is
  reported. A stuck evaluation service cannot lock a release.
* The decision is only as current as its evidence. Re-evaluating after new
  scenario versions or a new policy is an explicit act, and creates a new
  revision.
