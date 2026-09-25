"use client";

import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { Plus, RefreshCw } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";
import { PageHeader } from "@/components/common/page-header";
import { useCan, useMe } from "@/components/shell/me-context";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Label, Select } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { api, withQuery } from "@/lib/api";
import { queryOf } from "@/lib/api/simulation";
import { formatDuration, formatRelative, shortId } from "@/lib/format";
import { RUN_STATUSES, actorLabel, asRunStatus, isRunActive, runDurationMs } from "@/lib/simulations";
import type { Project, SimulationCapabilities, SimulationPage, SimulationRun } from "@/lib/types";
import { useUrlQuery } from "@/lib/use-url-query";
import { RunStatusBadge } from "./badges";
import { RunProgress } from "./run-progress";

const PAGE_SIZE = 25;
/** Poll quickly while a run on the page is moving, slowly otherwise. */
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

function RunRow({ run, now, meUserId }: { run: SimulationRun; now: Date; meUserId?: string }) {
  return (
    <tr className="align-top" data-testid="simulation-row" data-run-id={run.id} data-status={run.status}>
      <td className="px-3 py-2.5">
        <Link
          href={`/simulations/${run.id}`}
          className="font-medium text-indigo-700 hover:underline"
          aria-label={`Simulation ${shortId(run.id)} of ${run.agent_name} v${run.agent_version}`}
        >
          {run.agent_name}
        </Link>{" "}
        <Badge tone="brand">v{run.agent_version}</Badge>
        <div className="mt-0.5 font-mono text-xs text-slate-500">{shortId(run.id)}</div>
      </td>
      <td className="px-3 py-2.5">
        <RunStatusBadge status={run.status} />
        {run.cancel_requested && isRunActive(run.status) ? (
          <div className="mt-1 text-xs text-slate-500">cancelling…</div>
        ) : null}
      </td>
      <td className="px-3 py-2.5">
        <RunProgress run={run} />
      </td>
      <td className="px-3 py-2.5">
        {run.critical_failures > 0 ? (
          <Badge tone="danger" title="Failed scenarios of critical severity">
            {run.critical_failures} critical
          </Badge>
        ) : (
          <span className="text-xs text-slate-500">none</span>
        )}
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

export function SimulationList() {
  const { params, update, replace } = useUrlQuery();
  const status = params.get("status") ?? "";
  const agent = params.get("agent") ?? "";
  const projectId = params.get("project_id") ?? "";
  const canRun = useCan("simulation.run");
  const me = useMe();
  const now = useNow(5_000);

  const setParam = (key: string, value: string) => update({ [key]: value });

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
    queryKey: ["simulations", status, agent, projectId],
    queryFn: ({ pageParam, signal }) => {
      const params = queryOf<"listSimulations">({
        limit: PAGE_SIZE,
        status: asRunStatus(status),
        agent,
        project_id: projectId,
        cursor: pageParam,
      });
      return api<SimulationPage>(withQuery("/simulations", params), { signal });
    },
    initialPageParam: "",
    getNextPageParam: (last) => last.next_cursor || undefined,
    refetchInterval: (query) =>
      query.state.data?.pages.some((p) => p.items.some((r) => isRunActive(r.status)))
        ? ACTIVE_INTERVAL_MS
        : IDLE_INTERVAL_MS,
  });
  const items = runs.data?.pages.flatMap((p) => p.items) ?? [];
  const filtered = Boolean(status || agent || projectId);
  const projectList = projects.data?.items ?? [];

  return (
    <div className="space-y-4">
      <PageHeader
        title="Simulations"
        description="Agent versions run against scenarios on stateful tool twins, with injected faults and verified final state."
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
                href={projectId ? `/simulations/new?project_id=${projectId}` : "/simulations/new"}
                className={buttonVariants({ variant: "primary", size: "sm" })}
              >
                <Plus className="h-4 w-4" aria-hidden="true" />
                New simulation
              </Link>
            ) : null}
          </>
        }
      />
      <Card className="flex flex-wrap items-end gap-3 p-3">
        {projectList.length > 1 ? (
          <div className="w-48">
            <Label htmlFor="sim-project">Project</Label>
            <Select
              id="sim-project"
              value={projectId}
              onChange={(e) => setParam("project_id", e.target.value)}
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
          <Label htmlFor="sim-agent">Agent</Label>
          <Select id="sim-agent" value={agent} onChange={(e) => setParam("agent", e.target.value)}>
            <option value="">All agents</option>
            {(capabilities.data?.agents ?? (agent ? [agent] : [])).map((a) => (
              <option key={a} value={a}>
                {a}
              </option>
            ))}
          </Select>
        </div>
        <div className="w-44">
          <Label htmlFor="sim-status">Status</Label>
          <Select id="sim-status" value={status} onChange={(e) => setParam("status", e.target.value)}>
            <option value="">Any status</option>
            {RUN_STATUSES.map((s) => (
              <option key={s} value={s}>
                {s.charAt(0) + s.slice(1).toLowerCase()}
              </option>
            ))}
          </Select>
        </div>
        {filtered ? (
          <Button size="sm" variant="ghost" onClick={() => replace(new URLSearchParams())}>
            Clear filters
          </Button>
        ) : null}
      </Card>
      <Card>
        {runs.isPending ? (
          <div className="space-y-2 p-4" aria-busy="true" aria-label="Loading simulations">
            {Array.from({ length: 5 }, (_, i) => (
              <Skeleton key={i} className="h-12 w-full" />
            ))}
          </div>
        ) : runs.isError ? (
          <div className="p-4">
            <ErrorState error={runs.error} />
          </div>
        ) : items.length === 0 ? (
          <EmptyState title={filtered ? "No simulations match these filters" : "No simulations yet"}>
            {filtered ? (
              "Clear the filters to see every run."
            ) : (
              <>
                Run an agent version against its scenarios
                {canRun ? (
                  <>
                    {" "}
                    with{" "}
                    <Link href="/simulations/new" className="text-indigo-700 underline">
                      New simulation
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
            <div className="overflow-x-auto">
              <table className="w-full min-w-[56rem] text-left text-sm">
                <caption className="sr-only">Simulation runs, newest first</caption>
                <thead className="border-b border-slate-100 text-xs uppercase tracking-wide text-slate-500">
                  <tr>
                    <th scope="col" className="px-3 py-2 font-medium">
                      Agent
                    </th>
                    <th scope="col" className="px-3 py-2 font-medium">
                      Status
                    </th>
                    <th scope="col" className="px-3 py-2 font-medium">
                      Scenarios
                    </th>
                    <th scope="col" className="px-3 py-2 font-medium">
                      Critical scenarios failed
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
                    <RunRow key={r.id} run={r} now={now} meUserId={me?.user.id} />
                  ))}
                </tbody>
              </table>
            </div>
            <div className="flex items-center justify-between border-t border-slate-100 px-3 py-2 text-xs text-slate-500">
              <span aria-live="polite">
                Showing {items.length} {items.length === 1 ? "run" : "runs"}
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
