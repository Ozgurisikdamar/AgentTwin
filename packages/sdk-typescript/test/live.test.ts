/**
 * The SDK against the running stack (`make test-sdk-ts-live`, after
 * `make dev`): a run recorded here goes through the collector into the trace
 * service, and the API shows it with the right structure, context, content
 * policy and outcome. Skipped unless AGENTTWIN_LIVE=1.
 */
import { randomUUID } from "node:crypto";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { parse as parseYaml } from "yaml";

import { argsHash } from "../src/content.js";
import { loadManifest } from "../src/manifest.js";
import { reportOutcome } from "../src/outcomes.js";
import { AgentTwin, toolSpan } from "../src/tracing.js";
import { REPO } from "./helpers.js";

const LIVE = process.env.AGENTTWIN_LIVE === "1";
const API = process.env.AGENTTWIN_API_URL ?? "http://127.0.0.1:8080";
const OTLP = process.env.AGENTTWIN_OTLP_ENDPOINT ?? "http://127.0.0.1:4318";
const KEY = process.env.AGENTTWIN_API_KEY ?? "";
const PROJECT = process.env.AGENTTWIN_LIVE_PROJECT ?? "support";
const AGENT = "support-refund-agent";
const VERSION = "1.2.4";

async function api<T>(pathname: string): Promise<{ status: number; body: T }> {
  const res = await fetch(`${API}${pathname}`, {
    headers: { "x-agenttwin-api-key": KEY, accept: "application/json" },
  });
  const text = await res.text();
  return { status: res.status, body: (text ? JSON.parse(text) : undefined) as T };
}

async function eventually<T>(fn: () => Promise<T | undefined>, timeoutMs = 20_000): Promise<T> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const value = await fn();
    if (value !== undefined) return value;
    if (Date.now() > deadline) throw new Error("timed out waiting for the stack");
    await new Promise((r) => setTimeout(r, 250));
  }
}

interface Span {
  span_id: string;
  parent_span_id: string | null;
  name: string;
  kind: string;
  status: string;
  tool_name: string | null;
  tool_risk: string | null;
  attributes: Record<string, unknown>;
  content: Record<string, string> | null;
}
interface TraceDetail {
  trace: Record<string, unknown>;
  spans: Span[];
  outcome: Record<string, unknown> | null;
}

describe.skipIf(!LIVE)("live: the SDK against the running stack", () => {
  it(
    "a recorded run reaches the API with its structure, context, content policy and outcome",
    { timeout: 60_000 },
    async () => {
      expect(KEY, "AGENTTWIN_API_KEY").not.toBe("");
      const projects = await api<{ items: { id: string; slug: string }[] }>("/api/v1/projects");
      const project = projects.body.items.find((p) => p.slug === PROJECT || p.id === PROJECT);
      expect(project, `project ${PROJECT}`).toBeDefined();
      const pid = project!.id;
      const manifest = await loadManifest(path.join(REPO, "demo", AGENT, "manifests", `${VERSION}.yaml`), {
        parseYaml,
      });

      const session = `sdk-ts-live-${randomUUID()}`;
      const at = new AgentTwin({
        apiKey: KEY,
        otlpEndpoint: OTLP,
        apiUrl: API,
        serviceName: "sdk-ts-live",
        environment: "sdk-ts-live",
        source: "test",
        contentMode: "redacted",
        // Content is on, so the arguments are recorded: this field only as a mask.
        redaction: { jsonPaths: ["$.idempotency_key"] },
        scheduleDelayMs: 100,
      });
      const lookupOrder = toolSpan(
        { risk: "READ", client: at },
        async function lookup_order(args: { order_id: string }) {
          return { order_id: args.order_id, status: "delivered", email: "jane@example.com" };
        },
      );
      const refundArgs = { order_id: "ORD-1001", amount: 40, idempotency_key: `refund-${session}` };

      const traceId = await at.agentRun(
        {
          agent: AGENT,
          version: VERSION,
          sessionId: session,
          input: "Refund $40 for ORD-1001, reach me at jane@example.com",
          inputContext: { tenant: "demo-co", contact: "jane@example.com" },
        },
        async (run) => {
          await run.modelCall(
            "scripted",
            "scripted-planner-v1",
            {
              inputMessages: [{ role: "user", content: "refund please" }],
              temperature: 0,
              promptHash: manifest.promptHash,
              promptVersion: VERSION,
            },
            async (call) =>
              call.recordResponse({
                inputTokens: 120,
                outputTokens: 12,
                finishReasons: ["tool_use"],
                responseModel: "scripted-planner-v1",
              }),
          );
          await lookupOrder({ order_id: "ORD-1001" });
          await run.toolCall(
            "refund_payment",
            {
              args: refundArgs,
              risk: manifest.riskOf("refund_payment") as "WRITE_IRREVERSIBLE",
              idempotencyKey: refundArgs.idempotency_key,
            },
            async (call) => call.setError("timeout", "UpstreamTimeout"),
          );
          run.policyDecision("allow", {
            policy: "refund-limits",
            rule: "under-limit",
            tool: "refund_payment",
          });
          run.outcome("PARTIAL", { claimed: "SUCCESS", businessOutcome: "REFUND_PENDING" });
          run.setOutput("Your refund is on its way. We wrote to jane@example.com.");
          return run.traceId;
        },
      );
      expect(await at.flush()).toBe(true);
      await at.shutdown();
      expect(at.stats).toMatchObject({ ended: 6, exported: 6, failed: 0, dropped: 0 });

      // Visible in the API: the whole trace, normalized by the trace service.
      const detail = await eventually(async () => {
        const r = await api<TraceDetail>(`/api/v1/traces/${traceId}?project_id=${pid}`);
        // Spans are stored first; the summary (counts, prompt hash) follows.
        const done = r.status === 200 && r.body.spans.length === 6 && r.body.trace.tool_call_count === 2;
        return done ? r.body : undefined;
      });
      expect(detail.trace).toMatchObject({
        trace_id: traceId,
        agent_name: AGENT,
        agent_version: VERSION,
        session_id: session,
        environment: "sdk-ts-live",
        source: "test",
        sdk_name: "agenttwin-typescript",
        sdk_version: "0.1.0",
        content_mode: "redacted",
        content_dropped: false,
        span_count: 6,
        model_call_count: 1,
        tool_call_count: 2,
        root_name: `invoke_agent ${AGENT}`,
        prompt_hash: manifest.promptHash,
      });
      // The SDK's prompt hash is the one the control plane registered for the version.
      const agents = await api<{ items: { id: string; name: string }[] }>(`/api/v1/projects/${pid}/agents`);
      const agentId = agents.body.items.find((a) => a.name === AGENT)!.id;
      const version = await api<{ prompt_sha256: string }>(`/api/v1/agents/${agentId}/versions/${VERSION}`);
      expect(version.body.prompt_sha256).toBe(manifest.promptHash);

      const spans = Object.fromEntries(detail.spans.map((s) => [s.name, s]));
      const root = spans[`invoke_agent ${AGENT}`]!;
      expect(root.parent_span_id).toBeNull();
      for (const s of detail.spans) if (s !== root) expect(s.parent_span_id, s.name).toBe(root.span_id);
      expect(root.content?.input).toBe("Refund $40 for ORD-1001, reach me at [REDACTED:email]");
      expect(JSON.stringify(detail)).not.toContain("jane@example.com");
      expect(JSON.stringify(detail)).not.toContain(refundArgs.idempotency_key);

      const refund = spans["execute_tool refund_payment"]!;
      expect(refund).toMatchObject({
        kind: "tool",
        status: "ERROR",
        tool_name: "refund_payment",
        tool_risk: "WRITE_IRREVERSIBLE",
      });
      expect(JSON.stringify(refund.attributes)).toContain(argsHash(refundArgs));
      expect(JSON.parse(refund.content!.tool_args!)).toEqual({
        ...refundArgs,
        idempotency_key: "[REDACTED:field]",
      });
      const lookup = spans["execute_tool lookup_order"]!;
      expect(lookup).toMatchObject({ kind: "tool", status: "OK", tool_risk: "READ" });
      expect(JSON.parse(lookup.content!.tool_result!)).toMatchObject({
        email: "[REDACTED:email]",
      });
      expect(spans["chat scripted-planner-v1"]).toMatchObject({ kind: "model", status: "OK" });

      // The run's own outcome: the agent claimed success, nothing verified it.
      expect(detail.outcome).toMatchObject({ status: "PARTIAL", claimed_status: "SUCCESS", verified: false });

      // A delayed outcome, verified outside the run, replaces it.
      const stored = await reportOutcome(traceId, "FAILURE", {
        config: at.config,
        verified: true,
        verificationSource: "external_callback",
        claimedStatus: "SUCCESS",
        actualState: { refund_count: 0 },
        notes: "payment provider reports no refund",
        idempotencyKey: `outcome-${session}`,
      });
      expect(stored).toMatchObject({ status: "FAILURE", verified: true, contradiction: true, source: "api" });
      const after = await api<TraceDetail>(`/api/v1/traces/${traceId}?project_id=${pid}`);
      expect(after.body.outcome).toMatchObject({
        status: "FAILURE",
        verification_source: "external_callback",
        contradiction: true,
      });
    },
  );
});
