"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { Badge } from "@/components/ui/badge";
import { formatCost, formatDateTime, formatDuration, formatNumber, formatRelative } from "@/lib/format";
import type { Trace } from "@/lib/types";
import { OutcomeBadge, SignalBadge, StatusBadge } from "./badges";

export function traceHref(t: Pick<Trace, "trace_id" | "project_id">): string {
  return `/traces/${t.trace_id}?project_id=${t.project_id}`;
}

function Signals({ signals }: { signals: string[] | null }) {
  if (!signals?.length) return <span className="text-xs text-slate-500">none</span>;
  const shown = signals.slice(0, 3);
  return (
    <span className="flex flex-wrap gap-1">
      {shown.map((s) => (
        <SignalBadge key={s} signal={s} />
      ))}
      {signals.length > shown.length ? (
        <Badge tone="neutral" title={signals.slice(3).join(", ")}>
          +{signals.length - shown.length}
        </Badge>
      ) : null}
    </span>
  );
}

export function TraceTable({ traces, now }: { traces: Trace[]; now: Date }) {
  const router = useRouter();
  return (
    <div className="relative overflow-x-auto">
      <table className="w-full min-w-[960px] text-left text-sm">
        <caption className="sr-only">Traces, newest first</caption>
        <thead className="border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
          <tr>
            <th scope="col" className="px-3 py-2 font-medium">
              Agent
            </th>
            <th scope="col" className="px-3 py-2 font-medium">
              Started
            </th>
            <th scope="col" className="px-3 py-2 font-medium">
              Status
            </th>
            <th scope="col" className="px-3 py-2 font-medium">
              Outcome
            </th>
            <th scope="col" className="px-3 py-2 text-right font-medium">
              Duration
            </th>
            <th scope="col" className="px-3 py-2 text-right font-medium">
              Tools
            </th>
            <th scope="col" className="px-3 py-2 text-right font-medium">
              Tokens
            </th>
            <th scope="col" className="px-3 py-2 text-right font-medium">
              Cost
            </th>
            <th scope="col" className="px-3 py-2 font-medium">
              Signals
            </th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {traces.map((t) => {
            const href = traceHref(t);
            return (
              <tr
                key={`${t.project_id}:${t.trace_id}`}
                data-testid="trace-row"
                className="cursor-pointer hover:bg-slate-50"
                onClick={(e) => {
                  // Keep native behavior for links, selection and modifier clicks.
                  if ((e.target as HTMLElement).closest("a") || window.getSelection()?.toString()) return;
                  router.push(href);
                }}
              >
                <td className="px-3 py-2">
                  <Link href={href} className="font-medium text-indigo-700 hover:underline">
                    {t.agent_name ?? t.root_name ?? "unknown agent"}
                  </Link>
                  <div className="mt-0.5 flex flex-wrap items-center gap-1 text-xs text-slate-500">
                    {t.agent_version ? <Badge tone="brand">v{t.agent_version}</Badge> : null}
                    <span>{t.environment}</span>
                    {t.source !== "production" ? <Badge tone="info">{t.source}</Badge> : null}
                  </div>
                </td>
                <td className="whitespace-nowrap px-3 py-2 text-slate-700">
                  <time dateTime={t.started_at} title={formatDateTime(t.started_at)}>
                    {formatRelative(t.started_at, now)}
                  </time>
                </td>
                <td className="px-3 py-2">
                  <StatusBadge status={t.status} />
                  {!t.finalized ? <span className="ml-1 text-xs text-slate-500">settling…</span> : null}
                </td>
                <td className="px-3 py-2">
                  <OutcomeBadge status={t.outcome_status} verified={t.outcome_verified} />
                </td>
                <td className="whitespace-nowrap px-3 py-2 text-right tabular-nums">
                  {formatDuration(t.duration_ms)}
                </td>
                <td className="px-3 py-2 text-right tabular-nums">
                  {t.tool_call_count}
                  {t.retry_count > 0 ? (
                    <span className="ml-1 text-xs text-amber-700">({t.retry_count} retry)</span>
                  ) : null}
                </td>
                <td className="whitespace-nowrap px-3 py-2 text-right tabular-nums text-slate-700">
                  {formatNumber(t.input_tokens + t.output_tokens)}
                </td>
                <td className="whitespace-nowrap px-3 py-2 text-right tabular-nums text-slate-700">
                  {formatCost(t.cost_usd, t.cost_usd !== null)}
                </td>
                <td className="px-3 py-2">
                  <Signals signals={t.signals} />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
