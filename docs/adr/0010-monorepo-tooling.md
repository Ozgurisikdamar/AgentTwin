# ADR-0010 — Monorepo tooling: one Go module, one uv workspace, one pnpm workspace

* Status: accepted · Date: 2026-09-24

## Decision
* **Go**: a single module at the repository root. Each Go service lives in
  `services/<name>` with `cmd/` and `internal/`; Go's `internal` visibility rule makes
  cross-service imports impossible. Shared code in `packages/gokit`. The CLI is
  `packages/cli`.
* **Python**: a `uv` workspace (Python 3.12) with one lockfile: `packages/core-py`
  (`agenttwin_core`), `packages/sdk-python` (`agenttwin`), both Python services and the
  demo agent.
* **TypeScript**: a `pnpm` workspace with `apps/web` and `packages/sdk-typescript`.
* Contracts (JSON Schema) live in `packages/contracts` and `packages/scenario-schema`
  and are consumed by all three languages from the same files: Go embeds them through
  tiny packages in those directories (`//go:embed`), Python locates them via
  `AGENTTWIN_SCHEMA_DIR` (set in container images) or the repository layout, and the
  web app imports them through the `@agenttwin/schemas` workspace package. There are
  no copies that could drift. RabbitMQ topology (`packages/contracts/topology.json`)
  is shared the same way.

## Consequences
One `make test` runs everything; lockfiles pin all dependencies.
