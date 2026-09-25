# ADR-0025 — The edge names the projects a person may act in

* Status: accepted · Date: 2026-09-25

## Context
A person is a member of an organization and may act in every project of it:
their principal says `all_projects`. The edge (the control plane) forwards
public requests to the internal services with a short-lived internal token
carrying that principal (ADR-0009), and every service checks project access
with `can_access_project(project_id)`.

A service cannot tell which organization a project id belongs to: projects
live in the control plane's schema. With `all_projects` in the token, a
person of organization B naming organization A's project id passed the check
in every service. Reads stayed empty, because every query also filters by
the caller's organization. Writes did not: they were stored under B's
organization and A's project id. Where a uniqueness key left the
organization out, B could take a name in A's project, such as a scenario or
dataset name (`UNIQUE (project_id, name)`), or block a graph component. The
graph service's integration tests found it when a caller of another
organization mapped a tool of A's project.

## Decision
* **The internal token names the projects.** For a principal with
  `all_projects`, the edge reads the organization's project ids on every
  proxied request and mints the token with exactly those,
  `all_projects: false`. A service's existing check then refuses another
  organization's project with `404`, as for a missing one. No service
  changes. API keys and service principals already name their projects.
* **Fail closed.** If the list cannot be read, the edge answers `503` and
  forwards nothing. A token that cannot be minted is the edge's `500`, not
  a `401` from the service.
* **An organization has at most 100 projects** (`authn.MaxTokenProjects`).
  A token naming 100 projects is about 5 KiB, under the 8 KiB some proxies
  allow and the 16 KiB the Python services' HTTP parser allows. Creating
  project 101 answers `409 PROJECT_LIMIT_REACHED`. The count is taken with
  the organization row locked, so concurrent creations cannot pass the
  limit. `Mint` refuses more.
* **No cache.** The list comes from one indexed query per request, so a
  project created a moment ago is usable at once. The edge already reads
  the database on every authenticated request.
* **Keys include the organization where it costs nothing.** The graph
  service's component key is `(organization, project, kind, key)`, so a
  mistake anywhere upstream still cannot let two organizations collide in
  it.

## Consequences
* Cross-organization project ids are refused at every service, for writes
  as for reads, with one change at the edge. Tests cover the proxy (unit),
  the edge end to end (integration), the bound on tokens, and the limit
  under concurrency. Each protection was removed once to see its test fail.
* The simulation and evaluation services keep `UNIQUE (project_id, name)`.
  With the edge naming projects, only a caller of the project's own
  organization reaches those rows. Adding the organization to those keys
  is a later migration, not a precondition.
* The limit of 100 projects per organization is a product rule now. Raising
  it far means changing how a token names projects (a project claim per
  request, or a lookup by the services), not just the constant.
