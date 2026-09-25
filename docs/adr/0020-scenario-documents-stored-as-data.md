# ADR-0020 — Scenario documents are stored as data and exported in reading order

* Status: accepted · Date: 2026-09-25

## Context
Scenarios are authored as YAML — in repositories, in the UI — and must be
versioned, hashed, compared and pinned by simulation runs (ADR-0017). YAML text
carries comments and layout that no two tools preserve the same way. PostgreSQL
`jsonb` returns object keys ordered by length, not as written, so a naive export
began with `kind`, `spec` and only then `metadata`.

## Decision
* **The API accepts YAML or JSON** and parses YAML with the safe loader (§112):
  plain data only, no anchors or aliases, bounded size (512 KiB), depth and node
  count, duplicate keys rejected, YAML 1.2 core scalars.
* **The stored form is the data.** A document is validated (JSON Schema,
  evaluator parameters, cross-checks against its twin) and stored as `jsonb`
  with a spec hash (SHA-256 of canonical JSON). Saving creates a new immutable
  version only when the hash changes; identical content answers "no changes".
* **Saving is by name** within a project: a new name creates a scenario, an
  existing name adds a version to it (and restores it if archived). The editor
  warns before a new or renamed scenario would add a version to another one.
* **Comments are not kept.** The UI edits the YAML text in place, so comments
  and layout survive while editing, and states that they are not stored.
  Annotated sources belong in the repository and are loaded with the SDK or the
  seed.
* **Exports use a fixed reading order:** `apiVersion`, `kind`, `metadata`,
  `spec`; inside them the fields in the order people read them (name, severity,
  tags, owner, description; agent, twin, seed, covers, input, state, faults,
  expectations; an expectation's `id` and `type` first and `critical` last).
  Other keys follow alphabetically.

## Consequences
* A scenario's identity is independent of formatting: re-indenting a file does
  not create a version, and runs pin content rather than text.
* Exports are stable and diffable, but differ from the author's original layout;
  the canonical form is documented rather than hidden.
