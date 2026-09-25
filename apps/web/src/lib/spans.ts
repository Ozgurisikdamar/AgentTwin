import type { Span } from "./types";

/** A span positioned in the trace tree and on the trace timeline. */
export interface SpanRow {
  span: Span;
  depth: number;
  /** Offset from trace start and duration, both in milliseconds. */
  offsetMs: number;
  durationMs: number;
  hasChildren: boolean;
  /** Ancestor span ids, root first (used for collapsing). */
  ancestors: string[];
}

export interface Waterfall {
  rows: SpanRow[];
  startMs: number;
  totalMs: number;
  /** Spans whose parent is not part of the trace (still shown, at the top level). */
  orphans: number;
}

const time = (iso: string): number => {
  const t = Date.parse(iso);
  return Number.isNaN(t) ? 0 : t;
};

/**
 * Builds the waterfall: a depth-first ordering of the span tree where
 * siblings are ordered by start time. Spans whose parent is missing (partial
 * traces) become roots; parent cycles in malformed data are broken rather than
 * followed, so every span appears exactly once.
 */
export function buildWaterfall(spans: readonly Span[]): Waterfall {
  if (spans.length === 0) return { rows: [], startMs: 0, totalMs: 0, orphans: 0 };
  const byId = new Map<string, Span>();
  for (const s of spans) if (!byId.has(s.span_id)) byId.set(s.span_id, s);
  const unique = [...byId.values()];

  const children = new Map<string, Span[]>();
  const roots: Span[] = [];
  let orphans = 0;
  for (const s of unique) {
    const parent = s.parent_span_id;
    if (parent && parent !== s.span_id && byId.has(parent)) {
      const list = children.get(parent) ?? [];
      list.push(s);
      children.set(parent, list);
    } else {
      if (parent && parent !== s.span_id) orphans++;
      roots.push(s);
    }
  }
  const order = (a: Span, b: Span) =>
    time(a.started_at) - time(b.started_at) || a.span_id.localeCompare(b.span_id);
  roots.sort(order);
  for (const list of children.values()) list.sort(order);

  let startMs = Infinity;
  let endMs = -Infinity;
  for (const s of unique) {
    startMs = Math.min(startMs, time(s.started_at));
    endMs = Math.max(endMs, time(s.ended_at), time(s.started_at));
  }
  const totalMs = Math.max(endMs - startMs, 0);

  const rows: SpanRow[] = [];
  const visited = new Set<string>();
  const visit = (s: Span, depth: number, ancestors: string[]) => {
    if (visited.has(s.span_id)) return;
    visited.add(s.span_id);
    const kids = (children.get(s.span_id) ?? []).filter((k) => !visited.has(k.span_id));
    const start = time(s.started_at);
    rows.push({
      span: s,
      depth,
      offsetMs: start - startMs,
      durationMs: Math.max(s.duration_ms ?? time(s.ended_at) - start, 0),
      hasChildren: kids.length > 0,
      ancestors,
    });
    for (const k of kids) visit(k, depth + 1, [...ancestors, s.span_id]);
  };
  for (const r of roots) visit(r, 0, []);
  // Spans only reachable through a cycle were not visited: show them as roots.
  for (const s of unique.sort(order)) if (!visited.has(s.span_id)) visit(s, 0, []);
  return { rows, startMs: startMs === Infinity ? 0 : startMs, totalMs, orphans };
}

/** Rows visible when the spans in `collapsed` hide their descendants. */
export function visibleRows(rows: readonly SpanRow[], collapsed: ReadonlySet<string>): SpanRow[] {
  if (collapsed.size === 0) return [...rows];
  return rows.filter((r) => !r.ancestors.some((a) => collapsed.has(a)));
}

/** Left offset and width of a span bar as percentages of the trace duration.
 *  Very short spans keep a minimum visible width. */
export function barGeometry(
  row: SpanRow,
  totalMs: number,
  minWidthPct = 0.4,
): { left: number; width: number } {
  if (totalMs <= 0) return { left: 0, width: 100 };
  const left = Math.min(Math.max((row.offsetMs / totalMs) * 100, 0), 100);
  const width = Math.max((row.durationMs / totalMs) * 100, minWidthPct);
  return { left, width: Math.min(width, 100 - left) || minWidthPct };
}

/**
 * Short name for dense rows. Tool spans are named `execute_tool <tool>` by the
 * GenAI conventions; the kind icon already says "tool call", so rows show the
 * tool name. The full span name stays in the accessible label and details.
 */
export function displayName(span: Pick<Span, "kind" | "name">): string {
  const prefix = "execute_tool ";
  if (span.kind === "tool" && span.name.startsWith(prefix) && span.name.length > prefix.length) {
    return span.name.slice(prefix.length);
  }
  return span.name;
}

export type LabelPlacement = "after" | "before" | "inside";

/**
 * Where a bar's duration label fits without leaving the timeline column:
 * after the bar when there is room on the right, before it when there is
 * room on the left, otherwise inside the (necessarily wide) bar.
 */
export function labelPlacement(left: number, width: number, reservePct = 15): LabelPlacement {
  if (left + width <= 100 - reservePct) return "after";
  if (left >= reservePct) return "before";
  return "inside";
}

/** Tool results after which repeating the same call is a retry. */
const FAILED_RESULTS = new Set(["error", "timeout", "rate_limited", "denied", "invalid"]);

/**
 * Tool calls that retry an earlier one: the SDK reported an attempt above 1,
 * or the previous call of the same tool had identical arguments and failed.
 * Repeating a call that succeeded - a re-read to confirm an action - is a new
 * call. The rule is the trace service's, so this list and the trace's retry
 * count agree.
 */
export function retriedToolCalls(spans: readonly Span[]): Span[] {
  const previous = new Map<string, { result: string; argsHash: string }>();
  const out: Span[] = [];
  const tools = spans
    .filter((s) => s.kind === "tool")
    .sort((a, b) => time(a.started_at) - time(b.started_at));
  for (const s of tools) {
    const a = s.attributes ?? {};
    const name = a.tool_name || s.name;
    const result = a.tool_result_status || (s.status === "ERROR" ? "error" : "ok");
    const argsHash = a.tool_args_hash ?? "";
    const last = previous.get(name);
    const repeatsFailure =
      last !== undefined && FAILED_RESULTS.has(last.result) && last.argsHash === argsHash;
    if ((a.attempt ?? 1) > 1 || repeatsFailure) out.push(s);
    previous.set(name, { result, argsHash });
  }
  return out;
}
