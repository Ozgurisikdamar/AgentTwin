/**
 * Instrumentation overhead (spec §13: measure it, do not claim zero). The
 * collector is a real local HTTP server that takes 50 ms per request; the
 * numbers show what an instrumented step costs the application and that it
 * never waits for the network. Results are printed and recorded in
 * docs/benchmarks/sdk-overhead.md.
 */
import { writeFileSync } from "node:fs";
import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { encodeRequest, type ReadableSpan } from "../src/otlp.js";
import { AgentTwin } from "../src/tracing.js";

const N = 2000;
let server: Server;
let endpoint: string;
let requests = 0;

beforeAll(async () => {
  server = createServer((req, res) => {
    req.resume();
    req.on("end", () => {
      requests++;
      setTimeout(() => res.writeHead(200).end("{}"), 50); // a slow collector
    });
  });
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
  endpoint = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
});
afterAll(() => new Promise((r) => server.close(r)));

function bareTool(orderId: string): Record<string, string> {
  return { order_id: orderId, status: "delivered" };
}

interface Summary {
  p50_us: number;
  p99_us: number;
  mean_us: number;
}

function summarize(samples: number[]): Summary {
  const sorted = [...samples].sort((a, b) => a - b);
  const q = (p: number) => sorted[Math.min(sorted.length - 1, Math.floor((p / 100) * sorted.length))]!;
  const round = (x: number) => Math.round(x * 100) / 100;
  return {
    p50_us: round(q(50)),
    p99_us: round(q(99)),
    mean_us: round(sorted.reduce((a, b) => a + b, 0) / sorted.length),
  };
}

const us = (t0: bigint) => Number(process.hrtime.bigint() - t0) / 1000;

function measureSync(client: AgentTwin | undefined): number[] {
  const samples: number[] = [];
  if (client === undefined) {
    for (let i = 0; i < N; i++) {
      const t0 = process.hrtime.bigint();
      bareTool(`ORD-${i}`);
      samples.push(us(t0));
    }
    return samples;
  }
  client.agentRun({ agent: "bench-agent", version: "1" }, (run) => {
    for (let i = 0; i < N; i++) {
      const t0 = process.hrtime.bigint();
      run.toolCall("lookup_order", { args: { order_id: `ORD-${i}` }, risk: "READ" }, (call) =>
        call.setResult(bareTool(`ORD-${i}`)),
      );
      samples.push(us(t0));
    }
  });
  return samples;
}

/** An async agent: every step awaits, so the exports run in between (the
 * same thread), and their cost shows up in the steps' latency if at all. */
async function measureAsync(client: AgentTwin | undefined): Promise<number[]> {
  const samples: number[] = [];
  const step = async (i: number) => bareTool(`ORD-${i}`);
  if (client === undefined) {
    for (let i = 0; i < N; i++) {
      const t0 = process.hrtime.bigint();
      await step(i);
      samples.push(us(t0));
    }
    return samples;
  }
  await client.agentRun({ agent: "bench-agent", version: "1" }, async (run) => {
    for (let i = 0; i < N; i++) {
      const t0 = process.hrtime.bigint();
      await run.toolCall("lookup_order", { args: { order_id: `ORD-${i}` }, risk: "READ" }, () => step(i));
      samples.push(us(t0));
      if (i % 100 === 0) await new Promise((r) => setImmediate(r)); // let the loop breathe, as I/O would
    }
  });
  return samples;
}

describe("instrumentation overhead", () => {
  it("is small and never waits for the network", { timeout: 120_000 }, async () => {
    const results: Record<string, Summary> = {
      "baseline (uninstrumented call)": summarize(measureSync(undefined)),
      "baseline, awaited": summarize(await measureAsync(undefined)),
    };
    const shed: Record<string, { ended: number; exported: number; dropped: number }> = {};
    for (const mode of ["off", "redacted"] as const) {
      const client = new AgentTwin({ contentMode: mode, maxQueueSize: 4096, otlpEndpoint: endpoint });
      measureSync(client); // warm up
      results[`tool span, content=${mode}`] = summarize(measureSync(client));
      await measureAsync(client); // warm up
      results[`tool span awaited, content=${mode}`] = summarize(await measureAsync(client));
      await client.shutdown();
      // Far faster than one 50 ms request at a time can carry: the bounded
      // queue sheds the excess and counts it; nothing is lost silently.
      expect(client.stats.failed).toBe(0);
      expect(client.stats.exported + client.stats.dropped).toBe(client.stats.ended);
      shed[mode] = {
        ended: client.stats.ended,
        exported: client.stats.exported,
        dropped: client.stats.dropped,
      };
    }
    // What the export itself costs the thread: encoding one full batch.
    const span: ReadableSpan = {
      traceId: "0af7651916cd43dd8448eb211c80319c",
      spanId: "b7ad6b7169203331",
      parentSpanId: "b7ad6b7169203332",
      name: "execute_tool lookup_order",
      kind: 1,
      startTimeUnixNano: 1_700_000_000_000_000_000n,
      endTimeUnixNano: 1_700_000_000_000_500_000n,
      attributes: Object.fromEntries(
        Array.from({ length: 14 }, (_, i) => [`agenttwin.attr.${i}`, `value-${i}`]),
      ),
      droppedAttributesCount: 0,
      events: [],
      status: { code: 1 },
    };
    const batch = Array.from({ length: 512 }, () => span);
    const encode: number[] = [];
    for (let i = 0; i < 50; i++) {
      const t0 = process.hrtime.bigint();
      encodeRequest({ attributes: {} }, { name: "agenttwin", version: "0" }, batch);
      encode.push(us(t0));
    }
    results["encode a batch of 512 spans"] = summarize(encode);
    const report = JSON.stringify({ ...results, spans: shed, requests }, null, 1);
    if (process.env.AGENTTWIN_BENCH_OUT) writeFileSync(process.env.AGENTTWIN_BENCH_OUT, report);
    else console.log(report);

    // Generous bounds so shared CI machines do not flake; typical values are
    // an order of magnitude lower (see docs/benchmarks/sdk-overhead.md).
    expect(results["tool span, content=off"]!.p50_us).toBeLessThan(500);
    expect(results["tool span, content=off"]!.p99_us).toBeLessThan(5000);
    expect(results["tool span, content=redacted"]!.p50_us).toBeLessThan(1500);
    // A step never waits for the 50 ms collector.
    expect(results["tool span awaited, content=off"]!.p99_us).toBeLessThan(20_000);
    expect(requests).toBeGreaterThan(0);
  });
});
