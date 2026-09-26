"use client";

import { useQuery } from "@tanstack/react-query";
import { Activity, FileCode2 } from "lucide-react";
import Link from "next/link";
import { PageHeader } from "@/components/common/page-header";
import { OutcomeBadge } from "@/components/traces/badges";
import { Badge } from "@/components/ui/badge";
import { buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { KeyValue } from "@/components/ui/key-value";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { ApiError, api } from "@/lib/api";
import { formatDuration, humanize, shortId } from "@/lib/format";
import { FINAL_CASE_STATUSES, resultCounts } from "@/lib/simulations";
import type { CaseDetail as CaseDetailData, OutcomeStatus } from "@/lib/types";
import { CaseStatusBadge, FailureLabel, SeverityBadge } from "./badges";
import { ExpectationResults, FaultList, JsonBlock, StateDiff, Trajectory } from "./case-parts";

const ACTIVE_INTERVAL_MS = 1_500;
const OUTCOMES = new Set(["SUCCESS", "PARTIAL", "FAILURE", "UNKNOWN"]);

export function CaseDetail({ runId, caseId }: { runId: string; caseId: string }) {
  const detail = useQuery({
    queryKey: ["simulation-case", runId, caseId],
    queryFn: ({ signal }) => api<CaseDetailData>(`/simulations/${runId}/cases/${caseId}`, { signal }),
    refetchInterval: (q) =>
      q.state.data && !FINAL_CASE_STATUSES.has(q.state.data.case.status) ? ACTIVE_INTERVAL_MS : false,
  });

  if (detail.isPending) {
    return (
      <div className="space-y-4" aria-busy="true" aria-label="Loading scenario result">
        <Skeleton className="h-10 w-96" />
        <Skeleton className="h-48 w-full" />
        <Skeleton className="h-72 w-full" />
      </div>
    );
  }
  if (detail.isError) {
    const e = detail.error;
    if (e instanceof ApiError && e.status === 404) {
      return (
        <EmptyState title="Scenario result not found">
          <Link href={`/simulations/${runId}`} className="text-indigo-700 underline">
            Back to the simulation
          </Link>
        </EmptyState>
      );
    }
    return <ErrorState error={e} />;
  }

  const { case: c, scenario, steps, state, twin } = detail.data;
  const input = scenario.document.spec?.input ?? {};
  const agent = c.agent_result ?? {};
  const claimed = OUTCOMES.has(String(agent.claimed_outcome))
    ? (agent.claimed_outcome as OutcomeStatus)
    : null;
  const counts = resultCounts(c.results);
  const reason = c.error ?? c.verdict?.reason ?? c.reason;

  return (
    <div className="space-y-4">
      <PageHeader
        eyebrow={
          <span className="inline-flex flex-wrap items-center gap-1">
            <Link href="/simulations" className="text-indigo-700 hover:underline">
              Simulations
            </Link>
            <span aria-hidden="true">/</span>
            <Link href={`/simulations/${runId}`} className="font-mono text-indigo-700 hover:underline">
              {shortId(runId)}
            </Link>
            <span aria-hidden="true">/</span>
            <span>scenario</span>
          </span>
        }
        title={
          <span className="inline-flex flex-wrap items-center gap-2">
            {c.scenario_name}
            <SeverityBadge severity={c.severity} />
            <CaseStatusBadge status={c.status} />
          </span>
        }
        description={reason ? <span data-testid="case-reason">{reason}</span> : undefined}
        actions={
          <>
            {c.trace_id ? (
              <Link
                href={`/traces/${c.trace_id}`}
                className={buttonVariants({ size: "sm" })}
                data-testid="open-trace"
              >
                <Activity className="h-4 w-4" aria-hidden="true" />
                Open trace
              </Link>
            ) : null}
            <Link href={`/scenarios/${c.scenario_id}`} className={buttonVariants({ size: "sm" })}>
              <FileCode2 className="h-4 w-4" aria-hidden="true" />
              Scenario
            </Link>
          </>
        }
      />

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
        <Card className="xl:col-span-2" data-testid="conversation">
          <CardHeader>
            <CardTitle>Conversation</CardTitle>
            {claimed ? <OutcomeBadge status={claimed} verified={false} /> : null}
          </CardHeader>
          <CardContent className="space-y-3 text-sm">
            <div>
              <p className="mb-1 text-xs font-medium text-slate-500">Customer</p>
              <p className="whitespace-pre-wrap rounded-md bg-slate-50 p-3 text-slate-900">
                {input.message ?? "—"}
              </p>
            </div>
            <div>
              <p className="mb-1 text-xs font-medium text-slate-500">Agent</p>
              <p
                className="whitespace-pre-wrap rounded-md bg-indigo-50/60 p-3 text-slate-900"
                data-testid="agent-output"
              >
                {agent.output || (agent.error ? `The agent failed: ${agent.error}` : "—")}
              </p>
            </div>
            <KeyValue
              items={[
                {
                  label: "Context",
                  value: input.context ? (
                    <span className="flex flex-wrap gap-1">
                      {Object.entries(input.context).map(([k, v]) => (
                        <Badge key={k}>
                          {k}: {String(v)}
                        </Badge>
                      ))}
                    </span>
                  ) : null,
                },
                {
                  label: "Business outcome",
                  value: agent.business_outcome ? humanize(agent.business_outcome) : null,
                },
                { label: "Twin", value: twin ? `${twin.name} v${twin.version}` : null },
                { label: "Seed", value: <code className="text-xs">{c.seed}</code> },
                { label: "Duration", value: formatDuration(c.latency_ms) },
              ]}
            />
          </CardContent>
        </Card>
        <Card data-testid="verdict-card">
          <CardHeader>
            <CardTitle>Verdict</CardTitle>
            <CaseStatusBadge status={c.status} />
          </CardHeader>
          <CardContent className="space-y-3">
            <KeyValue
              items={[
                { label: "Passed", value: counts.PASS },
                { label: "Failed", value: counts.FAIL },
                { label: "Skipped", value: counts.SKIPPED },
                { label: "Errors", value: counts.ERROR },
                {
                  label: "Critical expectations failed",
                  value: c.verdict?.critical_failures ? (
                    <Badge tone="danger">{c.verdict.critical_failures}</Badge>
                  ) : (
                    "none"
                  ),
                },
              ]}
            />
            {c.labels.length ? (
              <div className="flex flex-wrap gap-1" aria-label="Failure labels">
                {c.labels.map((l) => (
                  <FailureLabel key={l} label={l} />
                ))}
              </div>
            ) : null}
            <p className="text-xs text-slate-500">
              A self-reported outcome is only a claim: expectations check it against the twin&apos;s final
              state.
            </p>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Expectations</CardTitle>
          <span className="text-xs text-slate-500">in the order the scenario declares them</span>
        </CardHeader>
        <ExpectationResults results={c.results} />
      </Card>

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-5">
        <Card className="xl:col-span-3">
          <CardHeader>
            <CardTitle>Trajectory</CardTitle>
            <span className="text-xs text-slate-500">
              {steps.length} {steps.length === 1 ? "step" : "steps"} against the twin
            </span>
          </CardHeader>
          <Trajectory steps={steps} />
        </Card>
        <Card className="xl:col-span-2">
          <CardHeader>
            <CardTitle>Injected faults</CardTitle>
          </CardHeader>
          <FaultList faults={scenario.faults} steps={steps} />
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Final state</CardTitle>
          <span className="text-xs text-slate-500">compared with the state the scenario started from</span>
        </CardHeader>
        <CardContent className="space-y-3">
          <StateDiff changes={c.state_diff} />
          <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
            <details className="rounded border border-slate-200">
              <summary className="cursor-pointer px-2 py-1 text-xs font-medium text-slate-700">
                Initial state
              </summary>
              <div className="border-t border-slate-100 p-2">
                <JsonBlock value={state.initial} label="Initial twin state" />
              </div>
            </details>
            <details className="rounded border border-slate-200">
              <summary className="cursor-pointer px-2 py-1 text-xs font-medium text-slate-700">
                Final state
              </summary>
              <div className="border-t border-slate-100 p-2">
                <JsonBlock value={state.final} label="Final twin state" />
              </div>
            </details>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
