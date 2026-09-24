# AgentTwin

**Ship agents with evidence, not hope.**

Pre-production assurance and runtime containment for production AI agents:
change detection, dependency blast radius, production-shaped simulation with
stateful tool twins, deterministic + semantic evaluation, an explainable
PASS / WARN / BLOCK release gate, a regression miner that turns production
failures into permanent tests, and a runtime gateway that enforces tool policies.

> This repository is being built in phases (see
> [`docs/plan/implementation-board.md`](docs/plan/implementation-board.md)).
> Architecture: [`docs/architecture/system.md`](docs/architecture/system.md) ·
> Decisions: [`docs/adr/`](docs/adr/) · Contracts: [`packages/contracts`](packages/contracts) ·
> Threat model: [`docs/security/threat-model.md`](docs/security/threat-model.md).

## What works today (Phase 1)

* **Trace ingestion** from any OpenTelemetry-instrumented agent through the
  OTel Collector into the trace service: GenAI semantic conventions, per-project
  content policy, idempotent ingest, deterministic trace summaries, claimed vs
  verified outcomes with contradiction detection.
* **Python SDK** ([`packages/sdk-python`](packages/sdk-python)) with client-side
  redaction, content capture off by default and measured overhead
  ([benchmarks](docs/benchmarks/sdk-overhead.md)).
* **Control plane**: dev and OIDC authentication, organizations, projects,
  RBAC, API keys, immutable agent manifests and versions, hash-chained audit log.
* **Web UI**: trace explorer (filters, facets, live refresh), trace detail with
  waterfall, span details, outcome evidence and summary; overview and agents.
* **Demo**: *Demo Co*'s support-refund agent with production-like tools and
  fault injection, driven by a deterministic scripted planner (or Anthropic
  Claude when `DEMO_AGENT_MODEL=anthropic` and a key are set).

## Quick start (local)

Requirements: Docker with Compose 2.20+, `make`, `curl`.

```bash
cp .env.example .env
make dev        # builds and starts the stack, waits until healthy, seeds the demo workspace
```

Then open <http://localhost:3000> and sign in with a demo account
(development authentication):

| Account | Role |
|---|---|
| `owner@demo.agenttwin.dev` | owner |
| `admin@demo.agenttwin.dev` | admin |
| `engineer@demo.agenttwin.dev` | engineer |
| `reviewer@demo.agenttwin.dev` | reviewer |
| `viewer@demo.agenttwin.dev` | viewer |

Useful commands:

```bash
make demo       # one more refund conversation through the demo agent; prints the trace link
make doctor     # checks Docker, env, ports, PostgreSQL, RabbitMQ, OTel, migrations, services
make e2e        # Playwright tests against the running stack
make test       # unit + integration (real PostgreSQL + RabbitMQ) + frontend tests
make lint       # formatters, linters and type checkers for Go, Python and TypeScript
make down       # stop (keeps data) · make reset: wipe data and start again
```

Instrument your own agent with the Python SDK: see
[`packages/sdk-python/README.md`](packages/sdk-python/README.md). Point
`AGENTTWIN_OTLP_ENDPOINT` at `http://localhost:4318` and use the demo project
key from `.env` (`AGENTTWIN_DEMO_API_KEY`).

## License

Apache-2.0
