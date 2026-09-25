# ADR-0033 — The runtime gateway decides each tool action from versioned policies and binds approvals to the exact action

* Status: accepted · Date: 2026-09-25

## Context
Simulation, evaluation, the release gate and the regression miner stop a bad
agent version before it ships. Spec §30–31 adds the last line: an opt-in path
through which an agent's tool actions pass at runtime, so that an action the
organization never wants taken automatically (a refund above a limit, a second
refund in one conversation, a refund of an order that does not exist) is
denied or waits for a person, whatever the agent's prompt says. Golden path
steps 13–14 (§137) are its acceptance: an over-limit refund asks for approval,
the approved action succeeds once, and a modified request cannot reuse the
approval.

What exists already:

* **ADR-0005** chose CEL (`cel-go`), evaluated in-process: a policy version is
  an ordered list of rules `{name, when, effect, message}` plus a fail mode,
  compiled, type-checked and tested before it can be activated.
* **RBAC** has the permissions (`policy.write`, `policy.activate`,
  `policy.test`, `approval.decide`) and the API-key scope `runtime:invoke`
  (the demo key holds it). The control plane's proxy already routes
  `/api/v1/policies`, `/approvals`, `/policy-decisions` and `/tool-endpoints`
  to a `runtime-gateway`.
* **Events.** `policy.activated.v1` is consumed by the graph service (a
  `GUARDED_BY` edge from the tool to the policy). `policy.violation_detected.v1`
  is bound to the evaluation service's queue. `audit.recorded.v1` feeds the
  control plane's audit log.
* **Tracing.** The SDK records a policy decision as a `policy.decision` span.
  The trace service turns a `deny` into the violation `policy_denied:<tool>`,
  which the miner groups as `POLICY_VIOLATION`, and `require_approval` into
  the signal `approval_required`, which is not a failure.
* **Egress.** `gokit/netguard` builds outbound clients with host allowlists,
  private-range blocking, dial-time IP checks (DNS rebinding), redirect
  re-validation, timeouts and size caps.
* **The demo agent** calls every tool through one client whose base URL is the
  tools API in production and a tool twin in simulations; the manifests
  already declare `refund_payment`'s approval rule (`args.amount > 100`).

## Decision
* **A Go service, `runtime-gateway`, owns the `runtime` schema** (policies,
  policy versions, tool endpoints, decisions, approval requests,
  idempotency records, per-trace tool counters, its outbox). It sits in the
  agent's hot path, so it is Go, like the other latency-sensitive services.
  Callers reach it through the control plane, which authenticates them and
  forwards with an internal token (ADR-0009): `/gateway/v1` is added to the
  proxy's ownership table. A request costs one more hop; the gateway verifying
  API keys itself is the upgrade when that hop matters.

* **The invoke contract is the tool contract.** `POST /gateway/v1/tools/{tool}`
  takes the tool's arguments as its JSON body, exactly what the tool itself
  takes, so an agent opts in by changing its tools base URL. The context
  travels in headers: `X-AgentTwin-Project` (optional when the key has one
  project), `X-AgentTwin-Agent`, `X-AgentTwin-Agent-Version`,
  `X-AgentTwin-Environment` (default `production`), the trace (`traceparent`,
  or `X-AgentTwin-Trace-Id`), `Idempotency-Key` (or an `idempotency_key`
  argument) and `X-AgentTwin-Approval-Token`. The answer is the tool's own
  status and body with the decision in headers (`X-AgentTwin-Decision`,
  `-Decision-Id`, `-Policy`, `-Policy-Version`, `-Policy-Rule`), or an error
  body in the platform's shape: `403 POLICY_DENIED`, `403 APPROVAL_REQUIRED`
  (with the approval's id and expiry), `403 APPROVAL_MISMATCH`,
  `403 APPROVAL_USED`, `403 APPROVAL_EXPIRED`, `404 TOOL_NOT_REGISTERED`,
  `400 IDEMPOTENCY_KEY_REQUIRED`, `409 IDEMPOTENCY_KEY_REUSED`,
  `409 IDEMPOTENCY_IN_PROGRESS`, `502/504` when the tool fails or times out.

* **A tool is callable only once registered.** A tool endpoint names the tool,
  its URL, its kind (`http`: POST the arguments; `mcp`: a JSON-RPC
  `tools/call`), its risk tier, a timeout, whether an idempotency key is
  required and which request headers are forwarded. Registering one needs
  `policy.write` and is refused unless the URL's host is in the egress
  allowlist (`RUNTIME_EGRESS_ALLOWED_HOSTS`; private hosts such as compose
  service names only through `RUNTIME_EGRESS_PRIVATE_HOSTS`). Every call goes
  through `netguard`, which checks the address again when it dials, so a DNS
  answer that changes to a private or metadata address is still refused.
  Irreversible tools require an idempotency key by default.

* **The action and its hash.** The action is the organization, project,
  agent, tool and arguments. Its hash is the SHA-256 of their canonical JSON.
  Approvals, idempotency records and decisions are keyed by it; the arguments
  themselves are stored only redacted (`gokit/redact`), for people to read.

* **Policies.** A policy is a named, versioned document
  (`apiVersion: agenttwin.dev/v1`, `kind: Policy`) that targets one tool:
  ordered rules, a default effect (`allow`), a fail mode, optional limits for
  `allow_with_limits` (a lower timeout, a smaller response cap), the approval
  expiry, and its own tests. Versions are immutable; one version of a policy
  is active at a time; several policies may guard one tool.
  - **Activation** (`policy.activate`) compiles every rule, type-checks it
    against the declared variables (`tool`, `args`, `risk`, `agent`,
    `agent_version`, `environment`, `subject`, `trace.id`, `trace.calls`) and
    runs the version's tests, which must all pass. `fail_open` is refused
    unless the tool is registered as `READ`. Activation publishes
    `policy.activated.v1` through the outbox, with an audit entry.
  - **Testing** (`policy.test`) runs a version, or a draft not yet saved,
    against its tests and extra cases, and around every numeric threshold
    the rules compare against: `args.amount > 100` is evaluated at 99, 100
    and 101 (for decimals, one hundredth either side). The same evaluator
    serves tests, activation and live traffic.
  - **Decision.** Every active policy of the tool is evaluated with a cost
    limit. The most restrictive effect of the rules that match wins
    (`deny` > `require_approval` > `allow_with_limits` > `allow`); among
    rules with that effect, the first in order is the one reported. Every
    matched rule is recorded. No match gives the policy's default.
  - **Failure** (a rule errs at runtime, exceeds its cost, or reads a missing
    argument) applies the policy's fail mode: `fail_closed` (deny),
    `require_approval`, or `fail_open`, which applies only to a `READ` tool —
    anywhere else it fails closed. A rule that matched `deny` still denies
    when another rule erred. If the gateway cannot read its policies or
    record its decision, nothing is forwarded (`503`).
  - **Counters.** `trace.calls` is the number of times the tool was forwarded
    in the same trace before this call, so "at most one refund per
    conversation" is the rule `trace.calls >= 1 → deny`.

* **Approvals.** `require_approval` creates an approval request: the tool,
  the redacted arguments, the action hash, the rule and its message, the
  risk tier, the agent and version, the trace, the requester and an expiry
  (the policy's, default 15 minutes). The same action asked again while its
  request is pending gets the same request. A person with `approval.decide`
  approves or denies it, with a reason, and sees what the agent is trying to
  do and why it needs approval (spec §41.9).
  - **The token.** Once approved, the requester (the same API key) claims a
    token: 32 random bytes, stored only as a hash, scoped to the
    organization, project, tool and approval, valid for at most 10 minutes
    and never past the approval's expiry. Claiming again mints a new token
    and voids the previous one.
  - **Use.** A call with the token is checked before anything is forwarded:
    same project and tool, not expired, not used, and the same action hash.
    A different action is refused with `APPROVAL_MISMATCH`, recorded with its
    redacted arguments so the approval shows what was attempted, and does not
    use the token. The matching action marks the approval `USED` in the same
    transaction that records the decision, so it succeeds once. Policies are
    evaluated again: an approval satisfies `require_approval`, never `deny`.
  - A pending request past its expiry is `EXPIRED`. Every approval action is
    audited.

* **Idempotency.** A key is scoped to the project and tool. Before the policy
  runs, a key already completed for the same action returns the stored
  response (`Idempotent-Replayed: true`) without calling the tool again; the
  same key for a different action is `409 IDEMPOTENCY_KEY_REUSED`; one still
  in flight is `409 IDEMPOTENCY_IN_PROGRESS`. A timeout leaves the outcome
  unknown: the next call with the key is forwarded again, with the key, for
  the tool to deduplicate. The key is forwarded to the tool as
  `Idempotency-Key`.

* **Record and signals.** Every request that reaches a decision is recorded
  (append-only, enforced by a trigger): the action hash, effect, deciding and
  matched rules, policy versions, whether the fail mode applied, the approval,
  the tool's status and the latency. `deny` and `require_approval` publish
  `policy.violation_detected.v1`; the miner turns a runtime denial on a trace
  into a regression signal even when the agent did not record the decision
  itself.

* **Tenancy.** Every row carries the organization and project; every read and
  write is scoped by the internal token's projects. An API key reaches only
  its own projects' tools, approvals and tokens.

## Consequences
* Containment is opt-in and needs no agent code beyond a base URL and a few
  headers; an agent that reads the decision headers can record them on its
  trace, and the demo agent does.
* Over-limit, repeated and malformed actions are stopped by rules a person
  can read, test at their thresholds and audit, before they reach the tool.
* A person approves one exact action; a changed amount or order needs a new
  approval. Tokens never pass through the person.
* The gateway adds a hop and a database transaction to each forwarded call.
  It is not a general egress proxy: no browser automation, shell or arbitrary
  protocol (spec §7.6).
* Tool endpoints carry no stored credentials in V1: the tool trusts the
  network path (compose, a private network). Credential injection from a
  secret store is the upgrade.

## Upgrade triggers
* The control-plane hop dominates latency: the gateway verifies API keys
  itself (same peppered hashes, a short cache).
* Policies must be shared with enforcement points outside AgentTwin: OPA or a
  policy service (ADR-0005).
* Tools need credentials: a secret-store reference per endpoint.
