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

## What works today (Phases 1–3)

* **Trace ingestion** from any OpenTelemetry-instrumented agent through the
  OTel Collector into the trace service: GenAI semantic conventions, per-project
  content policy, idempotent ingest, deterministic trace summaries, claimed vs
  verified outcomes with contradiction detection.
* **Python SDK** ([`packages/sdk-python`](packages/sdk-python)) with client-side
  redaction, content capture off by default and measured overhead
  ([benchmarks](docs/benchmarks/sdk-overhead.md)); it also starts simulations,
  builds datasets and runs evaluations.
* **Control plane**: dev and OIDC authentication, organizations, projects,
  RBAC, API keys, immutable agent manifests and versions, hash-chained audit log.
* **Simulation**: versioned scenarios run against declarative, stateful tool
  twins with injected faults (timeout after mutation, a tool that lies about
  success, rate limits…); every case records its trajectory, the twin's state
  diff and a verdict per expectation; the same seed reproduces the verdicts.
* **Evaluation**: deterministic expectations (state, trajectory, policy,
  side effects) and semantic ones graded by a judge — a deterministic fake by
  default, Anthropic or any OpenAI-compatible model behind configuration
  (`JUDGE_PROVIDER`), with a spending budget and a verdict cache. An
  evaluation runs a baseline and a candidate version on one pinned suite and
  seed, and classifies every case: new critical failure, regressed,
  incomplete, improved or unchanged, with the first step where the two
  diverged. Versioned datasets, human reviews that replace a verdict, and
  judge calibration against labels people gave (agreement and Cohen's kappa).
* **Web UI**: trace explorer and trace detail with waterfall; scenarios and
  simulations with the evidence down to the tool call and the state diff;
  evaluations, compared cases side by side, datasets, the review queue and
  judge calibration.
* **Demo**: *Demo Co*'s support-refund agent with production-like tools,
  driven by a deterministic scripted planner (or Anthropic Claude when
  `DEMO_AGENT_MODEL=anthropic` and a key are set). Version 1.2.4 is good,
  1.3.0 regresses (it refunds before checking the policy and trusts a tool's
  false success), 1.3.1 fixes it. `make dev` seeds 9 scenarios, simulations
  of 1.2.4 and 1.3.0, the `refund-regression-suite` dataset and an evaluation
  of 1.3.0 against 1.2.4 that finds the regression: two new critical failures
  and one regressed case.

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
