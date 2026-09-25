# Domain glossary

| Term | Meaning in AgentTwin | Owner |
|---|---|---|
| **Organization** | Tenant boundary. Every tenant-owned row carries `organization_id`. | control-plane |
| **Membership / Role** | User ↔ organization with one role: OWNER, ADMIN, ENGINEER, REVIEWER, VIEWER. | control-plane |
| **Project** | A product area inside an organization (e.g. `support`). Holds agents, tools, scenarios, policies, settings (content capture, retention). | control-plane |
| **Environment** | `dev`, `staging`, `production` or custom label on traces, runs and policies. | control-plane |
| **API key** | Project-scoped machine credential `atk_<prefix>_<secret>` with scopes (`traces:write`, `releases:check`, `runtime:invoke`, `read`). | control-plane |
| **Agent** | A named agent in a project (`support-refund-agent`). | control-plane |
| **Agent version** | Immutable registration of a manifest (`agenttwin.dev/v1 Agent`) with manifest hash, prompt hash, model config, tool references, commit metadata. | control-plane |
| **Agent manifest** | Human-readable YAML describing model, limits, tools (+risk, approval), data capture and expected outcomes. | contracts |
| **Tool** | A capability an agent can call. Has a risk tier (READ, WRITE_REVERSIBLE, WRITE_IRREVERSIBLE, EXECUTE, ADMIN), optional risk dimensions and an input schema. Unknown tools default to WRITE_IRREVERSIBLE. | control-plane |
| **Trace / Span** | One agent run and its steps, normalized from OTel (`AGENT_RUN`, `MODEL`, `TOOL`, `RETRIEVAL`, `POLICY`, `OUTCOME`, `HTTP`). | trace-service |
| **Outcome** | First-class result of a trace: SUCCESS / PARTIAL / FAILURE / UNKNOWN + verification source. A natural-language claim never marks an outcome verified. | trace-service |
| **Scenario** | Executable test: input, initial twin state, allowed tools, faults, expectations, severity, seed, tags, coverage. Versioned; YAML import/export. | simulation-service |
| **Tool twin** | Stateful, isolated stand-in for real tools (declarative rules, OpenAPI-derived, MCP-derived, record/replay, custom adapter). | simulation-service |
| **Fault rule** | Injected failure for a tool call (timeout before/after mutation, 429, 5xx, malformed JSON, success-without-mutation, …). | simulation-service |
| **Simulation run** | Execution of a suite of scenarios against one agent version with pinned inputs. State machine QUEUED → PREPARING → RUNNING → EVALUATING → COMPLETED/FAILED/CANCELLED. | simulation-service |
| **Evaluator** | Versioned function from run evidence to `PASS/FAIL/ERROR/SKIPPED` + score + evidence. Deterministic, trajectory or semantic (judge). | evaluation-service |
| **Dataset / Eval case** | Versioned collection of scenario-backed cases with tags, severity, source and owner. | evaluation-service |
| **Eval run / Comparison** | Evaluation of baseline and candidate simulation results — one pinned suite and seed for both sides — with a class per scenario (NEW_CRITICAL_FAILURE, REGRESSED, INCOMPLETE, IMPROVED, UNCHANGED), metric deltas and first divergence. INCOMPLETE means a side could not be graded; it is never read as a pass. | evaluation-service |
| **Human review** | A person's verdict on one expectation of one side, with a note. It replaces that result, and the case and run are classified again; the latest review counts. | evaluation-service |
| **Review queue** | Results of finished evaluations a person should decide: the judge could not grade them, did not grade them, or graded a critical expectation without being calibrated for it. | evaluation-service |
| **Judge calibration** | The judge's labels on examples people labeled, per criterion: agreement, Cohen's kappa and a confusion matrix. A criterion is calibrated when the latest completed calibration meets the requirements (20 examples, 80% agreement, kappa 0.6). | evaluation-service |
| **First divergence** | The first step where the normalized baseline and candidate trajectories differ, described in plain language. | evaluation-service |
| **Failure** | A production trace with failure signals (failed outcome, policy violation, tool error, loop, cost, flag) plus structured features and embedding. | evaluation-service |
| **Failure cluster** | Group of similar failures (rules + similarity; HDBSCAN with enough data). Lifecycle CANDIDATE → CONFIRMED → PROMOTED → FIXED / DISMISSED / REOPENED. | evaluation-service |
| **Regression case** | A promoted cluster: a scenario + eval case that every future release gate of that agent replays. Never deleted when fixed. | evaluation-service |
| **Component / Dependency edge** | Graph node (AGENT, PROMPT, MODEL, TOOL, HTTP_API, SERVICE, DATABASE, POLICY, SCENARIO, …) and typed edge (USES, CALLS, READS, WRITES, CAN_MUTATE, GUARDED_BY, TESTED_BY, …). | graph-service |
| **Edge evidence** | Why an edge exists: DECLARED_MANIFEST, OPENAPI, MCP, OBSERVED_TRACE, MANUAL, INFERRED + confidence + first/last seen. | graph-service |
| **Change set** | What differs between baseline and candidate: prompt, model, params, tools, schemas, risk, policies, commit/files — each with evidence confidence. | control-plane |
| **Blast radius** | Components whose behavior may change (influence) and what they can touch (effect), with bounded paths, weights and impacted scenarios. | graph-service |
| **Release candidate** | Baseline version → candidate version (+ commit metadata) under evaluation. | control-plane |
| **Gate decision** | Immutable PASS/WARN/BLOCK with triggered rules, evidence snapshot, coverage and rule-based risk index. Re-evaluation creates a new revision. | control-plane |
| **Gate override** | Audited human decision to ship despite BLOCK; history still shows "Originally BLOCKED". | control-plane |
| **Runtime policy** | Versioned CEL rules for tool actions → allow / allow_with_limits / require_approval / deny, with a fail mode. | runtime-gateway |
| **Approval request / token** | Pending human decision for an exact action; the token is single-use, short-lived and bound to the action hash. | runtime-gateway |
| **Audit event** | Append-only, hash-chained record of governance actions (actor, action, resource, before/after hash, reason, request id). | control-plane |
| **Deterministic fake** | A labeled test/demo stand-in (scripted planner model, fake judge). Never presented as a real model result. | all |
