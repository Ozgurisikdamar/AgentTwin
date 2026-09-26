import { readFileSync } from "node:fs";
import path from "node:path";
import { afterEach, describe, expect, it } from "vitest";

import * as A from "../src/attributes.js";
import { canonicalJson, sha256Hex } from "../src/hashing.js";
import { SpanKind, StatusCode } from "../src/otlp.js";
import {
  AgentTwin,
  agentTrace,
  configure,
  currentRun,
  currentSpan,
  outcome,
  parseTraceparent,
  toolSpan,
  type AgentRun,
} from "../src/tracing.js";
import { SDK_VERSION } from "../src/version.js";
import { REPO } from "./helpers.js";
import { attrs, byName, closeClients, CONTENT_KEYS, makeClient } from "./tracing-helpers.js";

afterEach(closeClients);

function runRefund(client: AgentTwin, extra: Partial<Parameters<AgentTwin["agentRun"]>[0]> = {}): string {
  return client.agentRun(
    {
      agent: "support-refund-agent",
      version: "1.3.0",
      input: "Refund ORD-1001, my email is jane@example.com. password=hunter2hunter",
      inputContext: { tenant: "demo-co", customer_id: "CUS-100", contact: "jane@example.com" },
      sessionId: "sess-1",
      ...extra,
    },
    (run) => {
      run.modelCall(
        "anthropic",
        "claude-sonnet-5",
        {
          inputMessages: [{ role: "user", content: "contact jane@example.com" }],
          systemInstructions: "Always call get_refund_policy before refund_payment.",
          temperature: 0,
          promptHash: "abc",
        },
        (call) =>
          call.recordResponse({
            outputMessages: [{ role: "assistant", content: "ok" }],
            inputTokens: 120,
            outputTokens: 12,
            finishReasons: ["tool_use"],
            responseModel: "claude-sonnet-5",
          }),
      );
      run.retrieval("support-kb", { query: "refund policy" }, (r) =>
        r.setDocuments([{ id: "kb-1" }, { id: "kb-2" }]),
      );
      run.toolCall(
        "refund_payment",
        {
          args: { order_id: "ORD-1001", amount: 50.5, note: "api_key: sk-ant-api03-AbCdEfGhIjKlMnOpQrStUv" },
          risk: "WRITE_IRREVERSIBLE",
          idempotencyKey: "refund-ORD-1001",
          attempt: 1,
        },
        (tool) => tool.setResult({ refund_id: "R-1", status: "succeeded" }),
      );
      run.policyDecision("allow", {
        policy: "refund-limits",
        version: "3",
        rule: "amount<=100",
        tool: "refund_payment",
      });
      run.outcome("SUCCESS", {
        businessOutcome: "REFUND_COMPLETED",
        claimed: "SUCCESS",
        verified: true,
        verificationSource: "tool_result",
        stateDiff: { refunds: [0, 1] },
      });
      run.setOutput("Your refund of 50 USD was issued. Mail sent to jane@example.com");
      return run.traceId;
    },
  );
}

describe("tracing", () => {
  it("records the span contract and hierarchy; content is off by default", async () => {
    const { client, exporter } = makeClient({ environment: "production", releaseId: "rel-1" });
    const traceId = runRefund(client, {
      source: "simulation",
      simulationRunId: "run-9",
      scenarioId: "refund-happy-path",
    });
    expect(await client.flush()).toBe(true);
    const spans = byName(exporter.getFinishedSpans());
    const root = spans["invoke_agent support-refund-agent"]!;
    expect(root.traceId).toBe(traceId);
    expect(root.parentSpanId).toBeUndefined();
    expect(root.kind).toBe(SpanKind.INTERNAL);
    expect(root.status.code).toBe(StatusCode.OK);
    for (const name of [
      "chat claude-sonnet-5",
      "retrieval support-kb",
      "execute_tool refund_payment",
      "policy.decision",
      "outcome.verify",
    ]) {
      expect(spans[name]?.parentSpanId, name).toBe(root.spanId);
      expect(spans[name]?.traceId, name).toBe(traceId);
    }
    expect(Object.keys(spans)).toHaveLength(6);

    const ra = attrs(root);
    expect(ra).toMatchObject({
      "agenttwin.span.kind": "agent",
      "gen_ai.operation.name": "invoke_agent",
      "gen_ai.agent.name": "support-refund-agent",
      "gen_ai.conversation.id": "sess-1",
      "agenttwin.session.id": "sess-1",
    });
    for (const span of Object.values(spans)) {
      const a = attrs(span);
      expect(a, span.name).toMatchObject({
        "agenttwin.agent.name": "support-refund-agent",
        "agenttwin.agent.version": "1.3.0",
        "agenttwin.source": "simulation",
        "agenttwin.environment": "production",
        "agenttwin.simulation.run_id": "run-9",
        "agenttwin.scenario.id": "refund-happy-path",
        "agenttwin.release.id": "rel-1",
      });
      for (const key of CONTENT_KEYS)
        expect(a, `content ${key} leaked on ${span.name}`).not.toHaveProperty(key);
    }

    const model = spans["chat claude-sonnet-5"]!;
    expect(model.kind).toBe(SpanKind.CLIENT);
    expect(attrs(model)).toMatchObject({
      "gen_ai.provider.name": "anthropic",
      "gen_ai.request.model": "claude-sonnet-5",
      "gen_ai.request.temperature": 0,
      "gen_ai.usage.input_tokens": 120,
      "gen_ai.usage.output_tokens": 12,
      "gen_ai.response.finish_reasons": ["tool_use"],
      "gen_ai.response.model": "claude-sonnet-5",
      "agenttwin.prompt.hash": "abc",
    });

    const tool = attrs(spans["execute_tool refund_payment"]!);
    const args = { order_id: "ORD-1001", amount: 50.5, note: "api_key: sk-ant-api03-AbCdEfGhIjKlMnOpQrStUv" };
    expect(tool).toMatchObject({
      "gen_ai.tool.name": "refund_payment",
      "gen_ai.operation.name": "execute_tool",
      "agenttwin.tool.risk": "WRITE_IRREVERSIBLE",
      "agenttwin.tool.args_hash": sha256Hex(canonicalJson(args)),
      "agenttwin.tool.idempotency_key_hash": sha256Hex("refund-ORD-1001").slice(0, 16),
      "agenttwin.tool.result_status": "ok",
      "agenttwin.tool.attempt": 1,
    });
    expect(JSON.stringify(tool)).not.toContain("refund-ORD-1001");

    expect(attrs(spans["retrieval support-kb"]!)["agenttwin.retrieval.document_count"]).toBe(2);
    expect(attrs(spans["policy.decision"]!)).toMatchObject({
      "agenttwin.policy.decision": "allow",
      "agenttwin.policy.name": "refund-limits",
      "agenttwin.policy.version": "3",
      "agenttwin.policy.rule": "amount<=100",
      "gen_ai.tool.name": "refund_payment",
    });
    const out = attrs(spans["outcome.verify"]!);
    expect(out).toMatchObject({
      "agenttwin.outcome.status": "SUCCESS",
      "agenttwin.outcome.claimed": "SUCCESS",
      "agenttwin.outcome.business": "REFUND_COMPLETED",
      "agenttwin.outcome.verified": true,
      "agenttwin.outcome.verification_source": "tool_result",
    });
    expect(JSON.parse(out["agenttwin.state.diff"] as string)).toEqual({ refunds: [0, 1] });

    expect(client.resource.attributes).toMatchObject({
      "agenttwin.sdk.name": "agenttwin-typescript",
      "agenttwin.sdk.version": SDK_VERSION,
      "agenttwin.content.mode": "off",
      "agenttwin.content.redacted": false,
      "deployment.environment.name": "production",
      "service.name": "test-agent",
      "agenttwin.release.id": "rel-1",
    });
    expect(client.stats).toMatchObject({ ended: 6, exported: 6, failed: 0, dropped: 0 });
  });

  it("redacted mode masks personal data and secrets in every content field", async () => {
    const { client, exporter } = makeClient({ contentMode: "redacted" });
    runRefund(client);
    await client.flush();
    const spans = byName(exporter.getFinishedSpans());
    const root = attrs(spans["invoke_agent support-refund-agent"]!);
    expect(root["agenttwin.input"]).toBe(
      "Refund ORD-1001, my email is [REDACTED:email]. password=[REDACTED:credential]",
    );
    expect(JSON.parse(root["agenttwin.input.context"] as string)).toEqual({
      tenant: "demo-co",
      customer_id: "CUS-100",
      contact: "[REDACTED:email]",
    });
    expect(root["agenttwin.output"]).toBe("Your refund of 50 USD was issued. Mail sent to [REDACTED:email]");
    expect(JSON.stringify(exporter.getFinishedSpans().map(attrs))).not.toContain("jane@example.com");
    const tool = attrs(spans["execute_tool refund_payment"]!);
    expect(JSON.parse(tool["gen_ai.tool.call.arguments"] as string)).toEqual({
      order_id: "ORD-1001",
      amount: 50.5,
      note: "api_key: [REDACTED:api_key]",
    });
    expect(JSON.parse(tool["gen_ai.tool.call.result"] as string)).toEqual({
      refund_id: "R-1",
      status: "succeeded",
    });
    const model = attrs(spans["chat claude-sonnet-5"]!);
    expect(JSON.parse(model["gen_ai.input.messages"] as string)).toEqual([
      { role: "user", content: "contact [REDACTED:email]" },
    ]);
    expect(model["gen_ai.system_instructions"]).toBe("Always call get_refund_policy before refund_payment.");
    expect(client.resource.attributes["agenttwin.content.redacted"]).toBe(true);
  });

  it("full mode keeps personal data but never secrets", async () => {
    const { client, exporter } = makeClient({ contentMode: "full" });
    runRefund(client);
    await client.flush();
    const root = attrs(byName(exporter.getFinishedSpans())["invoke_agent support-refund-agent"]!);
    expect(root["agenttwin.input"]).toBe(
      "Refund ORD-1001, my email is jane@example.com. password=[REDACTED:credential]",
    );
    expect(JSON.parse(root["agenttwin.input.context"] as string).contact).toBe("jane@example.com");
    expect(client.resource.attributes["agenttwin.content.redacted"]).toBe(false);
  });

  it("JSON-path redaction applies to tool arguments; content is truncated to its bound", async () => {
    const { client, exporter } = makeClient({
      contentMode: "redacted",
      maxContentBytes: 64,
      redaction: { jsonPaths: ["$.customer.name"], strategy: "hash" },
    });
    await client.agentRun({ agent: "a", version: "1", input: "x".repeat(500) }, async (run) => {
      await run.toolCall(
        "lookup_customer",
        { args: { customer: { name: "Jane Roe", tier: "gold" } }, risk: "read" },
        async () => ({
          ok: true,
        }),
      );
    });
    await client.flush();
    const spans = byName(exporter.getFinishedSpans());
    const tool = attrs(spans["execute_tool lookup_customer"]!);
    const args = JSON.parse(tool["gen_ai.tool.call.arguments"] as string);
    expect(args.customer.tier).toBe("gold");
    expect(args.customer.name).toMatch(/^\[HASH:field:[0-9a-f]{12}\]$/);
    expect(tool["agenttwin.tool.risk"]).toBe("READ");
    expect(tool["gen_ai.tool.call.result"]).toBe('{"ok":true}');
    const input = attrs(spans["invoke_agent a"]!)["agenttwin.input"] as string;
    expect(Buffer.byteLength(input)).toBeLessThanOrEqual(64);
    expect(input.endsWith("…[truncated]")).toBe(true);
  });

  it("the active span follows async work: awaits, timers, promise chains, concurrent runs", async () => {
    const { client, exporter } = makeClient();
    const one = (version: string) =>
      client.agentRun({ agent: "multi", version }, async (run) => {
        await new Promise((r) => setTimeout(r, 5));
        expect(currentRun()).toBe(run);
        await run.toolCall("lookup_order", { args: { v: version }, risk: "READ" }, async (call) => {
          await new Promise((r) => setTimeout(r, 5));
          expect(currentSpan()).toBe(call);
          // A policy decision inside a tool call is a child of that call.
          run.policyDecision("allow", { tool: "lookup_order" });
          return {};
        });
        await new Promise<void>((resolve) =>
          setTimeout(() => {
            run.outcome("SUCCESS");
            resolve();
          }, 1),
        );
      });
    await Promise.all([one("1.0"), one("2.0")]);
    expect(currentRun()).toBeUndefined();
    await client.flush();
    const spans = exporter.getFinishedSpans();
    const roots = spans.filter((s) => s.name === "invoke_agent multi");
    expect(roots).toHaveLength(2);
    for (const root of roots) {
      const version = attrs(root)["agenttwin.agent.version"];
      const children = spans.filter((s) => s.traceId === root.traceId && s !== root);
      expect(children.map((s) => s.name).sort()).toEqual([
        "execute_tool lookup_order",
        "outcome.verify",
        "policy.decision",
      ]);
      const tool = children.find((s) => s.name === "execute_tool lookup_order")!;
      expect(tool.parentSpanId).toBe(root.spanId);
      expect(children.find((s) => s.name === "policy.decision")!.parentSpanId).toBe(tool.spanId);
      expect(children.find((s) => s.name === "outcome.verify")!.parentSpanId).toBe(root.spanId);
      for (const child of children) expect(attrs(child)["agenttwin.agent.version"]).toBe(version);
    }
  });

  it("tool errors and timeouts: status, error type, exception event; the error is rethrown", async () => {
    const { client, exporter } = makeClient();
    await client.agentRun({ agent: "a", version: "1" }, async (run) => {
      await expect(
        run.toolCall("refund_payment", { args: {}, risk: "WRITE_IRREVERSIBLE" }, async () => {
          await AbortSignalTimeout(1);
        }),
      ).rejects.toMatchObject({ name: "TimeoutError" });
      run.toolCall("refund_payment", { args: {}, risk: "WRITE_IRREVERSIBLE", attempt: 2 }, (t) =>
        t.setError("rate_limited", undefined, { httpStatus: 429 }),
      );
      expect(() =>
        run.toolCall("lookup", (): number => {
          throw new TypeError("bad input for jane@example.com");
        }),
      ).toThrow(TypeError);
      expect(() => run.toolCall("x", { risk: "NOT_A_RISK" as "READ" }, () => 1)).toThrow(RangeError);
    });
    await client.flush();
    const spans = exporter.getFinishedSpans();
    const [first, second] = spans.filter((s) => s.name === "execute_tool refund_payment");
    expect(attrs(first!)).toMatchObject({
      "agenttwin.tool.result_status": "timeout",
      "error.type": "TimeoutError",
    });
    expect(first!.status.code).toBe(StatusCode.ERROR);
    expect(first!.events.map((e) => e.name)).toEqual(["exception"]);
    expect(attrs(second!)).toMatchObject({
      "agenttwin.tool.result_status": "rate_limited",
      "http.response.status_code": 429,
      "error.type": "rate_limited",
    });
    expect(second!.status.code).toBe(StatusCode.ERROR);
    const lookup = byName(spans)["execute_tool lookup"]!;
    expect(attrs(lookup)).toMatchObject({
      "agenttwin.tool.result_status": "error",
      "error.type": "TypeError",
    });
    // Exception messages are redacted like the trace service redacts them.
    expect(lookup.events[0]!.attributes).toEqual({
      "exception.type": "TypeError",
      "exception.message": "bad input for [REDACTED:email]",
    });
    expect(byName(spans)["invoke_agent a"]!.status.code).toBe(StatusCode.OK);
  });

  it("a failed run is marked failed and the exception is rethrown", async () => {
    const { client, exporter } = makeClient();
    class BudgetExhausted extends Error {}
    await expect(
      client.agentRun({ agent: "a" }, async () => {
        throw new BudgetExhausted("step budget");
      }),
    ).rejects.toBeInstanceOf(BudgetExhausted);
    client.agentRun({ agent: "b" }, (run) => run.setError("MaxSteps", "gave up after 12 steps"));
    await client.flush();
    const spans = byName(exporter.getFinishedSpans());
    expect(spans["invoke_agent a"]!.status.code).toBe(StatusCode.ERROR);
    expect(attrs(spans["invoke_agent a"]!)["error.type"]).toBe("BudgetExhausted");
    expect(spans["invoke_agent b"]!.status).toEqual({
      code: StatusCode.ERROR,
      message: "gave up after 12 steps",
    });
  });

  it("outcome validation rejects what is not an outcome", () => {
    const client = new AgentTwin({ enabled: false });
    client.agentRun({ agent: "a" }, (run) => {
      expect(() => run.outcome("SUCCESS", { verified: true })).toThrow(/cannot be verified/);
      expect(() => run.outcome("DONE" as "SUCCESS")).toThrow(RangeError);
      expect(() => run.outcome("SUCCESS", { claimed: "YES" as "SUCCESS" })).toThrow(RangeError);
      expect(() => run.outcome("SUCCESS", { verificationSource: "self_report" as "unavailable" })).toThrow(
        RangeError,
      );
    });
    expect(client.stats.ended).toBe(1);
  });

  it("toolSpan and agentTrace: sync and async tools, idempotency key, errors, outcome", async () => {
    const { client, exporter } = makeClient({ contentMode: "redacted" });
    const refundPayment = toolSpan(
      {
        name: "refund_payment",
        risk: "WRITE_IRREVERSIBLE",
        argNames: ["orderId", "amount", "idempotencyKey"],
        client,
      },
      (orderId: string, amount: number, idempotencyKey?: string) => ({
        refund_id: `R-${orderId}`,
        amount,
        idempotencyKey,
      }),
    );
    const lookupOrder = toolSpan(
      { risk: "READ", client },
      async function lookupOrder(args: { order_id: string }) {
        await new Promise((r) => setTimeout(r, 1));
        return { order_id: args.order_id, email: "jane@example.com" };
      },
    );
    const broken = toolSpan({ risk: "READ", client }, function broken(_orderId: string): never {
      throw new RangeError("upstream exploded");
    });
    expect(lookupOrder.name).toBe("lookupOrder");
    expect(() => toolSpan({ risk: "ROOT" as "READ" }, () => 1)).toThrow(RangeError);

    const previous = configure({ enabled: false });
    try {
      await client.agentRun(
        { agent: "refund-agent", version: "1.3.0", project: "support" },
        async (trace: AgentRun) => {
          expect(currentRun()).toBe(trace);
          expect(refundPayment("ORD-1", 50.5, "k-1")).toMatchObject({ refund_id: "R-ORD-1" });
          await lookupOrder({ order_id: "ORD-1" });
          expect(() => broken("ORD-2")).toThrow(RangeError);
          outcome("SUCCESS", { businessOutcome: "REFUND_COMPLETED" });
        },
      );
      // agentTrace uses the default client (here disabled: nothing exported).
      expect(await agentTrace({ agent: "other" }, () => 42)).toBe(42);
    } finally {
      await previous.shutdown();
    }
    expect(currentRun()).toBeUndefined();
    outcome("SUCCESS"); // no-op outside a run
    await client.flush();
    const spans = byName(exporter.getFinishedSpans());
    const refund = attrs(spans["execute_tool refund_payment"]!);
    expect(JSON.parse(refund["gen_ai.tool.call.arguments"] as string)).toEqual({
      orderId: "ORD-1",
      amount: 50.5,
      idempotencyKey: "k-1",
    });
    expect(refund["agenttwin.tool.idempotency_key_hash"]).toBe(sha256Hex("k-1").slice(0, 16));
    const lookup = attrs(spans["execute_tool lookupOrder"]!);
    expect(JSON.parse(lookup["gen_ai.tool.call.result"] as string).email).toBe("[REDACTED:email]");
    expect(JSON.parse(lookup["gen_ai.tool.call.arguments"] as string)).toEqual({ order_id: "ORD-1" });
    expect(attrs(spans["execute_tool broken"]!)).toMatchObject({
      "agenttwin.tool.result_status": "error",
      "error.type": "RangeError",
    });
    expect(JSON.parse(attrs(spans["execute_tool broken"]!)["gen_ai.tool.call.arguments"] as string)).toEqual({
      args: ["ORD-2"],
    });
    const root = spans["invoke_agent refund-agent"]!;
    expect(attrs(root)["agenttwin.project"]).toBe("support");
    for (const name of [
      "execute_tool refund_payment",
      "execute_tool lookupOrder",
      "execute_tool broken",
      "outcome.verify",
    ]) {
      expect(spans[name]!.parentSpanId, name).toBe(root.spanId);
    }
    expect(attrs(spans["outcome.verify"]!)["agenttwin.outcome.status"]).toBe("SUCCESS");
    expect(spans["invoke_agent other"]).toBeUndefined();
  });

  it("a tool traced outside any run is a trace of its own; snake_case idempotency keys are found too", async () => {
    const { client, exporter } = makeClient();
    const tool = toolSpan({ client }, (args: { idempotency_key: string }) => args.idempotency_key.length);
    expect(tool({ idempotency_key: "abc" })).toBe(3);
    await client.flush();
    const [span] = exporter.getFinishedSpans();
    expect(span!.parentSpanId).toBeUndefined();
    expect(span!.name).toBe("execute_tool tool");
    expect(attrs(span!)["agenttwin.tool.idempotency_key_hash"]).toBe(sha256Hex("abc").slice(0, 16));
    expect(attrs(span!)).not.toHaveProperty("agenttwin.agent.name");
  });

  it("manual spans: start, end once, dispose; the traceparent names the span", async () => {
    const { client, exporter } = makeClient();
    const run = client.startAgentRun({ agent: "manual", version: "2" });
    const call = run.startToolCall("refund_payment", { args: { amount: 150 } });
    expect(call.traceparent).toBe(`00-${call.traceId}-${call.spanId}-01`);
    expect(parseTraceparent(call.traceparent)).toEqual({
      traceId: call.traceId,
      spanId: call.spanId,
      sampled: true,
    });
    call.setError("denied", "APPROVAL_REQUIRED", { httpStatus: 403 });
    call.end();
    call.end(new Error("ignored: already ended"));
    call.setAttribute("late", "ignored");
    {
      using ret = run.startRetrieval("kb");
      ret.setDocuments([]);
    }
    run.end();
    await client.flush();
    const spans = byName(exporter.getFinishedSpans());
    expect(Object.keys(spans).sort()).toEqual([
      "execute_tool refund_payment",
      "invoke_agent manual",
      "retrieval kb",
    ]);
    expect(attrs(spans["execute_tool refund_payment"]!)).toMatchObject({ "error.type": "APPROVAL_REQUIRED" });
    expect(attrs(spans["execute_tool refund_payment"]!)).not.toHaveProperty("late");
    expect(spans["retrieval kb"]!.parentSpanId).toBe(run.spanId);
    expect(client.stats.ended).toBe(3);
  });

  it("joins an upstream trace through traceparent and keeps its sampling decision", async () => {
    const { client, exporter } = makeClient({ sampleRatio: 0 });
    const upstream = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01";
    client.agentRun({ agent: "joined", traceparent: upstream }, (run) => run.toolCall("t", () => 1));
    client.agentRun({ agent: "unsampled", traceparent: upstream.replace(/-01$/, "-00") }, (run) => {
      expect(run.recording).toBe(false);
      expect(run.traceparent.endsWith("-00")).toBe(true);
    });
    client.agentRun({ agent: "bad parent", traceparent: "00-zz-yy-01" }, () => undefined);
    await client.flush();
    const spans = byName(exporter.getFinishedSpans());
    expect(Object.keys(spans).sort()).toEqual(["execute_tool t", "invoke_agent joined"]);
    expect(spans["invoke_agent joined"]!.traceId).toBe("0af7651916cd43dd8448eb211c80319c");
    expect(spans["invoke_agent joined"]!.parentSpanId).toBe("b7ad6b7169203331");
    expect(parseTraceparent("ff-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01")).toBeUndefined();
    expect(parseTraceparent(`00-${"0".repeat(32)}-b7ad6b7169203331-01`)).toBeUndefined();
  });

  it("samples by trace id: ratio 0 records nothing, 0.25 about a quarter, children follow the root", async () => {
    const none = makeClient({ sampleRatio: 0 });
    runRefund(none.client);
    await none.client.flush();
    expect(none.exporter.getFinishedSpans()).toEqual([]);
    expect(none.client.stats.ended).toBe(0);

    const some = makeClient({ sampleRatio: 0.25 });
    for (let i = 0; i < 2000; i++) some.client.agentRun({ agent: "s" }, (run) => run.toolCall("t", () => i));
    await some.client.flush();
    const spans = some.exporter.getFinishedSpans();
    const roots = spans.filter((s) => s.name === "invoke_agent s");
    expect(roots.length).toBeGreaterThan(400);
    expect(roots.length).toBeLessThan(600);
    expect(spans.length).toBe(roots.length * 2); // a sampled run keeps its children, an unsampled one has none
  });

  it("caps attributes per span and counts the dropped ones", async () => {
    const { client, exporter } = makeClient();
    client.agentRun({ agent: "wide" }, (run) => {
      for (let i = 0; i < 200; i++) run.setAttribute(`extra.${i}`, i);
      run.setAttribute("extra.0", "overwrite is not a new attribute");
    });
    await client.flush();
    const [span] = exporter.getFinishedSpans();
    expect(Object.keys(span!.attributes)).toHaveLength(128);
    expect(span!.droppedAttributesCount).toBeGreaterThan(70);
    expect(span!.attributes["extra.0"]).toBe("overwrite is not a new attribute");
  });

  it("uses the Python SDK's attribute names and package version", () => {
    const python = readFileSync(path.join(REPO, "packages/sdk-python/src/agenttwin/_attributes.py"), "utf8");
    const keys = [...python.matchAll(/^([A-Z_]+) = "([^"]+)"$/gm)].map(([, k, v]) => [k, v]);
    expect(keys.length).toBeGreaterThan(60);
    for (const [k, v] of keys) expect((A as Record<string, unknown>)[k!], k).toBe(v);
    const pkg = JSON.parse(readFileSync(path.join(REPO, "packages/sdk-typescript/package.json"), "utf8"));
    expect(SDK_VERSION).toBe(pkg.version);
  });
});

function AbortSignalTimeout(ms: number): Promise<never> {
  return new Promise((_, reject) => {
    const signal = AbortSignal.timeout(ms);
    signal.addEventListener("abort", () => reject(signal.reason));
  });
}
