# ADR-0027 — A change set is computed once from two registered versions, and never changes

* Status: accepted · Date: 2026-09-25

## Context
A release decision starts with a question: what changed? (spec §21, §118).
Between two versions of an agent the answer can be:

* the prompt
* the model and its parameters
* limits
* tools and their input schemas, risk and permissions
* retrieval sources and dependencies
* code
* a policy, an evaluator or a dataset, as declared by the author

A CI job asks the question and may ask it again. Two reviewers opening the same
change must see the same answer, even after later imports and new scenarios
change the graph. And the answer is not always exact: a prompt may be stored
only as a hash, and code is known only by file names.

## Decision
* **Computed from two registered versions of one agent.** `POST
  /api/v1/projects/{id}/change-sets` names a base and a candidate version.
  The control plane loads their manifests and compares them field by field in
  a pure package (`changes`). Git metadata (commits and changed file names)
  and changes that no manifest carries (policy, evaluator, dataset) come with
  the request.
* **Every item says how it is known:**
  - `exact`: both sides were compared.
  - `hash_only`: the text is not stored, so only its hash changed.
  - `filenames_only`: the code is known only by the names of the changed
    files, and not understood.
  - `declared`: the author stated it.

  A tool's input-schema change is classified following spec §118:
  - a required argument added
  - an argument removed
  - a type narrowed or widened
  - enum values removed or added
  - constraints tightened or loosened
  - descriptions changed

  Each is marked breaking or not. A risk escalation is a new privilege.
* **Prompt text is guarded as it is for versions.** When the project stores
  prompt text, the item carries a bounded line diff, with secrets and personal
  data masked. Only callers with `settings.read` see the diff. Everyone with
  read access sees the tools the changed lines name, matched as whole words in
  any case. That is enough to explain an impact without showing the prompt.
* **Each change is a seed of the blast radius, scoped to the candidate.**
  - A prompt change seeds the prompt, with the tools its changed lines
    mention.
  - A tool change seeds the tool.
  - A change the graph can only attribute to the whole agent (model
    parameters, limits, code) seeds the version.
  - The traversal enters only the candidate version (ADR-0026).

  Seeds are bounded to what the graph service accepts (summary 300
  characters, 100 mentions). The impact enters at most 100 seeds. Past that,
  it enters from the candidate version and says so.
* **Content-addressed and immutable.** A change set is keyed by the SHA-256
  of the normalized request. Asking again for the same two versions, title,
  Git metadata and declarations returns the stored change set with `200`. One
  `INSERT … ON CONFLICT` decides that, so a repeat and a concurrent request
  take the same path. An update trigger refuses any change to a stored row.
  Composite foreign keys hold both versions to the agent, and the agent to
  the project, in the schema as well as in the use case.
* **Creating requires `release.write`**, the permission CI keys hold. Reading
  requires read access to the project.

## Consequences
* A change set is evidence: a later release gate can name it and get exactly
  what was compared. The impact is not stored with it (ADR-0029). The graph
  and the scenario library move on, and the gate pins what it ran.
* What a change set cannot know, it says. A code change without file names is
  "code changed, commits only". A hash-only prompt change is a prompt change
  with no diff. The impact still enters from the right component.
* Re-running a CI job is free, and cannot create near-duplicate change sets
  that differ only by id.
* Changing how items are computed does not rewrite history. Old change sets
  keep the items they were stored with. A new computation is a new change set
  if the request differs.
