"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import type { ReactNode } from "react";
import { PageHeader } from "@/components/common/page-header";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { api } from "@/lib/api";
import { formatCost, formatDuration, formatNumber, formatPercent, humanize } from "@/lib/format";
import type { TraceStats } from "@/lib/types";

function Metric({
  label,
  value,
  hint,
  href,
}: {
  label: string;
  value: ReactNode;
  hint?: string;
  href?: string;
}) {
  const body = (
    <>
      <p className="text-xs font-medium uppercase tracking-wide text-slate-500">{label}</p>
      <p className="mt-1 text-2xl font-semibold tabular-nums text-slate-900">{value}</p>
      {hint ? <p className="mt-0.5 text-xs text-slate-500">{hint}</p> : null}
    </>
  );
  return (
    <Card>
      <CardContent>
        {href ? (
          <Link
            href={href}
            className="block rounded focus-visible:outline-2 focus-visible:outline-indigo-600"
          >
            {body}
          </Link>
        ) : (
          body
        )}
      </CardContent>
    </Card>
  );
}

/** A labeled bar list; the numbers are the content, the bars are decoration. */
function Breakdown({
  title,
  data,
  hrefFor,
  labelFor = (k) => k,
}: {
  title: string;
  data: Record<string, number>;
  hrefFor?: (key: string) => string;
  labelFor?: (key: string) => string;
}) {
  const entries = Object.entries(data).sort((a, b) => b[1] - a[1]);
  const max = Math.max(1, ...entries.map(([, v]) => v));
  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
      </CardHeader>
      <CardContent>
        {entries.length === 0 ? (
          <p className="text-sm text-slate-500">No data in this period.</p>
        ) : (
          <ul className="space-y-2">
            {entries.map(([key, value]) => (
              <li key={key}>
                <div className="flex items-center justify-between gap-2 text-sm">
                  {hrefFor ? (
                    <Link href={hrefFor(key)} className="truncate text-indigo-700 hover:underline">
                      {labelFor(key)}
                    </Link>
                  ) : (
                    <span className="truncate text-slate-800">{labelFor(key)}</span>
                  )}
                  <span className="tabular-nums text-slate-700">{formatNumber(value)}</span>
                </div>
                <div className="mt-1 h-1.5 rounded bg-slate-100" aria-hidden="true">
                  <div className="h-1.5 rounded bg-indigo-500" style={{ width: `${(value / max) * 100}%` }} />
                </div>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

export function Overview() {
  const stats = useQuery({
    queryKey: ["trace-stats"],
    queryFn: ({ signal }) => api<{ from: string; to: string; stats: TraceStats }>("/trace-stats", { signal }),
    refetchInterval: 15_000,
  });

  return (
    <div className="space-y-4">
      <PageHeader title="Overview" description="Production health of your agents over the last 7 days." />
      {stats.isPending ? (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
          {Array.from({ length: 4 }, (_, i) => (
            <Skeleton key={i} className="h-24" />
          ))}
        </div>
      ) : stats.isError ? (
        <ErrorState error={stats.error} />
      ) : stats.data.stats.total === 0 ? (
        <Card>
          <EmptyState title="No agent runs in the last 7 days">
            Send traces with the AgentTwin SDK, or load the demo workspace with{" "}
            <code className="rounded bg-slate-100 px-1">make seed</code>.
          </EmptyState>
        </Card>
      ) : (
        <>
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
            <Metric label="Agent runs" value={formatNumber(stats.data.stats.total)} href="/traces" />
            <Metric
              label="Failure rate"
              value={formatPercent(stats.data.stats.error_rate)}
              hint={`${formatNumber(stats.data.stats.errors)} runs errored or failed`}
              href="/traces?outcome=FAILURE"
            />
            <Metric
              label="Latency"
              value={formatDuration(stats.data.stats.p50_ms)}
              hint={`p50 · p95 ${formatDuration(stats.data.stats.p95_ms)}`}
            />
            <Metric
              label="Claims contradicted"
              value={formatNumber(stats.data.stats.contradictions)}
              hint={`${formatNumber(stats.data.stats.unverified_outcomes)} outcomes are self-reported only`}
              href="/traces?signal=contradiction"
            />
          </div>
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2 xl:grid-cols-4">
            <Breakdown
              title="Outcomes"
              data={stats.data.stats.by_outcome}
              labelFor={(k) => (k === "NONE" ? "No outcome" : humanize(k))}
              hrefFor={(k) => (k === "NONE" ? "/traces" : `/traces?outcome=${k}`)}
            />
            <Breakdown
              title="Agent versions"
              data={stats.data.stats.by_agent_version}
              hrefFor={(k) => {
                const [agent, version] = k.split("@");
                return `/traces?agent=${encodeURIComponent(agent ?? "")}&agent_version=${encodeURIComponent(version ?? "")}`;
              }}
            />
            <Breakdown
              title="Failure signals"
              data={stats.data.stats.by_signal}
              labelFor={(k) => k.replaceAll("_", " ")}
              hrefFor={(k) => `/traces?signal=${encodeURIComponent(k)}`}
            />
            <Breakdown
              title="Tools called"
              data={stats.data.stats.by_tool}
              hrefFor={(k) => `/traces?tool=${encodeURIComponent(k)}`}
            />
          </div>
          <p className="text-xs text-slate-500">
            Total model cost in this period: {formatCost(stats.data.stats.cost_usd)} (only runs that report
            token prices are counted).
          </p>
        </>
      )}
    </div>
  );
}
