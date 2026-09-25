"use client";

import { useQuery } from "@tanstack/react-query";
import { ArrowLeft } from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";
import { PageHeader } from "@/components/common/page-header";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { CopyButton } from "@/components/ui/copy-button";
import { KeyValue } from "@/components/ui/key-value";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ApiError, api, withQuery } from "@/lib/api";
import { formatCost, formatDateTime, formatDuration, formatNumber, humanize, shortId } from "@/lib/format";
import { buildWaterfall, retriedToolCalls } from "@/lib/spans";
import type { Span, Trace, TraceDetail as TraceDetailData } from "@/lib/types";
import { OutcomeBadge, RiskBadge, SignalBadge, StatusBadge } from "./badges";
import { OutcomeCard } from "./outcome-card";
import { SpanDetails } from "./span-details";
import { WaterfallView } from "./waterfall";

/** Trace sources: live traffic vs. runs AgentTwin started itself (simulation, replay, evaluation). */
const SOURCE_LABEL: Record<string, string> = {
  production: "live traffic",
  simulation: "simulation run",
  replay: "replay",
  eval: "evaluation run",
  test: "test run",
};

function SummaryCard({ trace }: { trace: Trace }) {
  const s = trace.summary;
  return (
    <Card className="xl:col-span-2" data-testid="summary-card">
      <CardHeader>
        <CardTitle>Summary</CardTitle>
        <div className="flex flex-wrap justify-end gap-1">
          {trace.signals?.length ? (
            trace.signals.map((sig) => <SignalBadge key={sig} signal={sig} />)
          ) : (
            <span className="text-xs text-slate-500">No failure signals</span>
          )}
        </div>
      </CardHeader>
      <CardContent className="grid gap-6 lg:grid-cols-2">
        <KeyValue
          items={[
            {
              label: "Tool sequence",
              value: s?.tool_sequence_sketch ? (
                <code className="text-xs">{s.tool_sequence_sketch}</code>
              ) : null,
            },
            {
              label: "Steps",
              value: `${trace.span_count} spans · ${trace.model_call_count} model · ${trace.tool_call_count} tool`,
            },
            { label: "Retries", value: trace.retry_count },
            { label: "Errors", value: s?.errors?.length ? s.errors.join(", ") : "none" },
            {
              label: "Violations",
              value: s?.violations?.length ? (
                <span className="flex flex-wrap gap-1">
                  {s.violations.map((v) => (
                    <Badge key={v} tone="danger">
                      {v.replaceAll("_", " ")}
                    </Badge>
                  ))}
                </span>
              ) : (
                "none"
              ),
            },
            { label: "Last good step", value: s?.last_successful_step },
            { label: "Failing tool", value: s?.failing_tool },
          ]}
        />
        <KeyValue
          items={[
            { label: "Model", value: trace.models?.join(", ") },
            {
              label: "Tokens",
              value: `${formatNumber(trace.input_tokens)} in · ${formatNumber(trace.output_tokens)} out`,
            },
            { label: "Cost", value: formatCost(trace.cost_usd, trace.cost_usd !== null) },
            {
              label: "Prompt",
              value: trace.prompt_hash ? (
                <span className="inline-flex items-center gap-1">
                  <code className="text-xs">{shortId(trace.prompt_hash, 12)}</code>
                  <CopyButton value={trace.prompt_hash} label="prompt hash" />
                </span>
              ) : null,
            },
            {
              label: "Session",
              value: trace.session_id ? <code className="text-xs">{trace.session_id}</code> : null,
            },
            { label: "Release", value: trace.release_id },
            {
              label: "Commit",
              value: trace.commit_sha ? (
                <code className="text-xs">{shortId(trace.commit_sha, 12)}</code>
              ) : null,
            },
            {
              label: "Telemetry",
              value: `${trace.sdk_name ?? "unknown SDK"}${trace.sdk_version ? ` ${trace.sdk_version}` : ""} · ${trace.semconv_version}`,
            },
            {
              label: "Content",
              value: `${trace.content_mode}${trace.content_dropped ? " (some content dropped by policy)" : ""}${trace.content_purged ? " (purged by retention)" : ""}${trace.truncated ? " · truncated" : ""}`,
            },
          ]}
        />
      </CardContent>
    </Card>
  );
}

function SpanList({
  spans,
  empty,
  columns,
  onSelect,
}: {
  spans: Span[];
  empty: string;
  columns: { header: string; cell: (s: Span) => React.ReactNode; className?: string }[];
  onSelect: (id: string) => void;
}) {
  if (spans.length === 0) return <p className="px-1 py-3 text-sm text-slate-500">{empty}</p>;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead className="text-xs uppercase tracking-wide text-slate-500">
          <tr>
            <th scope="col" className="py-1.5 pr-3 font-medium">
              Span
            </th>
            {columns.map((c) => (
              <th key={c.header} scope="col" className={`py-1.5 pr-3 font-medium ${c.className ?? ""}`}>
                {c.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {spans.map((s) => (
            <tr key={s.span_id}>
              <td className="py-1.5 pr-3">
                <button
                  type="button"
                  className="text-left text-indigo-700 hover:underline"
                  onClick={() => onSelect(s.span_id)}
                >
                  {s.name}
                </button>
              </td>
              {columns.map((c) => (
                <td key={c.header} className={`py-1.5 pr-3 ${c.className ?? ""}`}>
                  {c.cell(s)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function TraceDetail({ traceId, projectId }: { traceId: string; projectId?: string }) {
  const query = useQuery({
    queryKey: ["trace", traceId, projectId ?? ""],
    queryFn: ({ signal }) =>
      api<TraceDetailData>(
        withQuery(`/traces/${traceId}`, new URLSearchParams(projectId ? { project_id: projectId } : {})),
        { signal },
      ),
    // Spans may still be arriving: refresh until the trace is finalized.
    refetchInterval: (q) => (q.state.data && !q.state.data.trace.finalized ? 2_000 : false),
  });
  const data = query.data;
  const waterfall = useMemo(() => buildWaterfall(data?.spans ?? []), [data?.spans]);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  if (query.isPending) {
    return (
      <div className="space-y-4" aria-busy="true" aria-label="Loading trace">
        <Skeleton className="h-8 w-80" />
        <Skeleton className="h-40 w-full" />
        <Skeleton className="h-72 w-full" />
      </div>
    );
  }
  if (query.isError) {
    const notFound = query.error instanceof ApiError && query.error.status === 404;
    return (
      <div className="space-y-4">
        <Link
          href="/traces"
          className="inline-flex items-center gap-1 text-sm text-indigo-700 hover:underline"
        >
          <ArrowLeft className="h-4 w-4" aria-hidden="true" />
          Back to traces
        </Link>
        {notFound ? (
          <Card>
            <EmptyState title="Trace not found">
              It may still be arriving from the collector, it may belong to a project you cannot access, or
              retention already removed it.
            </EmptyState>
          </Card>
        ) : (
          <ErrorState error={query.error} />
        )}
      </div>
    );
  }

  const { trace, spans, outcome, flags } = query.data;
  const selected =
    waterfall.rows.find((r) => r.span.span_id === selectedId) ??
    waterfall.rows.find((r) => r.span.span_id === trace.root_span_id) ??
    waterfall.rows[0];
  const modelSpans = spans.filter((s) => s.kind === "model");
  const toolSpans = spans.filter((s) => s.kind === "tool");
  const policySpans = spans.filter((s) => s.kind === "policy");
  const errorSpans = spans.filter((s) => s.status === "ERROR");
  const retries = retriedToolCalls(spans);
  const select = (id: string) => {
    setSelectedId(id);
    document.getElementById("span-panel")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  };

  return (
    <div className="space-y-4">
      <PageHeader
        eyebrow={
          <nav aria-label="Breadcrumb" className="flex items-center gap-1">
            <Link href="/traces" className="text-indigo-700 hover:underline">
              Traces
            </Link>
            <span aria-hidden="true">/</span>
            <code data-testid="trace-id">{trace.trace_id}</code>
            <CopyButton value={trace.trace_id} label="trace id" />
          </nav>
        }
        title={
          <span className="flex flex-wrap items-center gap-2">
            {trace.agent_name ?? trace.root_name ?? "Trace"}
            {trace.agent_version ? <Badge tone="brand">v{trace.agent_version}</Badge> : null}
            <StatusBadge status={trace.status} />
            <OutcomeBadge status={trace.outcome_status} verified={trace.outcome_verified} />
          </span>
        }
        description={
          <span className="flex flex-wrap gap-x-3 gap-y-1">
            <span title="Deployment environment">{humanize(trace.environment)} environment</span>
            <span title="Where the run came from">
              {SOURCE_LABEL[trace.source] ?? `${humanize(trace.source)} run`}
            </span>
            <time dateTime={trace.started_at}>{formatDateTime(trace.started_at)}</time>
            <span>{formatDuration(trace.duration_ms)}</span>
            {!trace.finalized ? <span className="text-amber-800">still receiving spans…</span> : null}
          </span>
        }
      />

      <div className="grid gap-4 xl:grid-cols-3">
        <OutcomeCard trace={trace} outcome={outcome} spans={spans} />
        <SummaryCard trace={trace} />
      </div>

      <Card data-testid="waterfall">
        <CardHeader>
          <CardTitle>Execution</CardTitle>
          <span className="text-xs text-slate-500">
            {spans.length} spans over {formatDuration(waterfall.totalMs)}
          </span>
        </CardHeader>
        <div className="grid xl:grid-cols-[minmax(0,1fr)_24rem]">
          <div className="min-w-0 overflow-x-auto border-slate-100 xl:border-r">
            <WaterfallView
              waterfall={waterfall}
              selected={selected?.span.span_id ?? null}
              onSelect={select}
            />
          </div>
          <aside
            id="span-panel"
            aria-label="Selected span"
            className="border-t border-slate-100 p-4 xl:border-t-0"
          >
            {selected ? (
              <SpanDetails
                span={selected.span}
                offsetMs={selected.offsetMs}
                contentMode={trace.content_mode}
              />
            ) : (
              <p className="text-sm text-slate-500">Select a span to see its details.</p>
            )}
          </aside>
        </div>
      </Card>

      <Card>
        <CardContent>
          <Tabs defaultValue="tools">
            <TabsList aria-label="Trace details">
              <TabsTrigger value="tools">Tool calls ({toolSpans.length})</TabsTrigger>
              <TabsTrigger value="models">Model calls ({modelSpans.length})</TabsTrigger>
              <TabsTrigger value="policy">Policy decisions ({policySpans.length})</TabsTrigger>
              <TabsTrigger value="errors">Errors ({errorSpans.length})</TabsTrigger>
              <TabsTrigger value="retries">Retries ({retries.length})</TabsTrigger>
              <TabsTrigger value="flags">Flags ({flags?.length ?? 0})</TabsTrigger>
            </TabsList>
            <TabsContent value="tools">
              <SpanList
                spans={toolSpans}
                empty="No tool calls in this run."
                onSelect={select}
                columns={[
                  {
                    header: "Risk",
                    cell: (s) => <RiskBadge risk={s.tool_risk ?? s.attributes?.tool_risk} />,
                  },
                  {
                    header: "Result",
                    cell: (s) =>
                      humanize(s.attributes?.tool_result_status ?? (s.status === "ERROR" ? "error" : "ok")),
                  },
                  { header: "Attempt", cell: (s) => s.attributes?.attempt ?? 1 },
                  {
                    header: "Idempotency key",
                    cell: (s) => (s.attributes?.idempotency_key_hash ? "yes" : "none"),
                  },
                  {
                    header: "Duration",
                    cell: (s) => formatDuration(s.duration_ms),
                    className: "text-right tabular-nums",
                  },
                ]}
              />
            </TabsContent>
            <TabsContent value="models">
              <SpanList
                spans={modelSpans}
                empty="No model calls in this run."
                onSelect={select}
                columns={[
                  { header: "Model", cell: (s) => s.model ?? s.attributes?.request_model },
                  {
                    header: "Tokens",
                    cell: (s) =>
                      `${formatNumber(s.attributes?.input_tokens)} / ${formatNumber(s.attributes?.output_tokens)}`,
                  },
                  { header: "Finish", cell: (s) => s.attributes?.finish_reasons?.join(", ") ?? "—" },
                  {
                    header: "Duration",
                    cell: (s) => formatDuration(s.duration_ms),
                    className: "text-right tabular-nums",
                  },
                ]}
              />
            </TabsContent>
            <TabsContent value="policy">
              <SpanList
                spans={policySpans}
                empty="No runtime policy decisions were recorded for this run."
                onSelect={select}
                columns={[
                  {
                    header: "Decision",
                    cell: (s) => humanize(s.policy_decision ?? s.attributes?.policy_decision),
                  },
                  { header: "Policy", cell: (s) => s.attributes?.policy_name ?? "—" },
                  { header: "Rule", cell: (s) => s.attributes?.policy_rule ?? "—" },
                ]}
              />
            </TabsContent>
            <TabsContent value="errors">
              <SpanList
                spans={errorSpans}
                empty="No errors in this run."
                onSelect={select}
                columns={[
                  {
                    header: "Type",
                    cell: (s) => s.attributes?.error_type ?? s.attributes?.exception_type ?? "error",
                  },
                  {
                    header: "Message",
                    cell: (s) => s.attributes?.exception_message ?? s.status_message ?? "—",
                  },
                ]}
              />
            </TabsContent>
            <TabsContent value="retries">
              <SpanList
                spans={retries}
                empty="No tool call was retried."
                onSelect={select}
                columns={[
                  { header: "Attempt", cell: (s) => s.attributes?.attempt ?? "repeat" },
                  {
                    header: "Idempotency key",
                    cell: (s) =>
                      s.attributes?.idempotency_key_hash ? "yes" : "none — duplicate side effects possible",
                  },
                  { header: "Result", cell: (s) => humanize(s.attributes?.tool_result_status) },
                ]}
              />
            </TabsContent>
            <TabsContent value="flags">
              {flags?.length ? (
                <ul className="divide-y divide-slate-100 text-sm">
                  {flags.map((f) => (
                    <li key={f.id} className="py-2">
                      <Badge tone="warning">{humanize(f.kind)}</Badge>{" "}
                      <span className="text-slate-800">{f.reason}</span>
                      <span className="ml-2 text-xs text-slate-500">
                        {f.flagged_by} · {formatDateTime(f.created_at)}
                      </span>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="px-1 py-3 text-sm text-slate-500">Nobody flagged this trace.</p>
              )}
            </TabsContent>
          </Tabs>
        </CardContent>
      </Card>
    </div>
  );
}
