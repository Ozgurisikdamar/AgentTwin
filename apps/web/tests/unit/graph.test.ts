import { describe, expect, it } from "vitest";
import { parseKinds } from "@/components/graph/graph-explorer";
import type { GraphView } from "@/lib/api/graph";
import {
  NODE_HEIGHT,
  NODE_WIDTH,
  confidencePercent,
  distances,
  edgeEvidence,
  filterView,
  isRiskyTool,
  layoutGraph,
  manualMapping,
  mappingBody,
  refKey,
  riskOf,
} from "@/lib/graph";
import { livePromptNeighbourhood, liveRefundPayment } from "./graph-fixtures";

const view = livePromptNeighbourhood as GraphView;
const node = (kind: string, key: string) => view.nodes.find((n) => n.kind === kind && n.key === key)!;
const labels = (v: GraphView) => v.nodes.map((n) => n.label).sort();

describe("edge evidence and risk", () => {
  it("names how an edge is known: observed beats declared, uncertain is inferred", () => {
    const observed = view.edges.filter((e) => edgeEvidence(e) === "observed");
    expect(observed).toHaveLength(9);
    expect(edgeEvidence({ sources: ["MANIFEST"], certain: true })).toBe("declared");
    expect(edgeEvidence({ sources: ["OPENAPI"], certain: true })).toBe("declared");
    expect(edgeEvidence({ sources: ["INFERRED"], certain: false })).toBe("inferred");
    expect(edgeEvidence({ sources: ["INFERRED", "OBSERVED"], certain: true })).toBe("observed");
  });

  it("reads a tool's risk from its manifest, else from an import", () => {
    expect(riskOf(node("TOOL", "refund_payment"))).toBe("WRITE_IRREVERSIBLE");
    expect(riskOf({ attributes: { imported_risk: "admin" } })).toBe("ADMIN");
    expect(riskOf({ attributes: {} })).toBe("");
    expect(isRiskyTool(node("TOOL", "refund_payment"))).toBe(true);
    expect(isRiskyTool(node("TOOL", "export_customer_data"))).toBe(true);
    expect(isRiskyTool(node("TOOL", "lookup_order"))).toBe(false);
    expect(isRiskyTool(node("SERVICE", "payments-api"))).toBe(false);
    expect(refKey(node("SERVICE", "payments-api"))).toBe("SERVICE:payments-api");
  });
});

describe("filterView", () => {
  it("keeps the whole live view without filters", () => {
    const all = filterView(view, {});
    expect(all.nodes).toHaveLength(18);
    expect(all.edges).toHaveLength(46);
  });

  it("with observed evidence only, leaves the prompt alone: a prompt is never seen in traffic", () => {
    const observed = filterView(view, { evidence: "observed" });
    expect(labels(observed)).toEqual(["prompt 49482e32190e"]);
    expect(observed.edges).toEqual([]);
  });

  it("drops edges only traffic saw when declared evidence is asked", () => {
    const extra = structuredClone(view);
    extra.edges.push({
      ...extra.edges[0]!,
      id: "01a0d7dc-0000-7000-8000-00000000e0e0",
      sources: ["OBSERVED"],
    });
    expect(filterView(extra, {}).edges).toHaveLength(47);
    expect(filterView(extra, { evidence: "declared" }).edges).toHaveLength(46);
    expect(filterView(extra, { evidence: "observed" }).edges.map((e) => e.id)).toContain(
      "01a0d7dc-0000-7000-8000-00000000e0e0",
    );
  });

  it("hides tools below the risk tier and what only they reached", () => {
    const risky = filterView(view, { risk: "irreversible" });
    const removed = view.nodes.map(refKey).filter((k) => !risky.nodes.map(refKey).includes(k));
    // In the live view every other component is still reached through a risky tool:
    // orders-api, for one, is an API the admin tool export_customer_data can change.
    expect(removed.sort()).toEqual([
      "TOOL:escalate_to_human",
      "TOOL:get_refund_policy",
      "TOOL:lookup_customer",
      "TOOL:lookup_order",
      "TOOL:send_email",
    ]);
    expect(risky.edges.some((e) => e.from === node("TOOL", "lookup_order").id)).toBe(false);
    // Without the admin tool, orders-api is reached only through read-only tools: it goes too.
    const admin = node("TOOL", "export_customer_data").id;
    const noAdmin = {
      ...view,
      nodes: view.nodes.filter((n) => n.id !== admin),
      edges: view.edges.filter((e) => e.from !== admin && e.to !== admin),
    };
    const kept = filterView(noAdmin, { risk: "irreversible" }).nodes.map(refKey);
    expect(kept).not.toContain("SERVICE:orders-api");
    expect(kept).not.toContain("HTTP_API:orders-api");
    expect(kept).toContain("DATABASE:payments-db");
    const writes = filterView(view, { risk: "writes" });
    expect(
      writes.nodes
        .filter((n) => n.kind === "TOOL")
        .map((n) => n.key)
        .sort(),
    ).toEqual(["escalate_to_human", "export_customer_data", "refund_payment", "send_email"]);
    // Every edge left joins two components left.
    const ids = new Set(risky.nodes.map((n) => n.id));
    expect(risky.edges.every((e) => ids.has(e.from) && ids.has(e.to))).toBe(true);
  });

  it("shows only a blast radius, still connected to the focus", () => {
    const only = new Set([
      "AGENT_VERSION:support-refund-agent@1.3.0",
      "TOOL:refund_payment",
      "SERVICE:payments-api",
    ]);
    expect(labels(filterView(view, { only }))).toEqual([
      "payments-api",
      "prompt 49482e32190e",
      "refund_payment",
      "support-refund-agent@1.3.0",
    ]);
    // Without the agent version the tool is an island: it goes too.
    expect(labels(filterView(view, { only: new Set(["TOOL:refund_payment"]) }))).toEqual([
      "prompt 49482e32190e",
    ]);
  });
});

describe("layout", () => {
  it("puts components in columns by distance from the focus", () => {
    const dist = distances(view);
    expect(dist.get(node("PROMPT", view.focus[0]!.key).id)).toBe(0);
    expect(dist.get(node("AGENT_VERSION", "support-refund-agent@1.3.0").id)).toBe(1);
    expect(dist.get(node("TOOL", "refund_payment").id)).toBe(2);
    expect(dist.get(node("SERVICE", "payments-api").id)).toBe(3);
    expect(dist.get(node("AGENT_VERSION", "support-refund-agent@1.2.4").id)).toBe(3);
  });

  it("stacks a column by kind, then by label, centred on the tallest", () => {
    const layout = layoutGraph(view);
    const column = (c: number) =>
      layout.nodes
        .filter((p) => p.column === c)
        .sort((a, b) => a.row - b.row)
        .map((p) => p.component.label);
    expect(column(2)).toEqual([
      "escalate_to_human",
      "export_customer_data",
      "get_refund_policy",
      "lookup_customer",
      "lookup_order",
      "refund_payment",
      "send_email",
    ]);
    // Agent versions first, then HTTP APIs, services and the database.
    expect(layout.nodes.filter((p) => p.column === 3).map((p) => p.component.kind)).toEqual([
      "AGENT_VERSION",
      "AGENT_VERSION",
      "AGENT_VERSION",
      "AGENT_VERSION",
      "HTTP_API",
      "HTTP_API",
      "SERVICE",
      "SERVICE",
      "DATABASE",
    ]);
    const focus = layout.nodes.find((p) => p.column === 0)!;
    expect(focus.x).toBe(0);
    expect(layout.nodes.find((p) => p.column === 1)!.x).toBeGreaterThan(NODE_WIDTH);
    // The single component of column 0 sits halfway down the 9-tall column 3.
    expect(focus.y).toBeCloseTo((layout.height - NODE_HEIGHT) / 2);
    expect(layout.width).toBe(layout.nodes.find((p) => p.column === 3)!.x + NODE_WIDTH);
  });

  it("orders a column by where its neighbours sit, and is deterministic", () => {
    const layout = layoutGraph(view);
    const shuffled = { ...view, nodes: [...view.nodes].reverse(), edges: [...view.edges].reverse() };
    const again = layoutGraph(shuffled);
    const pos = (l: typeof layout) => Object.fromEntries(l.nodes.map((p) => [p.component.id, [p.x, p.y]]));
    expect(pos(again)).toEqual(pos(layout));
    const services = layout.nodes.filter((p) => p.component.kind === "SERVICE").map((p) => p.component.key);
    // orders-api hangs off lookup_order (row 4), payments-api off refund_payment (row 5).
    expect(services).toEqual(["orders-api", "payments-api"]);
    // Where the neighbours sit wins over the label: renamed so the label
    // would put it first, payments-api still follows orders-api.
    const renamed = structuredClone(view);
    renamed.nodes.find((n) => n.kind === "SERVICE" && n.key === "payments-api")!.label = "a-payments";
    const order = layoutGraph(renamed)
      .nodes.filter((p) => p.component.kind === "SERVICE")
      .map((p) => p.component.label);
    expect(order).toEqual(["orders-api", "a-payments"]);
  });

  it("places what the focus cannot reach after the last column", () => {
    const island = structuredClone(view);
    island.nodes.push({ ...island.nodes[1]!, id: "01a0d7dc-0000-7000-8000-00000000f00d", key: "island" });
    const dist = distances(island);
    expect(dist.get("01a0d7dc-0000-7000-8000-00000000f00d")).toBe(4);
  });
});

describe("manual mapping", () => {
  it("reads nothing from relationships no MANUAL evidence supports", () => {
    expect(manualMapping(liveRefundPayment.relations)).toEqual([]);
  });

  it("reads the outgoing, manually mapped dependencies it can express", () => {
    const manual = { source: "MANUAL" };
    const relations = [
      {
        direction: "out",
        type: "WRITES",
        component: { kind: "DATABASE", key: "payments-db" },
        evidence: [manual],
      },
      { direction: "in", type: "USES", component: { kind: "AGENT_VERSION", key: "a@1" }, evidence: [manual] },
      // A system calling the tool is not something the tool depends on.
      { direction: "in", type: "CALLS", component: { kind: "SERVICE", key: "gateway" }, evidence: [manual] },
      { direction: "out", type: "TESTED_BY", component: { kind: "SCENARIO", key: "s" }, evidence: [manual] },
      {
        direction: "out",
        type: "CALLS",
        component: { kind: "SERVICE", key: "payments-api" },
        evidence: [{ source: "MANIFEST" }],
      },
    ];
    expect(manualMapping(relations)).toEqual([
      { kind: "DATABASE", name: "payments-db", relation: "WRITES", criticality: "" },
    ]);
  });

  it("sends the rows with a name, trimmed and once, with a criticality only when one is picked", () => {
    expect(
      mappingBody([
        { kind: "SERVICE", name: " payments-api ", relation: "CALLS", criticality: "" },
        { kind: "SERVICE", name: "payments-api", relation: "CALLS", criticality: "HIGH" },
        { kind: "DATABASE", name: "payments-db", relation: "WRITES", criticality: "CRITICAL" },
        { kind: "QUEUE", name: "  ", relation: "PUBLISHES", criticality: "" },
      ]),
    ).toEqual([
      { kind: "SERVICE", name: "payments-api", relation: "CALLS" },
      { kind: "DATABASE", name: "payments-db", relation: "WRITES", criticality: "CRITICAL" },
    ]);
    expect(mappingBody([])).toEqual([]);
  });
});

describe("small readers", () => {
  it("writes a confidence as a percentage", () => {
    expect(confidencePercent(0.9)).toBe("90%");
    expect(confidencePercent(1)).toBe("100%");
    expect(confidencePercent(null)).toBe("—");
    expect(confidencePercent(Number.NaN)).toBe("—");
  });

  it("reads the kinds a link names, known ones only, once", () => {
    expect(parseKinds("tool, SERVICE,TOOL,bogus,,DATABASE")).toEqual(["TOOL", "SERVICE", "DATABASE"]);
    expect(parseKinds(null)).toEqual([]);
  });
});
