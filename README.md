<div align="center">

<img src="docs/assets/agenttwin-hero.svg" alt="AgentTwin — Ship agents with evidence, not hope." width="100%"/>

<br/>

<img src="https://img.shields.io/badge/status-active%20development-0F766E?style=for-the-badge" alt="Active development"/>
<img src="https://img.shields.io/badge/license-Apache--2.0-4F46E5?style=for-the-badge" alt="Apache 2.0"/>
<img src="https://img.shields.io/badge/OpenTelemetry-native-5EEAD4?style=for-the-badge&logo=opentelemetry&logoColor=111827" alt="OpenTelemetry native"/>
<img src="https://img.shields.io/badge/Go-control%20plane-00ADD8?style=for-the-badge&logo=go&logoColor=white" alt="Go"/>
<img src="https://img.shields.io/badge/Python-eval%20%2B%20simulation-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python"/>
<img src="https://img.shields.io/badge/PostgreSQL-pgvector-336791?style=for-the-badge&logo=postgresql&logoColor=white" alt="PostgreSQL + pgvector"/>

<br/><br/>

**Pre-production assurance and runtime containment for production AI agents.**

AgentTwin helps teams answer one question:

### **Can I safely ship this agent change — and what evidence supports that decision?**

<br/>

<table>
<tr>
<td align="center"><strong>REPLAY</strong><br/><sub>production-shaped behavior</sub></td>
<td align="center"><strong>COMPARE</strong><br/><sub>baseline vs candidate</sub></td>
<td align="center"><strong>MAP</strong><br/><sub>dependency blast radius</sub></td>
<td align="center"><strong>GATE</strong><br/><sub>PASS / WARN / BLOCK</sub></td>
<td align="center"><strong>CONTAIN</strong><br/><sub>unsafe runtime actions</sub></td>
</tr>
</table>

<p>
<a href="#why-agenttwin"><b>Why</b></a> ·
<a href="#the-assurance-loop"><b>Assurance Loop</b></a> ·
<a href="#what-makes-it-different"><b>Differentiation</b></a> ·
<a href="#architecture"><b>Architecture</b></a> ·
<a href="#release-gate"><b>Release Gate</b></a> ·
<a href="#development"><b>Development</b></a>
</p>

</div>

---

## Why AgentTwin

AI agents can technically **succeed while being wrong**.

A request may return `200 OK`, the agent may produce a confident final answer, and the run may still have:

- called the wrong tool;
- passed dangerous arguments;
- retried an irreversible action;
- skipped required approval;
- followed a malicious instruction hidden in retrieved content;
- looped and burned budget;
- claimed success although external state never changed;
- changed behavior after a prompt, model, tool or permission update.

Traditional observability tells you **what happened**. AgentTwin is designed to turn that evidence into a decision about **what is safe to ship next**.

> **Tracing is infrastructure. The product is the closed loop from real behavior to regression protection, simulation, release evidence and runtime containment.**

---

## The assurance loop

<div align="center">

<img src="docs/assets/assurance-loop.svg" alt="AgentTwin assurance loop" width="100%"/>

</div>

The key workflow is deliberately circular:

```text
production behavior
      ↓
trace + verified outcome evidence
      ↓
failure / regression mining
      ↓
permanent regression case
      ↓
change detection + blast radius
      ↓
production-shaped twin simulation
      ↓
deterministic + semantic evaluation
      ↓
explainable release gate
      ↓
canary / production
      ↓
runtime containment
      └──────────────→ back to evidence
```

A bad production trace should not end as an incident note. It should become a **durable test that protects every future release**.

---

## What makes it different

<table>
<tr>
<td width="50%" valign="top">

### 🧬 Production failures become regression protection

AgentTwin can turn failed outcomes, policy violations, loops, unsafe retries and human-flagged traces into reviewed **regression cases** that automatically join future release checks.

</td>
<td width="50%" valign="top">

### 🪞 Production-shaped digital twins

Scenarios are more than prompts. They include initial state, tool twins, permissions, expected outcomes, fault rules and final-state assertions so agents are tested against **stateful reality**, not toy inputs.

</td>
</tr>
<tr>
<td width="50%" valign="top">

### 🕸️ Change-aware blast radius

A prompt, model, tool schema, policy or permission change is mapped to affected components, dependency paths, historical failures and relevant scenarios — with evidence for **why each test was selected**.

</td>
<td width="50%" valign="top">

### 🧭 Trajectory-aware evaluation

AgentTwin evaluates final outcome **and** the path taken: tool choice, arguments, ordering, retries, approvals, loops, side effects, latency, cost, policy decisions and verified final state.

</td>
</tr>
<tr>
<td width="50%" valign="top">

### 🚦 Explainable release gates

Release decisions are **PASS / WARN / BLOCK** with machine-readable evidence. One average score can never hide a new critical irreversible failure.

</td>
<td width="50%" valign="top">

### 🛡️ Runtime containment

The optional runtime gateway can allow, constrain, require approval or deny tool actions using explicit policy, idempotency and audit — especially for irreversible operations.

</td>
</tr>
</table>

---

## The questions AgentTwin asks before you ship

```text
WHAT CHANGED?
      ↓
WHAT CAN THAT CHANGE AFFECT?
      ↓
WHICH REAL BEHAVIORS SHOULD WE REPLAY?
      ↓
WHAT FAILS IN A SAFE TWIN?
      ↓
IS THIS CHANGE SAFE TO SHIP?
      ↓
WHAT SHOULD BE WATCHED OR CONTAINED AFTER RELEASE?
```

This is the product center of gravity — not token charts, prompt playgrounds or another generic trace viewer.

---

## Architecture

<div align="center">

<img src="docs/assets/architecture.svg" alt="AgentTwin target V1 architecture" width="100%"/>

</div>

AgentTwin uses microservices where there is a real difference in **workload, language/runtime, scaling, failure mode or security boundary** — not for microservice theatre.

| Service | Runtime | Responsibility |
| --- | --- | --- |
| **control-plane** | Go | auth, tenancy, projects, agents, releases, gate decisions, audit, API edge |
| **trace-service** | Go | OTLP ingestion, normalization, traces, spans, outcomes |
| **graph-service** | Go | components, dependency evidence, bounded blast-radius traversal |
| **evaluation-service** | Python / FastAPI | datasets, evaluators, comparisons, regression mining, embeddings, review |
| **simulation-service** | Python / FastAPI | scenarios, stateful tool twins, replay, fault injection, evidence |
| **runtime-gateway** | Go | policy decisions, approvals, idempotency, outbound safety, audit |

The V1 infrastructure stays intentionally lean:

**PostgreSQL + pgvector · RabbitMQ · OpenTelemetry Collector · S3-compatible object storage · Docker Compose**

Kafka, Qdrant, Neo4j, ClickHouse, Temporal, Kubernetes and Redis are **not default dependencies**. They only enter when measured scaling needs justify them.

Full architecture: [docs/architecture/system.md](docs/architecture/system.md)

---

## Release gate

<div align="center">

<img src="docs/assets/release-gate.svg" alt="AgentTwin explainable PASS WARN BLOCK release gate" width="100%"/>

</div>

The gate is evidence-first. A secondary risk index may help sorting, but it is never the authority.

### BLOCK examples

Critical deterministic scenario failure · cross-tenant leakage · missing approval · duplicate irreversible side effect · authorization bypass · prompt-injection critical failure · a claimed success disproven by state verification.

### WARN examples

Meaningful latency or cost regression · new tool permission · new affected dependency without test coverage · evaluator uncertainty · insufficient simulation coverage.

### PASS

Critical gates are green and configured policy thresholds are satisfied.

---

## Core product surfaces

<table>
<tr>
<td width="33%" valign="top">

### Trace Explorer

Inspect trajectories, tool calls, model calls, policy decisions, retries, outcome verification and linked regressions.

</td>
<td width="33%" valign="top">

### Regression Miner

Redact, match, embed, group, classify and promote production failures into permanent tests.

</td>
<td width="33%" valign="top">

### Scenario Library

Versioned scenarios with state, tool twins, fault injection, evaluators, severity and reproducible seeds.

</td>
</tr>
<tr>
<td width="33%" valign="top">

### Blast Radius

Map changed components to impacted dependencies, scenarios, policies and historical failures.

</td>
<td width="33%" valign="top">

### Baseline vs Candidate

Replay identical initial states and surface improved, unchanged, regressed and new-critical-failure slices.

</td>
<td width="33%" valign="top">

### Runtime Gateway

Enforce action policy with `allow`, `allow_with_limits`, `require_approval` and `deny`.

</td>
</tr>
</table>

---

## Security by design

AgentTwin treats agent reliability and agent security as the same production problem.

- **Content capture is off by default.**
- Client-side redaction supports drop, mask and hash modes.
- Tenant identity comes from verified credentials — not agent-supplied span attributes.
- Irreversible runtime actions fail closed when policy evaluation fails.
- Approval tokens are single-use, short-lived and bound to the exact action hash.
- Simulation workers are designed for isolation and restricted egress.
- Remote tool descriptions and results are treated as untrusted input.
- Multi-tenant isolation, SSRF controls and prompt-injection suites are release-blocking concerns.
- Gate decisions and evidence are designed to be immutable and auditable.

Threat model: [docs/security/threat-model.md](docs/security/threat-model.md)

---

## Agent Assurance Coverage

Traditional line coverage does not describe agent safety.

AgentTwin is designed to measure coverage across:

| Coverage surface | Example question |
| --- | --- |
| **Tool coverage** | Did we exercise every tool the agent can call? |
| **Critical tool coverage** | Did we test every irreversible action? |
| **Policy coverage** | Did scenarios exercise active policies? |
| **Failure-mode coverage** | Which known failure classes were replayed? |
| **Regression coverage** | Did every promoted regression run again? |
| **Dependency coverage** | Are affected components represented in the suite? |
| **Fault coverage** | Did we test timeouts, 429/500/503, stale and duplicate responses? |
| **Outcome verification** | Did we verify business state instead of trusting agent self-report? |

AgentTwin does **not** turn these into a misleading “95% safe” claim.

---

## Development

> **AgentTwin is under active phased development.** The architecture, ADRs, contracts and shared foundation are being implemented before the complete product stack is declared ready.

Track live build status: [docs/plan/implementation-board.md](docs/plan/implementation-board.md)

### Intended local developer experience

```bash
git clone https://github.com/Ozgurisikdamar/AgentTwin.git
cd AgentTwin

cp .env.example .env
make dev
```

The repository contract is that the finished local stack should be able to boot from one documented command, seed the demo workspace, run the demo agent, ingest traces, execute simulations and produce an evidence-backed release decision.

Useful developer entrypoints:

```bash
make help
make test
make lint
make doctor
```

---

## Repository map

```text
docs/
  architecture/       system architecture and domain references
  adr/                architectural decision records
  plan/               phased implementation board
  security/           threat model and security design

packages/
  contracts/          shared versioned event / API contracts
  gokit/              shared Go infrastructure primitives
  scenario-schema/    versioned scenario definitions

infra/
  otel/               OpenTelemetry collector configuration
  prometheus/         metrics collection
  grafana/            local observability provisioning
```

As implementation phases land, service, SDK, web and demo-agent directories join this foundation.

---

## Design principles

1. **Deterministic where possible.** Use an LLM only when semantic reasoning genuinely adds value.
2. **Evaluate trajectories, not only final text.**
3. **Outcome evidence beats agent self-report.**
4. **Production failures become future tests.**
5. **Simulation should resemble deployment.**
6. **Blast radius is probability × possible damage.**
7. **Human approval is selective, not universal.**
8. **Every release decision must be explainable.**
9. **No bespoke ML model until reviewed data justifies it.**
10. **No infrastructure component without a measured reason to exist.**

---

## Documentation

| Document | Purpose |
| --- | --- |
| [System Architecture](docs/architecture/system.md) | C4-style architecture, request paths, data ownership and events |
| [Implementation Board](docs/plan/implementation-board.md) | Build phases and acceptance criteria |
| [Architecture Decisions](docs/adr/) | Why the system is built this way |
| [Threat Model](docs/security/threat-model.md) | Security boundaries and abuse cases |
| [Shared Contracts](packages/contracts/README.md) | Versioned internal event and contract definitions |

---

## License

Apache License 2.0 — see [LICENSE](LICENSE).

<br/>

<div align="center">

### **Ship agents with evidence, not hope.**

<sub>AgentTwin · Simulation · Regression Mining · Blast Radius · Release Assurance · Runtime Containment</sub>

</div>
