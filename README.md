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
> Decisions: [`docs/adr/`](docs/adr/) · Contracts: [`packages/contracts`](packages/contracts).

## Quick start (local)

```bash
cp .env.example .env
make dev        # builds and starts the full stack, seeds the demo workspace
make test       # unit + integration + frontend tests
```

## License

Apache-2.0
