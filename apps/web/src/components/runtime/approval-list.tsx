"use client";

import { useInfiniteQuery } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import Link from "next/link";
import { PageHeader } from "@/components/common/page-header";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Label, Select } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { api, withQuery } from "@/lib/api";
import { type Approval, type ApprovalPage, type ApprovalStatus, queryOf } from "@/lib/api/runtime";
import { formatRelative } from "@/lib/format";
import { APPROVAL_STATUSES, approvalStatusLabel, expiry } from "@/lib/runtime";
import { useUrlQuery } from "@/lib/use-url-query";
import { ProjectPicker, useProject } from "./project-picker";
import { ApprovalStatusBadge, RiskBadge } from "./runtime-badges";

const PAGE_SIZE = 25;

function statusOf(value: string | null): ApprovalStatus | "" {
  if (value === "all") return "";
  return APPROVAL_STATUSES.find((s) => s === value) ?? "PENDING";
}

/** One approval request as the inbox shows it: the exact action, and until when. */
function ApprovalRow({ a, now }: { a: Approval; now: Date }) {
  const due = expiry(a.expires_at, now);
  return (
    <tr className="align-top" data-testid="approval-row" data-approval-id={a.id}>
      <td className="px-3 py-2.5">
        <Link
          href={`/approvals/${a.id}?project_id=${a.project_id}`}
          className="font-medium text-indigo-700 hover:underline"
          aria-label={`Approval request: ${a.summary}`}
        >
          {a.summary}
        </Link>
        <div className="mt-1 text-xs text-slate-500">{a.reason}</div>
      </td>
      <td className="px-3 py-2.5">
        <code>{a.tool}</code>
        <div className="mt-1">
          <RiskBadge risk={a.risk} />
        </div>
      </td>
      <td className="px-3 py-2.5 text-slate-800">
        {a.agent}
        <div className="text-xs text-slate-500">
          v{a.agent_version} · {a.environment}
        </div>
      </td>
      <td className="px-3 py-2.5 text-slate-800">
        {a.policy}
        <div className="text-xs text-slate-500">{a.rule}</div>
      </td>
      <td className="px-3 py-2.5">
        <ApprovalStatusBadge status={a.status} />
      </td>
      <td className="px-3 py-2.5 text-slate-700" title={a.expires_at}>
        {a.status === "PENDING" || a.status === "APPROVED" ? (
          <span className={due.expired ? "text-slate-500" : "font-medium text-amber-900"}>{due.text}</span>
        ) : (
          "—"
        )}
      </td>
      <td className="px-3 py-2.5 text-slate-700" title={a.created_at}>
        {formatRelative(a.created_at, now)}
      </td>
      <td className="px-3 py-2.5">
        {a.trace_id ? (
          <Link
            href={`/traces/${a.trace_id}`}
            className="font-mono text-xs text-indigo-700 hover:underline"
            aria-label={`Trace of ${a.summary}`}
          >
            {a.trace_id.slice(0, 12)}
          </Link>
        ) : (
          "—"
        )}
      </td>
    </tr>
  );
}

const COLUMNS = ["Action", "Tool", "Agent", "Policy", "Status", "Expires", "Requested", "Trace"];

export function ApprovalList() {
  const { params, update } = useUrlQuery();
  const status = statusOf(params.get("status"));
  const now = new Date();
  const { projects, list, projectId } = useProject();

  const approvals = useInfiniteQuery({
    queryKey: ["approvals", projectId, status],
    queryFn: ({ pageParam, signal }) =>
      api<ApprovalPage>(
        withQuery(
          "/approvals",
          queryOf<"listApprovals">({
            project_id: projectId,
            status: status || undefined,
            limit: PAGE_SIZE,
            cursor: pageParam || undefined,
          }),
        ),
        { signal },
      ),
    initialPageParam: "",
    getNextPageParam: (last) => last.next_cursor || undefined,
    enabled: Boolean(projectId),
    refetchInterval: status === "PENDING" ? 15_000 : false,
  });
  const items = approvals.data?.pages.flatMap((p) => p.items) ?? [];

  return (
    <div className="space-y-4">
      <PageHeader
        title="Approvals"
        description="Actions the runtime gateway held for a person. Approving one lets that exact action run once; a changed request needs its own approval."
        actions={
          <Button
            size="sm"
            onClick={() => void approvals.refetch()}
            disabled={approvals.isFetching || !projectId}
          >
            <RefreshCw
              className={`h-4 w-4 ${approvals.isFetching ? "animate-spin motion-reduce:animate-none" : ""}`}
              aria-hidden="true"
            />
            Refresh
          </Button>
        }
      />
      <Card className="flex flex-wrap items-end gap-3 p-3">
        <ProjectPicker id="approval-project" list={list} projectId={projectId} />
        <div className="w-48">
          <Label htmlFor="approval-status">Show</Label>
          <Select
            id="approval-status"
            value={status || "all"}
            onChange={(e) => update({ status: e.target.value === "PENDING" ? "" : e.target.value })}
          >
            {APPROVAL_STATUSES.map((s) => (
              <option key={s} value={s}>
                {s === "PENDING" ? "Waiting for a person" : approvalStatusLabel(s)}
              </option>
            ))}
            <option value="all">All</option>
          </Select>
        </div>
      </Card>
      <Card>
        {projects.isPending || (Boolean(projectId) && approvals.isPending) ? (
          <div className="space-y-2 p-4" aria-busy="true" aria-label="Loading approval requests">
            {Array.from({ length: 3 }, (_, i) => (
              <Skeleton key={i} className="h-12 w-full" />
            ))}
          </div>
        ) : (projects.error ?? approvals.error) ? (
          <div className="p-4">
            <ErrorState error={projects.error ?? approvals.error} />
          </div>
        ) : items.length === 0 ? (
          <EmptyState
            title={status === "PENDING" ? "Nothing is waiting for a person" : "No approval requests"}
          >
            An agent calling its tools through the runtime gateway gets an action held here when a policy
            requires approval. Try it with{" "}
            <code className="rounded bg-slate-100 px-1">make demo-contained</code>.
          </EmptyState>
        ) : (
          <>
            <div className="relative overflow-x-auto">
              <table className="w-full min-w-[64rem] text-left text-sm">
                <caption className="sr-only">Approval requests, newest first</caption>
                <thead className="border-b border-slate-100 text-xs uppercase tracking-wide text-slate-500">
                  <tr>
                    {COLUMNS.map((c) => (
                      <th key={c} scope="col" className="px-3 py-2 font-medium">
                        {c}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {items.map((a) => (
                    <ApprovalRow key={a.id} a={a} now={now} />
                  ))}
                </tbody>
              </table>
            </div>
            <div className="flex items-center justify-between border-t border-slate-100 px-3 py-2 text-xs text-slate-500">
              <span aria-live="polite">
                Showing {items.length} {items.length === 1 ? "request" : "requests"}
              </span>
              {approvals.hasNextPage ? (
                <Button
                  size="sm"
                  onClick={() => void approvals.fetchNextPage()}
                  disabled={approvals.isFetchingNextPage}
                >
                  {approvals.isFetchingNextPage ? "Loading…" : "Load more"}
                </Button>
              ) : null}
            </div>
          </>
        )}
      </Card>
    </div>
  );
}
