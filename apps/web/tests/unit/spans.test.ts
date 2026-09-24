import { describe, expect, it } from "vitest";
import {
  barGeometry,
  buildWaterfall,
  displayName,
  labelPlacement,
  retriedToolCalls,
  visibleRows,
} from "@/lib/spans";
import { candidateSpans, span } from "./fixtures";

describe("buildWaterfall", () => {
  it("orders spans depth-first with siblings by start time", () => {
    const shuffled = [...candidateSpans()].reverse();
    const w = buildWaterfall(shuffled);
    expect(w.rows.map((r) => r.span.span_id)).toEqual(["root", "m1", "t1", "t2", "h1", "t3", "o1"]);
    expect(w.rows.map((r) => r.depth)).toEqual([0, 1, 1, 1, 2, 1, 1]);
    expect(w.totalMs).toBe(2000);
    expect(w.rows.find((r) => r.span.span_id === "h1")?.ancestors).toEqual(["root", "t2"]);
    expect(w.rows.find((r) => r.span.span_id === "t2")?.hasChildren).toBe(true);
    expect(w.orphans).toBe(0);
  });

  it("keeps spans whose parent is missing and counts them", () => {
    const w = buildWaterfall([span("a", null, "agent", 0, 100), span("b", "missing", "tool", 50, 10)]);
    expect(w.rows.map((r) => [r.span.span_id, r.depth])).toEqual([
      ["a", 0],
      ["b", 0],
    ]);
    expect(w.orphans).toBe(1);
  });

  it("shows every span exactly once even with parent cycles and duplicates", () => {
    const spans = [
      span("x", "y", "tool", 0, 10),
      span("y", "x", "tool", 5, 10),
      span("z", "z", "tool", 7, 1),
      span("x", "y", "tool", 0, 10), // duplicate delivery
    ];
    const w = buildWaterfall(spans);
    expect(w.rows.map((r) => r.span.span_id).sort()).toEqual(["x", "y", "z"]);
  });

  it("handles empty input", () => {
    expect(buildWaterfall([])).toEqual({ rows: [], startMs: 0, totalMs: 0, orphans: 0 });
  });
});

describe("visibleRows", () => {
  it("hides descendants of collapsed spans", () => {
    const w = buildWaterfall(candidateSpans());
    expect(visibleRows(w.rows, new Set(["t2"])).map((r) => r.span.span_id)).not.toContain("h1");
    expect(visibleRows(w.rows, new Set(["root"])).map((r) => r.span.span_id)).toEqual(["root"]);
    expect(visibleRows(w.rows, new Set())).toHaveLength(7);
  });
});

describe("barGeometry", () => {
  it("positions bars relative to the trace and keeps tiny spans visible", () => {
    const w = buildWaterfall(candidateSpans());
    const t3 = w.rows.find((r) => r.span.span_id === "t3")!;
    expect(barGeometry(t3, w.totalMs)).toEqual({ left: 20, width: 2.5 });
    const tiny = { ...t3, offsetMs: 1999, durationMs: 0 };
    const g = barGeometry(tiny, w.totalMs);
    expect(g.left + g.width).toBeLessThanOrEqual(100);
    expect(g.width).toBeGreaterThan(0);
    expect(barGeometry(t3, 0)).toEqual({ left: 0, width: 100 });
  });
});

describe("displayName", () => {
  it("drops the GenAI operation prefix only for tool spans", () => {
    expect(displayName({ kind: "tool", name: "execute_tool refund_payment" })).toBe("refund_payment");
    expect(displayName({ kind: "tool", name: "execute_tool " })).toBe("execute_tool ");
    expect(displayName({ kind: "tool", name: "refund_payment" })).toBe("refund_payment");
    expect(displayName({ kind: "model", name: "chat claude" })).toBe("chat claude");
    expect(displayName({ kind: "agent", name: "execute_tool x" })).toBe("execute_tool x");
  });
});

describe("labelPlacement", () => {
  it("keeps duration labels inside the timeline column", () => {
    expect(labelPlacement(0, 20)).toBe("after");
    expect(labelPlacement(60, 25)).toBe("after");
    expect(labelPlacement(80, 15)).toBe("before");
    expect(labelPlacement(99.6, 0.4)).toBe("before");
    // A root span covers the whole trace: neither side has room.
    expect(labelPlacement(0, 100)).toBe("inside");
    expect(labelPlacement(10, 88)).toBe("inside");
  });
});

describe("retriedToolCalls", () => {
  it("finds calls that repeat a failed call", () => {
    expect(retriedToolCalls(candidateSpans()).map((s) => s.span_id)).toEqual(["t3"]);
  });

  const tool = (id: string, atMs: number, name: string, args: string, result: string, attempt?: number) =>
    span(id, "root", "tool", atMs, 10, {
      name: `execute_tool ${name}`,
      status: result === "ok" ? "OK" : "ERROR",
      attributes: {
        tool_name: name,
        tool_args_hash: args,
        tool_result_status: result,
        ...(attempt === undefined ? {} : { attempt }),
      },
    });

  it("does not count a re-read after a success as a retry", () => {
    const spans = [
      tool("a", 1, "lookup_order", "h-o", "ok", 1),
      tool("b", 2, "refund_payment", "h-r", "ok", 1),
      tool("c", 3, "lookup_order", "h-o", "ok", 1),
    ];
    expect(retriedToolCalls(spans)).toEqual([]);
  });

  it("counts retries of a failure, whatever the SDK reported", () => {
    const spans = [
      tool("a", 1, "refund_payment", "h-r", "rate_limited", 1),
      tool("b", 2, "refund_payment", "h-r", "rate_limited", 2),
      // An SDK that does not number attempts: the repeated failure still counts.
      tool("c", 3, "refund_payment", "h-r", "ok"),
      // Different arguments after a failure: a new call.
      tool("d", 4, "refund_payment", "h-other", "ok", 1),
    ];
    expect(retriedToolCalls(spans).map((s) => s.span_id)).toEqual(["b", "c"]);
  });
});
