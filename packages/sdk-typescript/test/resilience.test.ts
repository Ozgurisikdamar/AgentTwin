/**
 * Telemetry must never hurt the host agent (spec §13): exporter failures,
 * unreachable collectors and stalled networks are absorbed, the application
 * path never waits for the network, and the in-memory queue is bounded.
 */
import { execFile } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { createServer, type IncomingMessage, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import { afterEach, describe, expect, it } from "vitest";

import { ExportStats, OTLPHttpExporter, type SpanExporter } from "../src/export.js";
import type { ReadableSpan } from "../src/otlp.js";
import { AgentTwin } from "../src/tracing.js";
import { SDK_VERSION } from "../src/version.js";

const PACKAGE = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const run = promisify(execFile);
const clients: AgentTwin[] = [];
const servers: Server[] = [];
afterEach(async () => {
  await Promise.all(clients.splice(0).map((c) => c.shutdown(2000)));
  await Promise.all(servers.splice(0).map((s) => new Promise((r) => s.close(r))));
});

function client(...args: ConstructorParameters<typeof AgentTwin>): AgentTwin {
  const c = new AgentTwin(...args);
  clients.push(c);
  return c;
}

function emitRuns(c: AgentTwin, runs: number, toolsPerRun: number): void {
  for (let i = 0; i < runs; i++) {
    c.agentRun({ agent: "agent", version: "1" }, (run) => {
      for (let j = 0; j < toolsPerRun; j++) {
        run.toolCall("lookup_order", { args: { order_id: `ORD-${i}-${j}` }, risk: "READ" }, (t) =>
          t.setResult({ ok: true }),
        );
      }
    });
  }
}

interface Captured {
  path: string;
  headers: IncomingMessage["headers"];
  body: string;
}

/** A local OTLP endpoint answering each request with the next status (200 when out). */
async function collector(statuses: number[] = []): Promise<{ url: string; requests: Captured[] }> {
  const requests: Captured[] = [];
  const server = createServer((req, res) => {
    const chunks: Buffer[] = [];
    req.on("data", (c: Buffer) => chunks.push(c));
    req.on("end", () => {
      requests.push({
        path: req.url ?? "",
        headers: req.headers,
        body: Buffer.concat(chunks).toString("utf8"),
      });
      res.writeHead(statuses.shift() ?? 200, { "content-type": "application/json" });
      res.end("{}");
    });
  });
  servers.push(server);
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  return { url: `http://127.0.0.1:${(server.address() as AddressInfo).port}`, requests };
}

async function closedPort(): Promise<string> {
  const server = createServer();
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address() as AddressInfo;
  await new Promise((r) => server.close(r));
  return `http://127.0.0.1:${port}`;
}

describe("resilience", () => {
  it("exporter exceptions never reach the host", async () => {
    const exploding: SpanExporter = {
      export: () => Promise.reject(new Error("collector is on fire")),
      shutdown: () => Promise.reject(new Error("and shutdown fails too")),
    };
    const throwingSync: SpanExporter = {
      export: () => {
        throw new Error("synchronously");
      },
    };
    for (const exporter of [exploding, throwingSync]) {
      const c = client({ scheduleDelayMs: 10 }, { exporter });
      emitRuns(c, 5, 3);
      expect(await c.flush()).toBe(true);
      await c.shutdown();
      expect(c.stats).toMatchObject({ ended: 20, failed: 20, exported: 0 });
    }
  });

  it("an unreachable collector does not block the agent", async () => {
    const c = client({
      otlpEndpoint: await closedPort(),
      apiKey: "atk_x",
      exportTimeoutMs: 500,
      scheduleDelayMs: 10,
    });
    const start = performance.now();
    emitRuns(c, 40, 4);
    const elapsed = performance.now() - start;
    // 200 spans; the application path never waits for the network.
    expect(elapsed).toBeLessThan(1000);
    await c.shutdown(5000);
    expect(c.stats.ended).toBe(200);
    expect(c.stats.exported).toBe(0);
    expect(c.stats.failed).toBe(200);
  });

  it("the queue is bounded when the network stalls", async () => {
    let release!: () => void;
    const released = new Promise<void>((r) => (release = r));
    let exported = 0;
    const stalled: SpanExporter = {
      async export(spans) {
        await released;
        exported += spans.length;
        return true;
      },
    };
    const c = client({ maxQueueSize: 50, maxExportBatchSize: 10, scheduleDelayMs: 5 }, { exporter: stalled });
    const start = performance.now();
    emitRuns(c, 250, 7); // 2000 spans
    expect(performance.now() - start).toBeLessThan(2000);
    release();
    await c.flush();
    await c.shutdown();
    expect(c.stats.ended).toBe(2000);
    // Everything beyond the queue (plus the batch in flight) was dropped,
    // never buffered without bound.
    expect(exported).toBeLessThanOrEqual(60);
    expect(c.stats.dropped).toBeGreaterThanOrEqual(2000 - 60);
    expect(c.stats.notExported).toBeGreaterThanOrEqual(2000 - 60);
    expect(c.stats.exported).toBe(exported);
  });

  it("exports in batches: a full batch leaves at once, a partial one after the schedule delay", async () => {
    const batches: number[] = [];
    const exporter: SpanExporter = {
      export(spans) {
        batches.push(spans.length);
        return Promise.resolve(true);
      },
    };
    const c = client({ maxExportBatchSize: 4, scheduleDelayMs: 60_000 }, { exporter });
    emitRuns(c, 2, 1); // 4 spans: one full batch, no timer needed
    expect(batches).toEqual([]); // never exported inside the application's end()
    await new Promise((r) => setTimeout(r, 20));
    expect(batches).toEqual([4]);
    emitRuns(c, 1, 0); // 1 span waits for the (long) schedule...
    await new Promise((r) => setTimeout(r, 20));
    expect(batches).toEqual([4]);
    expect(await c.flush()).toBe(true); // ...or a flush
    expect(batches).toEqual([4, 1]);
    emitRuns(c, 2, 1); // every later full batch leaves at once too
    await new Promise((r) => setTimeout(r, 20));
    expect(batches).toEqual([4, 1, 4]);

    const timed: number[] = [];
    const t = client(
      { scheduleDelayMs: 20 },
      { exporter: { export: (s) => (timed.push(s.length), Promise.resolve(true)) } },
    );
    emitRuns(t, 1, 2); // 3 spans, no flush: the schedule sends them
    await new Promise((r) => setTimeout(r, 300));
    expect(timed).toEqual([3]);
    expect(t.stats.exported).toBe(3);
  });

  it("flush gives up at its timeout while the exporter hangs", async () => {
    const c = client(
      { scheduleDelayMs: 5 },
      { exporter: { export: () => new Promise<boolean>(() => undefined) } },
    );
    emitRuns(c, 1, 1);
    const start = performance.now();
    expect(await c.flush(100)).toBe(false);
    expect(performance.now() - start).toBeLessThan(1000);
    expect(await c.flush(50)).toBe(false);
  });

  it("spans after shutdown are dropped, not queued", async () => {
    const c = client({}, { exporter: { export: () => Promise.resolve(true) } });
    await c.shutdown();
    await c.shutdown(); // idempotent
    emitRuns(c, 1, 1);
    expect(c.stats).toMatchObject({ ended: 2, dropped: 2, exported: 0 });
  });

  it("a disabled client records nothing on the network and keeps no timer", async () => {
    const c = client({ enabled: false, otlpEndpoint: await closedPort() });
    emitRuns(c, 3, 1);
    expect(await c.flush()).toBe(true);
    expect(c.stats).toMatchObject({ ended: 6, exported: 0, dropped: 0 });
  });

  it("the wire format is OTLP/HTTP JSON with the API key, as the collector expects", async () => {
    const { url, requests } = await collector();
    const c = client({
      otlpEndpoint: `${url}/`,
      apiKey: "atk_demo0000_secret-value-123",
      scheduleDelayMs: 10,
      serviceName: "wire-agent",
      project: "support",
    });
    c.agentRun(
      { agent: "agent", version: "1", attributes: { "gen_ai.request.temperature": undefined } },
      (run) => {
        run.modelCall("anthropic", "m", { temperature: 0, maxTokens: 64 }, () => undefined);
        run.toolCall("lookup_order", { args: { order_id: "ORD-1" } }, () => ({}));
        try {
          run.toolCall("refund_payment", () => {
            throw new Error("boom");
          });
        } catch {}
      },
    );
    await c.shutdown();
    expect(requests).toHaveLength(1);
    const req = requests[0]!;
    expect(req.path).toBe("/v1/traces");
    expect(req.headers["x-agenttwin-api-key"]).toBe("atk_demo0000_secret-value-123");
    expect(req.headers["content-type"]).toBe("application/json");
    const body = JSON.parse(req.body);
    const [rs] = body.resourceSpans;
    const resource = Object.fromEntries(
      rs.resource.attributes.map((a: { key: string; value: unknown }) => [a.key, a.value]),
    );
    expect(resource).toMatchObject({
      "service.name": { stringValue: "wire-agent" },
      "agenttwin.sdk.name": { stringValue: "agenttwin-typescript" },
      "agenttwin.sdk.version": { stringValue: SDK_VERSION },
      "agenttwin.content.redacted": { boolValue: false },
      "agenttwin.project": { stringValue: "support" },
    });
    expect(rs.scopeSpans[0].scope).toEqual({ name: "agenttwin", version: SDK_VERSION });
    const spans = rs.scopeSpans[0].spans;
    expect(spans.map((s: { name: string }) => s.name).sort()).toEqual([
      "chat m",
      "execute_tool lookup_order",
      "execute_tool refund_payment",
      "invoke_agent agent",
    ]);
    for (const s of spans) {
      expect(s.traceId).toMatch(/^[0-9a-f]{32}$/);
      expect(s.spanId).toMatch(/^[0-9a-f]{16}$/);
      expect(s.startTimeUnixNano).toMatch(/^\d{19}$/);
      expect(BigInt(s.endTimeUnixNano)).toBeGreaterThanOrEqual(BigInt(s.startTimeUnixNano));
      expect(Math.abs(Number(BigInt(s.startTimeUnixNano) / 1_000_000n) - Date.now())).toBeLessThan(60_000);
    }
    const byName = Object.fromEntries(spans.map((s: { name: string }) => [s.name, s]));
    const chat = byName["chat m"];
    expect(chat.kind).toBe(3);
    const attr = (s: { attributes: { key: string; value: unknown }[] }, key: string) =>
      s.attributes.find((a) => a.key === key)?.value;
    expect(attr(chat, "gen_ai.request.temperature")).toEqual({ doubleValue: 0 });
    expect(attr(chat, "gen_ai.request.max_tokens")).toEqual({ intValue: "64" });
    const root = byName["invoke_agent agent"];
    expect(root.parentSpanId).toBeUndefined();
    expect(chat.parentSpanId).toBe(root.spanId);
    expect(root.status).toEqual({ code: 1 });
    const failed = byName["execute_tool refund_payment"];
    expect(failed.status).toEqual({ code: 2 });
    expect(failed.events[0].name).toBe("exception");
    expect(failed.events[0].timeUnixNano).toMatch(/^\d{19}$/);
  });

  it("retries a retryable failure, not a rejected request", async () => {
    const retrying = await collector([503, 429]);
    const stats = new ExportStats();
    const make = (url: string) =>
      new OTLPHttpExporter({
        endpoint: url,
        timeoutMs: 1000,
        retryDelaysMs: [5, 5],
        resource: { attributes: {} },
        scope: { name: "t", version: "0" },
      });
    const span: ReadableSpan = {
      traceId: "0af7651916cd43dd8448eb211c80319c",
      spanId: "b7ad6b7169203331",
      parentSpanId: undefined,
      name: "s",
      kind: 1,
      startTimeUnixNano: 1n,
      endTimeUnixNano: 2n,
      attributes: {},
      droppedAttributesCount: 0,
      events: [],
      status: { code: 1 },
    };
    expect(await make(retrying.url).export([span])).toBe(true);
    expect(retrying.requests).toHaveLength(3);
    const rejecting = await collector([400]);
    expect(await make(rejecting.url).export([span])).toBe(false);
    expect(rejecting.requests).toHaveLength(1);
    const down = await collector([503, 503, 503, 503]);
    expect(await make(down.url).export([span])).toBe(false);
    expect(down.requests).toHaveLength(3);
    expect(await make(down.url).export([])).toBe(true);
    expect(stats.notExported).toBe(0);
  });

  it("spans still queued when the process exits are flushed", { timeout: 30_000 }, async () => {
    const { url, requests } = await collector();
    // The package as it is published: compiled to JavaScript.
    const out = mkdtempSync(path.join(tmpdir(), "agenttwin-sdk-"));
    const tsc = path.join(PACKAGE, "node_modules", "typescript", "bin", "tsc");
    await run(process.execPath, [tsc, "-p", path.join(PACKAGE, "tsconfig.build.json"), "--outDir", out]);
    await run(process.execPath, [path.join(PACKAGE, "test", "fixtures", "exit-flush.mjs")], {
      env: {
        ...process.env,
        SDK_ENTRY: path.join(out, "index.js"),
        AGENTTWIN_OTLP_ENDPOINT: url,
        AGENTTWIN_API_KEY: "atk_exit",
      },
    });
    rmSync(out, { recursive: true, force: true });
    const names = requests.flatMap((r) =>
      JSON.parse(r.body).resourceSpans[0].scopeSpans[0].spans.map((s: { name: string }) => s.name),
    );
    expect(names.sort()).toEqual(["execute_tool t", "invoke_agent exiting"]);
  });
});
