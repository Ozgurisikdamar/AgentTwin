# AgentTwin TypeScript SDK (`@agenttwin/sdk`)

Instruments a tool-using agent so AgentTwin can reconstruct each run: the agent
run, model calls, tool calls, retrievals, policy decisions and the outcome.
Spans carry the same OpenTelemetry GenAI conventions and `agenttwin.*`
attributes as the [Python SDK](../sdk-python/README.md) and are exported over
OTLP/HTTP (JSON) to the AgentTwin collector.

- Node.js 20 or later, TypeScript or JavaScript (ES modules). **No runtime
  dependencies**: `fetch`, `node:crypto` and `AsyncLocalStorage` only.
- **Content capture is off by default**: only metadata, hashes and counts
  leave the process (ADR-0008).
- Telemetry never throws into your code and never makes it wait for the
  network: ended spans go into a bounded queue and are exported in batches in
  the background ([overhead measurements](../../docs/benchmarks/sdk-overhead.md)).

## Install

The package is part of this repository's pnpm workspace (`pnpm install` at
the repository root; `pnpm --filter @agenttwin/sdk build` writes `dist/`). To
use it from another project, pack it and install the tarball:

```bash
pnpm --filter @agenttwin/sdk pack --pack-destination /tmp
npm install /tmp/agenttwin-sdk-0.1.0.tgz
```

## Configure

`configure()` reads the environment; options win. The variables are the
Python SDK's:

| Variable                                                                                      | Default                 | Meaning                                                                              |
| --------------------------------------------------------------------------------------------- | ----------------------- | ------------------------------------------------------------------------------------ |
| `AGENTTWIN_API_KEY`                                                                           | –                       | Project API key (`atk_…`, scope `traces:write`). Determines the project server-side. |
| `AGENTTWIN_OTLP_ENDPOINT`                                                                     | `http://localhost:4318` | Collector OTLP/HTTP base URL (`OTEL_EXPORTER_OTLP_ENDPOINT` also works).             |
| `AGENTTWIN_API_URL`                                                                           | –                       | Control-plane URL, needed for delayed outcomes.                                      |
| `AGENTTWIN_ENVIRONMENT`                                                                       | `development`           | Deployment environment of this process.                                              |
| `AGENTTWIN_SOURCE`                                                                            | `production`            | `production`, `simulation`, `replay`, `eval` or `test`.                              |
| `AGENTTWIN_SERVICE_NAME`, `AGENTTWIN_PROJECT`, `AGENTTWIN_RELEASE_ID`, `AGENTTWIN_COMMIT_SHA` | –                       | Resource metadata.                                                                   |
| `AGENTTWIN_CONTENT_MODE`                                                                      | `off`                   | `off`, `redacted` or `full` (the project policy is enforced again server-side).      |
| `AGENTTWIN_REDACTION_STRATEGY`                                                                | `mask`                  | `mask`, `hash` or `drop`.                                                            |
| `AGENTTWIN_REDACTION_PATTERNS`                                                                | –                       | Extra regular expressions (JavaScript syntax), one per line.                         |
| `AGENTTWIN_REDACTION_JSON_PATHS`                                                              | –                       | Comma-separated fields to redact, e.g. `$.customer.email`.                           |
| `AGENTTWIN_SAMPLE_RATIO`                                                                      | `1`                     | Head sampling for new traces.                                                        |
| `AGENTTWIN_DISABLED`                                                                          | –                       | `true` turns export off; spans are still created locally but never sent.             |

Options (`configure({ ... })`, `new AgentTwin({ ... })`) use the same names in
camelCase, plus `maxQueueSize` (2048), `maxExportBatchSize` (512),
`scheduleDelayMs` (500), `exportTimeoutMs` (5000) and `maxContentBytes` (8192).

## Instrument

Wrapper style with the process-wide client — the equivalent of the Python
SDK's `with AgentTrace(...)` and `@tool_span`:

```ts
import { agentTrace, configure, toolSpan } from "@agenttwin/sdk";

configure();

const lookupOrder = toolSpan({ risk: "READ" }, async function lookupOrder(args: { order_id: string }) {
  return { order_id: args.order_id, status: "delivered", total: 140 };
});

// The idempotency key argument is recorded as a hash only.
const refundPayment = toolSpan(
  { name: "refund_payment", risk: "WRITE_IRREVERSIBLE" },
  async (args: { order_id: string; amount: number; idempotency_key: string }) => ({
    refund_id: "RF-1",
    status: "succeeded",
  }),
);

await agentTrace(
  { agent: "support-refund-agent", version: "1.2.4", project: "support", input: "Refund $40 for ORD-1001" },
  async (run) => {
    const order = await lookupOrder({ order_id: "ORD-1001" });
    await refundPayment({ order_id: order.order_id, amount: 40, idempotency_key: "refund-ORD-1001-40.00" });
    run.outcome("SUCCESS", { businessOutcome: "REFUND_COMPLETED" });
    console.log("trace id:", run.traceId);
  },
);
```

A wrapped tool is a child of whichever agent run is active when it is called,
across `await`s, timers and promise chains (`AsyncLocalStorage`); called
outside a run it records a trace of its own. The value it returns is recorded
as the result (content permitting); an exception is recorded and rethrown —
`timeout` for a `TimeoutError`, `error` otherwise. A single plain-object
argument is recorded as the arguments; give `argNames` for positional ones:

```ts
const refund = toolSpan({ risk: "WRITE_IRREVERSIBLE", argNames: ["orderId", "amount"] }, refundImpl);
```

Explicit style (several clients, model calls, retrievals, policy decisions):

```ts
import { AgentTwin, configFromEnv } from "@agenttwin/sdk";

const at = new AgentTwin(configFromEnv({ contentMode: "redacted", environment: "production" }));

await at.agentRun({ agent: "support-refund-agent", version: "1.3.0", input: userMessage, sessionId: "sess-42" }, async (run) => {
  const response = await run.modelCall(
    "anthropic",
    "claude-sonnet-5",
    { inputMessages: messages, promptHash: manifest.promptHash, promptVersion: "1.3.0" },
    async (call) => {
      const r = await anthropic.messages.create({ ... });
      call.recordResponse({
        outputMessages: [...],
        inputTokens: r.usage.input_tokens,
        outputTokens: r.usage.output_tokens,
        finishReasons: [r.stop_reason],
      });
      return r;
    },
  );
  await run.retrieval("support-kb", { query: "refund window" }, async (r) => r.setDocuments(await search()));
  run.policyDecision("allow", { policy: "refunds", rule: "under_limit", tool: "refund_payment" });
  await run.toolCall(
    "refund_payment",
    { args, risk: "WRITE_IRREVERSIBLE", idempotencyKey: args.idempotency_key },
    () => payments.refund(args),
  );
  run.outcome("SUCCESS", { claimed: "SUCCESS", businessOutcome: "REFUND_COMPLETED" });
});
```

Every callback method has a `start…` twin for work that does not fit in one
callback; end the span yourself (or with `using`):

```ts
const run = at.startAgentRun({ agent: "support-refund-agent", version: "1.3.0" });
const call = run.startToolCall("refund_payment", { args, risk: "WRITE_IRREVERSIBLE" });
try {
  call.setResult(await payments.refund(args));
} catch (err) {
  call.end(err);
  throw err;
} finally {
  call.end();
  run.end();
}
```

### Correlation

`span.traceparent` is the W3C header naming a span: pass it to a service that
joins the trace (the runtime gateway records its policy decision under the
tool call that sent it). A run can join an upstream request's trace with
`agentRun({ ..., traceparent: req.headers.traceparent }, ...)`; the upstream
sampling decision is kept.

### Outcomes: claimed vs verified

An agent's own final answer is **not** evidence. Leave
`verificationSource` at `"unavailable"` (the default) unless an independent
check backs the status — for example a state assertion against your
database:

```ts
run.outcome("FAILURE", {
  claimed: "SUCCESS",
  verified: true,
  verificationSource: "state_assertion",
  stateDiff: { refund_count: 2 },
});
```

Outcomes known only later (a refund settles, a customer replies) are recorded
through the API with the project key:

```ts
import { reportOutcome } from "@agenttwin/sdk";

await reportOutcome(traceId, "FAILURE", {
  verified: true,
  verificationSource: "external_callback",
  claimedStatus: "SUCCESS",
  actualState: { refund_count: 2 },
  idempotencyKey: `outcome-${traceId}`, // safe to retry
});
```

A verified outcome that contradicts the agent's claim is flagged
(`contradiction`) in the trace explorer. Errors are `OutcomeReportError` with
the API's `status` and machine-readable `code` (`status` 0 when the API is not
reachable); the key never appears in them.

### Agent manifests

```ts
import { parse } from "yaml";
import { loadManifest } from "@agenttwin/sdk";

const manifest = await loadManifest("agent.yaml", { parseYaml: parse });
manifest.promptHash; // what the control plane records for this version
manifest.riskOf("refund_payment"); // "WRITE_IRREVERSIBLE"
```

JSON manifests need no parser; the SDK has no YAML dependency of its own.

## Shutdown and tests

`await client.flush()` exports what is queued (tests, before a serverless
handler returns); `await client.shutdown()` flushes and stops. Spans still
queued when the process exits on its own are flushed by an exit hook; a
process killed by a signal is not — call `shutdown()` in your signal
handler. `InMemorySpanExporter` collects spans for your own tests:

```ts
const exporter = new InMemorySpanExporter();
const at = new AgentTwin({ contentMode: "redacted" }, { exporter });
// ... run the agent ...
await at.flush();
exporter.getFinishedSpans();
```

## Guarantees and limits

- Failures inside instrumentation are swallowed; exceptions thrown by _your_
  code are recorded on the span and rethrown. Programming errors (an unknown
  risk level or outcome status) throw a `RangeError` where they are made.
- When the collector is unreachable or slow, spans beyond `maxQueueSize`
  (2048) are dropped and counted in `client.stats` (`dropped`, `failed`,
  `notExported`); the agent is never blocked. Exports are retried on 429,
  502, 503, 504 and network errors, twice, with backoff.
- Content attributes are capped at 8 KiB each (`maxContentBytes`); a span
  keeps at most 128 attributes.
- Secrets (private keys, JWTs, bearer tokens, API keys, `password=`-style
  credentials) are redacted even in `full` mode; `redacted` also masks e-mail
  addresses, phone numbers and (Luhn-valid) card numbers. Exception messages
  are redacted the same way before they are recorded.
- Canonical JSON (argument hashes) and redaction produce byte-for-byte the
  output of the Go services and the Python SDK: the tests run the shared
  fixtures and thousands of random inputs through the Go reference.

Run the SDK tests with `pnpm --filter @agenttwin/sdk test`.
