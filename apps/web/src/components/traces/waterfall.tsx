"use client";

import { ChevronDown, ChevronRight } from "lucide-react";
import { useMemo, useState } from "react";
import { formatDuration } from "@/lib/format";
import { type Waterfall, barGeometry, displayName, labelPlacement, visibleRows } from "@/lib/spans";
import type { SpanKind } from "@/lib/types";
import { cn } from "@/lib/utils";
import { KIND_LABEL, KindIcon, RiskMarker } from "./badges";

const BAR_COLOR: Record<SpanKind, string> = {
  agent: "bg-slate-500",
  model: "bg-violet-500",
  tool: "bg-sky-500",
  retrieval: "bg-teal-500",
  policy: "bg-indigo-500",
  outcome: "bg-emerald-500",
  http: "bg-slate-400",
  mcp: "bg-cyan-500",
  other: "bg-slate-400",
};

export const LEGEND: SpanKind[] = ["agent", "model", "tool", "retrieval", "policy", "outcome"];

/**
 * The span tree on a shared timeline. Each row is keyboard reachable: the
 * toggle collapses children, the row button selects the span. The textual
 * label of every row carries timing, so nothing depends on the bars alone.
 */
export function WaterfallView({
  waterfall,
  selected,
  onSelect,
}: {
  waterfall: Waterfall;
  selected: string | null;
  onSelect: (spanId: string) => void;
}) {
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(new Set());
  const rows = useMemo(() => visibleRows(waterfall.rows, collapsed), [waterfall.rows, collapsed]);
  const toggle = (id: string) =>
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  const ticks = [0, 0.25, 0.5, 0.75, 1];

  return (
    <div>
      <div className="flex flex-wrap items-center gap-3 border-b border-slate-100 px-4 py-2 text-xs text-slate-600">
        {LEGEND.map((k) => (
          <span key={k} className="inline-flex items-center gap-1">
            <span aria-hidden="true" className={cn("h-2.5 w-2.5 rounded-sm", BAR_COLOR[k])} />
            {KIND_LABEL[k]}
          </span>
        ))}
        <span className="inline-flex items-center gap-1">
          <span aria-hidden="true" className="h-2.5 w-2.5 rounded-sm bg-rose-500" />
          Error
        </span>
        {waterfall.orphans > 0 ? (
          <span className="text-amber-800">
            {waterfall.orphans} span{waterfall.orphans === 1 ? "" : "s"} with a missing parent shown at top
            level
          </span>
        ) : null}
      </div>
      <div className="grid grid-cols-[minmax(16rem,38%)_1fr] border-b border-slate-100 px-4 py-1 text-[11px] text-slate-500">
        <span>Span</span>
        <div className="relative h-4" aria-hidden="true">
          {ticks.map((t) => (
            <span
              key={t}
              className="absolute -translate-x-1/2 whitespace-nowrap tabular-nums first:translate-x-0 last:-translate-x-full"
              style={{ left: `${t * 100}%` }}
            >
              {formatDuration(t * waterfall.totalMs)}
            </span>
          ))}
        </div>
      </div>
      <ul aria-label="Spans in execution order" className="divide-y divide-slate-50">
        {rows.map((row) => {
          const { span } = row;
          const isSelected = span.span_id === selected;
          const isError = span.status === "ERROR";
          const isCollapsed = collapsed.has(span.span_id);
          const { left, width } = barGeometry(row, waterfall.totalMs);
          const placement = labelPlacement(left, width);
          const risk =
            span.tool_risk && span.tool_risk !== "READ"
              ? `, ${span.tool_risk.replaceAll("_", " ").toLowerCase()} risk`
              : "";
          const label = `${KIND_LABEL[span.kind] ?? "Span"} ${span.name}${risk}, ${isError ? "error" : "ok"}, starts at ${formatDuration(row.offsetMs)}, lasts ${formatDuration(row.durationMs)}`;
          return (
            <li
              key={span.span_id}
              data-testid="waterfall-row"
              data-kind={span.kind}
              className={cn(
                "grid grid-cols-[minmax(16rem,38%)_1fr] items-center",
                isSelected ? "bg-indigo-50" : "hover:bg-slate-50",
              )}
            >
              <div
                className="flex min-w-0 items-center gap-1 py-1 pl-2 pr-2"
                style={{ paddingLeft: `${0.5 + row.depth * 1}rem` }}
              >
                {row.hasChildren ? (
                  <button
                    type="button"
                    onClick={() => toggle(span.span_id)}
                    aria-expanded={!isCollapsed}
                    aria-label={`${isCollapsed ? "Expand" : "Collapse"} ${span.name}`}
                    className="flex h-5 w-5 shrink-0 items-center justify-center rounded text-slate-500 hover:bg-slate-200"
                  >
                    {isCollapsed ? (
                      <ChevronRight className="h-3.5 w-3.5" aria-hidden="true" />
                    ) : (
                      <ChevronDown className="h-3.5 w-3.5" aria-hidden="true" />
                    )}
                  </button>
                ) : (
                  <span className="w-5 shrink-0" aria-hidden="true" />
                )}
                <button
                  type="button"
                  onClick={() => onSelect(span.span_id)}
                  aria-pressed={isSelected}
                  aria-label={label}
                  className="flex min-w-0 flex-1 items-center gap-1.5 rounded px-1 py-0.5 text-left text-sm"
                >
                  <span className={cn("shrink-0", isError ? "text-rose-600" : "text-slate-500")}>
                    <KindIcon kind={span.kind} />
                  </span>
                  <span
                    title={span.name}
                    className={cn("truncate", isError ? "text-rose-800" : "text-slate-800")}
                  >
                    {displayName(span)}
                  </span>
                  <RiskMarker risk={span.tool_risk} />
                </button>
              </div>
              <div className="relative mr-4 h-6" aria-hidden="true">
                <div
                  className={cn(
                    "absolute top-1.5 h-3 rounded-sm",
                    isError ? "bg-rose-500" : BAR_COLOR[span.kind],
                  )}
                  style={{ left: `${left}%`, width: `${width}%` }}
                />
                <span
                  data-placement={placement}
                  className={cn(
                    "absolute whitespace-nowrap text-[11px] tabular-nums",
                    placement === "inside"
                      ? "top-[3px] mr-1 rounded bg-white/90 px-1 leading-4 text-slate-700"
                      : "top-1 px-1 text-slate-500",
                  )}
                  style={
                    placement === "after"
                      ? { left: `${left + width}%` }
                      : placement === "before"
                        ? { right: `${100 - left}%` }
                        : { right: `${100 - (left + width)}%` }
                  }
                >
                  {formatDuration(row.durationMs)}
                </span>
              </div>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
