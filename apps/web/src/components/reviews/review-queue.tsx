"use client";

import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { Scale } from "lucide-react";
import Link from "next/link";
import { PageHeader } from "@/components/common/page-header";
import { ClassificationBadge } from "@/components/evaluations/badges";
import { ResultBadge, SeverityBadge } from "@/components/simulations/badges";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Label, Select } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { api, withQuery } from "@/lib/api";
import { type EvalExpectationResult, type ReviewQueuePage, queryOf } from "@/lib/api/evaluation";
import { formatRelative } from "@/lib/format";
import type { Project } from "@/lib/types";
import { useUrlQuery } from "@/lib/use-url-query";

const PAGE_SIZE = 25;

/** Why a pending result needs a person, from what the judge did with it. */
export function pendingWhy(p: { status: string | null; critical: boolean }): string {
  if (p.status === "ERROR") return "the judge could not grade it";
  if (p.status === "SKIPPED") return "not judged (the judge budget was spent or it was not called)";
  return p.critical
    ? "critical, graded by a judge that is not calibrated for it"
    : "graded by an uncalibrated judge";
}

export function ReviewQueue() {
  const { params, update } = useUrlQuery();
  const projectId = params.get("project_id") ?? "";
  const projects = useQuery({
    queryKey: ["projects"],
    queryFn: ({ signal }) => api<{ items: Project[] }>("/projects", { signal }),
    staleTime: 60_000,
  });
  const queue = useInfiniteQuery({
    queryKey: ["review-queue", projectId],
    queryFn: ({ pageParam, signal }) =>
      api<ReviewQueuePage>(
        withQuery(
          "/reviews",
          queryOf<"listReviewQueue">({ project_id: projectId, limit: PAGE_SIZE, cursor: pageParam }),
        ),
        { signal },
      ),
    initialPageParam: "",
    getNextPageParam: (last) => last.next_cursor || undefined,
  });
  const items = queue.data?.pages.flatMap((p) => p.items) ?? [];
  const projectList = projects.data?.items ?? [];

  return (
    <div className="space-y-4">
      <PageHeader
        title="Reviews"
        description="Results of finished evaluations a person should decide: the judge could not grade them, or graded a critical expectation without being calibrated for it. A review replaces the result and the case is classified again."
        actions={
          <Link href="/judges" className={buttonVariants({ size: "sm" })}>
            <Scale className="h-4 w-4" aria-hidden="true" />
            Judge calibration
          </Link>
        }
      />
      {projectList.length > 1 ? (
        <Card className="flex flex-wrap items-end gap-3 p-3">
          <div className="w-48">
            <Label htmlFor="rq-project">Project</Label>
            <Select
              id="rq-project"
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
        </Card>
      ) : null}
      <Card>
        {queue.isPending ? (
          <div className="space-y-2 p-4" aria-busy="true" aria-label="Loading the review queue">
            {Array.from({ length: 3 }, (_, i) => (
              <Skeleton key={i} className="h-16 w-full" />
            ))}
          </div>
        ) : queue.isError ? (
          <div className="p-4">
            <ErrorState error={queue.error} />
          </div>
        ) : items.length === 0 ? (
          <EmptyState title="Nothing to review">
            Every judged result of the finished evaluations was graded by a judge calibrated for it, or has
            been reviewed. Any case can still be reviewed from its evaluation.
          </EmptyState>
        ) : (
          <>
            <ul className="divide-y divide-slate-100" aria-label="Cases to review">
              {items.map((item) => (
                <li
                  key={`${item.eval_run_id}:${item.scenario_name}`}
                  className="space-y-2 px-4 py-3"
                  data-testid="review-item"
                  data-scenario={item.scenario_name}
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <Link
                      href={`/evaluations/${item.eval_run_id}/cases/${encodeURIComponent(item.scenario_name)}`}
                      className="font-medium text-indigo-700 hover:underline"
                    >
                      {item.scenario_name}
                    </Link>
                    <ClassificationBadge classification={item.classification} />
                    <SeverityBadge severity={item.severity} />
                    <span className="text-xs text-slate-500">
                      {item.agent_name} v{item.baseline_version} → v{item.candidate_version} ·{" "}
                      <span title={item.finished_at}>{formatRelative(item.finished_at)}</span>
                    </span>
                  </div>
                  <ul
                    className="space-y-1 pl-2 text-sm"
                    aria-label={`Pending results of ${item.scenario_name}`}
                  >
                    {item.pending.map((p) => (
                      <li
                        key={`${p.side}:${p.expectation_id}`}
                        className="flex flex-wrap items-baseline gap-2"
                        data-testid="pending-result"
                      >
                        <Badge tone="neutral">{p.side.toLowerCase()}</Badge>
                        <span className="font-medium text-slate-900">{p.expectation_id}</span>
                        {p.critical ? <Badge tone="danger">critical</Badge> : null}
                        {p.status ? (
                          <ResultBadge status={p.status as EvalExpectationResult["status"]} />
                        ) : null}
                        <span className="text-xs text-slate-600">{pendingWhy(p)}</span>
                        {p.reason ? <p className="w-full pl-1 text-xs text-slate-500">{p.reason}</p> : null}
                      </li>
                    ))}
                  </ul>
                </li>
              ))}
            </ul>
            {queue.hasNextPage ? (
              <div className="border-t border-slate-100 px-3 py-2 text-right">
                <Button
                  size="sm"
                  onClick={() => void queue.fetchNextPage()}
                  disabled={queue.isFetchingNextPage}
                >
                  {queue.isFetchingNextPage ? "Loading…" : "Load more"}
                </Button>
              </div>
            ) : null}
          </>
        )}
      </Card>
    </div>
  );
}
