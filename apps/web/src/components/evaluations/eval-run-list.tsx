"use client";

import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { Plus, RefreshCw } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";
import { PageHeader } from "@/components/common/page-header";
import { useCan, useMe } from "@/components/shell/me-context";
import { RunStatusBadge } from "@/components/simulations/badges";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Label, Select } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { api, withQuery } from "@/lib/api";
import { type EvalRun, type EvalRunPage, queryOf } from "@/lib/api/evaluation";
import { CLASSIFICATIONS, CLASSIFICATION_LABEL, countLabel, suiteLabel } from "@/lib/evaluations";
import { formatDuration, formatRelative, shortId } from "@/lib/format";
import { RUN_STATUSES, actorLabel, asRunStatus, isRunActive, runDurationMs } from "@/lib/simulations";
import type { Project, SimulationCapabilities } from "@/lib/types";
import { useUrlQuery } from "@/lib/use-url-query";

const PAGE_SIZE = 25;
const ACTIVE_INTERVAL_MS = 2_000;
const IDLE_INTERVAL_MS = 15_000;

function useNow(intervalMs: number): Date {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), intervalMs);
    return () => clearInterval(t);
  }, [intervalMs]);
  return now;
}

const COUNT_TONE = {
  NEW_CRITICAL_FAILURE: "danger",
  REGRESSED: "danger",
  INCOMPLETE: "warning",
  IMPROVED: "success",
  UNCHANGED: "neutral",
} as const;

/** The non-zero classification counts of a run, worst first. */
export function CountBadges({ run }: { run: Pick<EvalRun, "counts" | "status"> }) {
  const shown = CLASSIFICATIONS.filter((c) => run.counts[c] > 0);
  if (shown.length === 0) {
    return <span className="text-xs text-slate-500">{run.status === "COMPLETED" ? "no cases" : "—"}</span>;
  }
  return (
    <div className="flex flex-wrap gap-1" data-testid="count-badges">
      {shown.map((c) => (
        <Badge key={c} tone={COUNT_TONE[c]} data-classification={c} title={CLASSIFICATION_LABEL[c]}>
          {countLabel(c, run.counts[c])}
        </Badge>
      ))}
    </div>
  );
}

function EvalRunRow({ run, now, meUserId }: { run: EvalRun; now: Date; meUserId?: string }) {
  return (
    <tr className="align-top" data-testid="eval-run-row" data-run-id={run.id} data-status={run.status}>
      <td className="px-3 py-2.5">
        <Link
          href={`/evaluations/${run.id}`}
          className="font-medium text-indigo-700 hover:underline"
          aria-label={`Evaluation ${shortId(run.id)}: ${run.agent_name} v${run.baseline_version} against v${run.candidate_version}`}
        >
          {run.agent_name}
        </Link>
        <div className="mt-1 flex flex-wrap items-center gap-1 text-xs text-slate-600">
          <Badge tone="neutral" title="Baseline">
            v{run.baseline_version}
          </Badge>
          <span aria-hidden="true">→</span>
          <Badge tone="brand" title="Candidate">
            v{run.candidate_version}
          </Badge>
        </div>
        <div className="mt-0.5 font-mono text-xs text-slate-500">{shortId(run.id)}</div>
      </td>
      <td className="px-3 py-2.5">
        <RunStatusBadge status={run.status} />
        {run.cancel_requested && isRunActive(run.status) ? (
          <div className="mt-1 text-xs text-slate-500">cancelling…</div>
        ) : null}
      </td>
      <td className="px-3 py-2.5">
        <CountBadges run={run} />
      </td>
      <td className="px-3 py-2.5 text-slate-700">
        <span>{suiteLabel(run.selection)}</span>
        <div className="text-xs text-slate-500">
          {run.case_count} {run.case_count === 1 ? "case" : "cases"}
        </div>
      </td>
      <td className="px-3 py-2.5 text-slate-700">{actorLabel(run.requested_by, meUserId)}</td>
      <td className="px-3 py-2.5 text-slate-700" title={run.created_at}>
        {formatRelative(run.created_at, now)}
      </td>
      <td className="px-3 py-2.5 text-right tabular-nums text-slate-700">
        {formatDuration(runDurationMs(run, now))}
      </td>
    </tr>
  );
}

export function EvalRunList() {
  const { params, update, replace } = useUrlQuery();
  const status = params.get("status") ?? "";
  const agent = params.get("agent") ?? "";
  const projectId = params.get("project_id") ?? "";
  const datasetId = params.get("dataset_id") ?? "";
  const canRun = useCan("eval.run");
  const me = useMe();
  const now = useNow(5_000);

  const projects = useQuery({
    queryKey: ["projects"],
    queryFn: ({ signal }) => api<{ items: Project[] }>("/projects", { signal }),
    staleTime: 60_000,
  });
  const capabilities = useQuery({
    queryKey: ["simulation-capabilities"],
    queryFn: ({ signal }) => api<SimulationCapabilities>("/simulations/capabilities", { signal }),
    staleTime: 300_000,
  });
  const runs = useInfiniteQuery({
    queryKey: ["eval-runs", status, agent, projectId, datasetId],
    queryFn: ({ pageParam, signal }) => {
      const q = queryOf<"listEvalRuns">({
        limit: PAGE_SIZE,
        status: asRunStatus(status),
        agent,
        project_id: projectId,
        dataset_id: datasetId,
        cursor: pageParam,
      });
      return api<EvalRunPage>(withQuery("/eval-runs", q), { signal });
    },
    initialPageParam: "",
    getNextPageParam: (last) => last.next_cursor || undefined,
    refetchInterval: (query) =>
      query.state.data?.pages.some((p) => p.items.some((r) => isRunActive(r.status)))
        ? ACTIVE_INTERVAL_MS
        : IDLE_INTERVAL_MS,
  });
  const items = runs.data?.pages.flatMap((p) => p.items) ?? [];
  const filtered = Boolean(status || agent || projectId || datasetId);
  const projectList = projects.data?.items ?? [];

  return (
    <div className="space-y-4">
      <PageHeader
        title="Evaluations"
        description="A candidate agent version against its baseline on the same pinned scenarios, compared case by case: what newly fails, what regressed, what improved."
        actions={
          <>
            <Button size="sm" onClick={() => void runs.refetch()} disabled={runs.isFetching}>
              <RefreshCw
                className={`h-4 w-4 ${runs.isFetching ? "animate-spin motion-reduce:animate-none" : ""}`}
                aria-hidden="true"
              />
              Refresh
            </Button>
            {canRun ? (
              <Link
                href={projectId ? `/evaluations/new?project_id=${projectId}` : "/evaluations/new"}
                className={buttonVariants({ variant: "primary", size: "sm" })}
              >
                <Plus className="h-4 w-4" aria-hidden="true" />
                New evaluation
              </Link>
            ) : null}
          </>
        }
      />
      <Card className="flex flex-wrap items-end gap-3 p-3">
        {projectList.length > 1 ? (
          <div className="w-48">
            <Label htmlFor="eval-project">Project</Label>
            <Select
              id="eval-project"
              value={projectId}
              onChange={(e) => update({ project_id: e.target.value })}
            >
              <option value="">All projects</option>
              {projectList.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </Select>
          </div>
        ) : null}
        <div className="w-48">
          <Label htmlFor="eval-agent">Agent</Label>
          <Select id="eval-agent" value={agent} onChange={(e) => update({ agent: e.target.value })}>
            <option value="">All agents</option>
            {(capabilities.data?.agents ?? (agent ? [agent] : [])).map((a) => (
              <option key={a} value={a}>
                {a}
              </option>
            ))}
          </Select>
        </div>
        <div className="w-44">
          <Label htmlFor="eval-status">Status</Label>
          <Select id="eval-status" value={status} onChange={(e) => update({ status: e.target.value })}>
            <option value="">Any status</option>
            {RUN_STATUSES.map((s) => (
              <option key={s} value={s}>
                {s.charAt(0) + s.slice(1).toLowerCase()}
              </option>
            ))}
          </Select>
        </div>
        {datasetId ? (
          <div className="text-xs text-slate-600">
            Runs of dataset <code>{shortId(datasetId)}</code>
          </div>
        ) : null}
        {filtered ? (
          <Button size="sm" variant="ghost" onClick={() => replace(new URLSearchParams())}>
            Clear filters
          </Button>
        ) : null}
      </Card>
      <Card>
        {runs.isPending ? (
          <div className="space-y-2 p-4" aria-busy="true" aria-label="Loading evaluations">
            {Array.from({ length: 5 }, (_, i) => (
              <Skeleton key={i} className="h-12 w-full" />
            ))}
          </div>
        ) : runs.isError ? (
          <div className="p-4">
            <ErrorState error={runs.error} />
          </div>
        ) : items.length === 0 ? (
          <EmptyState title={filtered ? "No evaluations match these filters" : "No evaluations yet"}>
            {filtered ? (
              "Clear the filters to see every evaluation."
            ) : (
              <>
                Compare a candidate agent version with its baseline
                {canRun ? (
                  <>
                    {" "}
                    with{" "}
                    <Link href="/evaluations/new" className="text-indigo-700 underline">
                      New evaluation
                    </Link>
                  </>
                ) : null}
                , or load the demo workspace with <code className="rounded bg-slate-100 px-1">make seed</code>
                .
              </>
            )}
          </EmptyState>
        ) : (
          <>
            <div className="relative overflow-x-auto">
              <table className="w-full min-w-[60rem] text-left text-sm">
                <caption className="sr-only">Evaluation runs, newest first</caption>
                <thead className="border-b border-slate-100 text-xs uppercase tracking-wide text-slate-500">
                  <tr>
                    <th scope="col" className="px-3 py-2 font-medium">
                      Agent · baseline → candidate
                    </th>
                    <th scope="col" className="px-3 py-2 font-medium">
                      Status
                    </th>
                    <th scope="col" className="px-3 py-2 font-medium">
                      Result
                    </th>
                    <th scope="col" className="px-3 py-2 font-medium">
                      Suite
                    </th>
                    <th scope="col" className="px-3 py-2 font-medium">
                      Requested by
                    </th>
                    <th scope="col" className="px-3 py-2 font-medium">
                      Created
                    </th>
                    <th scope="col" className="px-3 py-2 text-right font-medium">
                      Duration
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {items.map((r) => (
                    <EvalRunRow key={r.id} run={r} now={now} meUserId={me?.user?.id} />
                  ))}
                </tbody>
              </table>
            </div>
            <div className="flex items-center justify-between border-t border-slate-100 px-3 py-2 text-xs text-slate-500">
              <span aria-live="polite">
                Showing {items.length} {items.length === 1 ? "evaluation" : "evaluations"}
              </span>
              {runs.hasNextPage ? (
                <Button
                  size="sm"
                  onClick={() => void runs.fetchNextPage()}
                  disabled={runs.isFetchingNextPage}
                >
                  {runs.isFetchingNextPage ? "Loading…" : "Load more"}
                </Button>
              ) : null}
            </div>
          </>
        )}
      </Card>
    </div>
  );
}
