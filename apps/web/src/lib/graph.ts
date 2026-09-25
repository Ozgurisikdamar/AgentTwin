/**
 * Pure helpers of the dependency graph page: which components and edges to
 * show (evidence, risk tier, blast radius), and where to draw them. A view
 * is the graph service's bounded neighbourhood of a focus (ADR: no whole-
 * graph loads), laid out in columns by distance from the focus.
 */
import type { EvidenceSource, GraphComponent, GraphEdge, GraphView } from "./api/graph";

/** How kinds are stacked inside a column: the agent side first, tests last. */
export const KIND_ORDER = [
  "AGENT",
  "AGENT_VERSION",
  "PROMPT",
  "MODEL",
  "RETRIEVAL_SOURCE",
  "TOOL",
  "MCP_SERVER",
  "HTTP_API",
  "SERVICE",
  "DATABASE",
  "QUEUE",
  "EXTERNAL_SYSTEM",
  "POLICY",
  "EVALUATOR",
  "DATASET",
  "SCENARIO",
] as const;

function kindRank(kind: string): number {
  const i = (KIND_ORDER as readonly string[]).indexOf(kind);
  return i < 0 ? KIND_ORDER.length : i;
}

/** Facts: an edge any of whose evidence is declared by an author or a document. */
const DECLARED: readonly EvidenceSource[] = ["MANIFEST", "OPENAPI", "MCP", "MANUAL", "SCENARIO"];

/** How an edge is known, for its style: seen in traffic, declared, or only inferred. */
export type EdgeEvidence = "observed" | "declared" | "inferred";

export function edgeEvidence(edge: Pick<GraphEdge, "sources" | "certain">): EdgeEvidence {
  if (edge.sources.includes("OBSERVED")) return "observed";
  if (!edge.certain) return "inferred";
  return "declared";
}

export type EvidenceFilter = "all" | "observed" | "declared";

function edgeMatches(edge: GraphEdge, evidence: EvidenceFilter): boolean {
  if (evidence === "observed") return edge.sources.includes("OBSERVED");
  if (evidence === "declared") return edge.sources.some((s) => DECLARED.includes(s));
  return true;
}

/** Tool risk tiers, least to most dangerous (ADR: risk levels of the manifest). */
export const RISK_TIERS = ["READ", "WRITE_REVERSIBLE", "WRITE_IRREVERSIBLE", "ADMIN"] as const;
export type RiskFilter = "all" | "writes" | "irreversible";

/** A component's risk level: what the manifest says, else what an import says. */
export function riskOf(c: Pick<GraphComponent, "attributes">): string {
  const a = c.attributes as Record<string, unknown>;
  const risk =
    typeof a.risk === "string" ? a.risk : typeof a.imported_risk === "string" ? a.imported_risk : "";
  return risk.toUpperCase();
}

function riskAtLeast(risk: string, filter: RiskFilter): boolean {
  const i = (RISK_TIERS as readonly string[]).indexOf(risk);
  if (filter === "writes") return i >= 1;
  if (filter === "irreversible") return i >= 2;
  return true;
}

/** Whether a component is a tool that can change something irreversibly. */
export function isRiskyTool(c: Pick<GraphComponent, "kind" | "attributes">): boolean {
  return c.kind === "TOOL" && riskAtLeast(riskOf(c), "irreversible");
}

/** `KIND:key`, the identity a component has across services. */
export function refKey(c: { kind: string; key: string }): string {
  return `${c.kind}:${c.key}`;
}

export interface ViewFilter {
  evidence?: EvidenceFilter;
  risk?: RiskFilter;
  /** Only these components (by `refKey`), and the focus. */
  only?: ReadonlySet<string> | null;
}

/**
 * The view with the filters applied: edges of other evidence and tools below
 * the risk tier are left out, then everything no longer connected to the
 * focus (a filter must not leave islands the page cannot explain).
 */
export function filterView(view: GraphView, filter: ViewFilter): GraphView {
  const evidence = filter.evidence ?? "all";
  const risk = filter.risk ?? "all";
  const focus = new Set(view.focus.map((c) => c.id));
  const keep = new Set(
    view.nodes
      .filter((n) => {
        if (focus.has(n.id)) return true;
        if (filter.only && !filter.only.has(refKey(n))) return false;
        return n.kind !== "TOOL" || riskAtLeast(riskOf(n), risk);
      })
      .map((n) => n.id),
  );
  const edges = view.edges.filter((e) => keep.has(e.from) && keep.has(e.to) && edgeMatches(e, evidence));
  const adjacent = new Map<string, string[]>();
  for (const e of edges) {
    adjacent.set(e.from, [...(adjacent.get(e.from) ?? []), e.to]);
    adjacent.set(e.to, [...(adjacent.get(e.to) ?? []), e.from]);
  }
  const reached = new Set(focus);
  const queue = [...focus];
  while (queue.length) {
    const id = queue.shift()!;
    for (const next of adjacent.get(id) ?? []) {
      if (!reached.has(next)) {
        reached.add(next);
        queue.push(next);
      }
    }
  }
  return {
    ...view,
    nodes: view.nodes.filter((n) => reached.has(n.id)),
    edges: edges.filter((e) => reached.has(e.from) && reached.has(e.to)),
  };
}

/** Hops from the focus, either direction (unreachable components after the last). */
export function distances(view: GraphView): Map<string, number> {
  const adjacent = new Map<string, string[]>();
  for (const e of view.edges) {
    adjacent.set(e.from, [...(adjacent.get(e.from) ?? []), e.to]);
    adjacent.set(e.to, [...(adjacent.get(e.to) ?? []), e.from]);
  }
  const dist = new Map<string, number>();
  const queue: string[] = [];
  for (const f of view.focus) {
    if (!dist.has(f.id)) {
      dist.set(f.id, 0);
      queue.push(f.id);
    }
  }
  while (queue.length) {
    const id = queue.shift()!;
    for (const next of adjacent.get(id) ?? []) {
      if (!dist.has(next)) {
        dist.set(next, dist.get(id)! + 1);
        queue.push(next);
      }
    }
  }
  const last = Math.max(0, ...dist.values());
  for (const n of view.nodes) if (!dist.has(n.id)) dist.set(n.id, last + 1);
  return dist;
}

export const NODE_WIDTH = 220;
export const NODE_HEIGHT = 44;
const GAP_X = 90;
const GAP_Y = 14;

export interface PlacedNode {
  component: GraphComponent;
  column: number;
  row: number;
  x: number;
  y: number;
}

export interface Layout {
  nodes: PlacedNode[];
  width: number;
  height: number;
}

/**
 * Columns by distance from the focus; inside a column, grouped by kind and
 * ordered by where their neighbours in the previous column sit (fewer
 * crossings), then by label. Deterministic: the same view, the same picture.
 */
export function layoutGraph(view: GraphView): Layout {
  const dist = distances(view);
  const columns = new Map<number, GraphComponent[]>();
  for (const n of view.nodes) {
    const c = dist.get(n.id) ?? 0;
    columns.set(c, [...(columns.get(c) ?? []), n]);
  }
  const neighbours = new Map<string, string[]>();
  for (const e of view.edges) {
    neighbours.set(e.from, [...(neighbours.get(e.from) ?? []), e.to]);
    neighbours.set(e.to, [...(neighbours.get(e.to) ?? []), e.from]);
  }
  const rowOf = new Map<string, number>();
  const order = [...columns.keys()].sort((a, b) => a - b);
  const tallest = Math.max(1, ...[...columns.values()].map((c) => c.length));
  const placed: PlacedNode[] = [];
  for (const column of order) {
    const members = columns.get(column)!;
    const centre = (id: string) => {
      const rows = (neighbours.get(id) ?? [])
        .filter((n) => dist.get(n) === column - 1 && rowOf.has(n))
        .map((n) => rowOf.get(n)!);
      return rows.length ? rows.reduce((a, b) => a + b, 0) / rows.length : Number.POSITIVE_INFINITY;
    };
    const sorted = [...members].sort(
      (a, b) =>
        kindRank(a.kind) - kindRank(b.kind) ||
        centre(a.id) - centre(b.id) ||
        a.label.localeCompare(b.label) ||
        a.id.localeCompare(b.id),
    );
    const offset = ((tallest - sorted.length) * (NODE_HEIGHT + GAP_Y)) / 2;
    sorted.forEach((component, row) => {
      rowOf.set(component.id, row);
      placed.push({
        component,
        column,
        row,
        x: column * (NODE_WIDTH + GAP_X),
        y: offset + row * (NODE_HEIGHT + GAP_Y),
      });
    });
  }
  return {
    nodes: placed,
    width: (Math.max(0, ...order) + 1) * (NODE_WIDTH + GAP_X) - GAP_X,
    height: tallest * (NODE_HEIGHT + GAP_Y) - GAP_Y,
  };
}

/** "refund_payment writes payments-db", read from an edge of the view. */
export function edgeSentence(
  edge: GraphEdge,
  byId: ReadonlyMap<string, GraphComponent>,
  phrase: string,
): string {
  const from = byId.get(edge.from)?.label ?? edge.from;
  const to = byId.get(edge.to)?.label ?? edge.to;
  return `${from} ${phrase} ${to}`;
}

/** Confidence as a percentage ("90%"). */
export function confidencePercent(c: number | null | undefined): string {
  if (typeof c !== "number" || !Number.isFinite(c)) return "—";
  return `${Math.round(c * 100)}%`;
}

/** A dependency a tool can be mapped to by hand (the mapping API's kinds). */
export const DEPENDENCY_KINDS = [
  "SERVICE",
  "HTTP_API",
  "DATABASE",
  "QUEUE",
  "EXTERNAL_SYSTEM",
  "MCP_SERVER",
] as const;
export const DEPENDENCY_RELATIONS = [
  "DEPENDS_ON",
  "CALLS",
  "READS",
  "WRITES",
  "PUBLISHES",
  "CONSUMES",
  "CAN_MUTATE",
] as const;
export const CRITICALITIES = ["LOW", "MEDIUM", "HIGH", "CRITICAL"] as const;

export interface MappingRow {
  kind: (typeof DEPENDENCY_KINDS)[number];
  name: string;
  relation: (typeof DEPENDENCY_RELATIONS)[number];
  /** Empty: keep what is recorded about the system. */
  criticality: "" | (typeof CRITICALITIES)[number];
}

/**
 * A tool's current manual mapping, from its relations: the outgoing ones a
 * `MANUAL` evidence supports (the mapping replaces exactly these).
 */
export function manualMapping(
  relations: readonly {
    direction: string;
    type: string;
    component: { kind: string; key: string };
    evidence: readonly { source: string }[];
  }[],
): MappingRow[] {
  return relations.flatMap((r) => {
    if (r.direction !== "out" || !r.evidence.some((e) => e.source === "MANUAL")) return [];
    const kind = r.component.kind as MappingRow["kind"];
    const relation = r.type as MappingRow["relation"];
    if (!DEPENDENCY_KINDS.includes(kind) || !DEPENDENCY_RELATIONS.includes(relation)) return [];
    return [{ kind, name: r.component.key, relation, criticality: "" as const }];
  });
}

/** The mapping request's `depends_on`, rows without a name left out, duplicates merged. */
export function mappingBody(rows: readonly MappingRow[]): {
  kind: MappingRow["kind"];
  name: string;
  relation: MappingRow["relation"];
  criticality?: (typeof CRITICALITIES)[number];
}[] {
  const seen = new Set<string>();
  return rows.flatMap((r) => {
    const name = r.name.trim();
    const id = `${r.kind}:${name}:${r.relation}`;
    if (!name || seen.has(id)) return [];
    seen.add(id);
    return [
      { kind: r.kind, name, relation: r.relation, ...(r.criticality ? { criticality: r.criticality } : {}) },
    ];
  });
}
