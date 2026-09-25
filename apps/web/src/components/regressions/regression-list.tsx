"use client";

import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import Link from "next/link";
import { PageHeader } from "@/components/common/page-header";
import { SeverityBadge } from "@/components/simulations/badges";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Label, Select } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { api, withQuery } from "@/lib/api";
import { type Regression, type RegressionPage, queryOf } from "@/lib/api/evaluation";
import { formatRelative, shortId } from "@/lib/format";
import { FAILURE_LABELS, SEVERITIES, STATUS_VIEWS, statusView, taxonomyLabel } from "@/lib/regressions";
import type { Agent, Project } from "@/lib/types";
import { useUrlQuery } from "@/lib/use-url-query";
import { RegressionStatusBadge } from "./regression-badges";

const PAGE_SIZE = 25;

/** One mined failure group as the inbox shows it (spec §41.3). */
function RegressionRow({ r, now }: { r: Regression; now: Date }) {
  const relabelled = r.taxonomy !== r.suggested_taxonomy;
  return (
    <tr className="align-top" data-testid="regression-row" data-regression-id={r.id}>
      <td className="px-3 py-2.5">
        <Link
          href={`/regressions/${r.id}`}
          className="font-medium text-indigo-700 hover:underline"
          aria-label={`Regression: ${r.title}`}
        >
          {r.title}
        </Link>
        <div className="mt-1 text-xs text-slate-500">
          {r.agent} · cluster <code title={r.fingerprint}>{r.fingerprint.slice(0, 8)}</code>
        </div>
        {r.merged_into ? (
          <div className="mt-1 text-xs text-slate-500">
            Merged into{" "}
            <Link href={`/regressions/${r.merged_into}`} className="text-indigo-700 hover:underline">
              {shortId(r.merged_into)}
            </Link>
          </div>
        ) : null}
      </td>
      <td className="px-3 py-2.5">
        <RegressionStatusBadge status={r.status} />
      </td>
      <td className="px-3 py-2.5">
        <SeverityBadge severity={r.severity} />
      </td>
      <td className="px-3 py-2.5 text-slate-800">
        {taxonomyLabel(r.taxonomy)}
        {relabelled ? (
          <div className="mt-1 text-xs text-slate-500">suggested {taxonomyLabel(r.suggested_taxonomy)}</div>
        ) : null}
      </td>
      <td className="px-3 py-2.5 tabular-nums text-slate-800">{r.occurrence_count}</td>
      <td className="px-3 py-2.5">
        <span className="flex flex-wrap gap-1">
          {r.versions.map((v) => (
            <Badge key={v}>v{v}</Badge>
          ))}
        </span>
      </td>
      <td className="px-3 py-2.5 text-slate-700">{r.component ? <code>{r.component}</code> : "—"}</td>
      <td className="px-3 py-2.5">
        <Link
          href={`/traces/${r.representative_trace_id}`}
          className="font-mono text-xs text-indigo-700 hover:underline"
          aria-label={`Representative trace of ${r.title}`}
        >
          {r.representative_trace_id.slice(0, 12)}
        </Link>
      </td>
      <td className="px-3 py-2.5 text-slate-700" title={r.first_seen}>
        {formatRelative(r.first_seen, now)}
      </td>
      <td className="px-3 py-2.5 text-slate-700" title={r.last_seen}>
        {formatRelative(r.last_seen, now)}
      </td>
    </tr>
  );
}

const COLUMNS = [
  "Failure",
  "Status",
  "Severity",
  "Label",
  "Failures",
  "Versions",
  "Component",
  "Representative trace",
  "First seen",
  "Last seen",
];

export function RegressionList() {
  const { params, update, replace } = useUrlQuery();
  const view = statusView(params.get("view"));
  const severity = params.get("severity") ?? "";
  const taxonomy = params.get("taxonomy") ?? "";
  const agent = params.get("agent") ?? "";
  const merged = params.get("merged") === "1";
  const now = new Date();

  const projects = useQuery({
    queryKey: ["projects"],
    queryFn: ({ signal }) => api<{ items: Project[] }>("/projects", { signal }),
    staleTime: 60_000,
  });
  const projectList = projects.data?.items ?? [];
  const projectId = params.get("project_id") || projectList[0]?.id || "";
  const agents = useQuery({
    queryKey: ["project-agents", projectId],
    queryFn: ({ signal }) => api<{ items: Agent[] }>(`/projects/${projectId}/agents`, { signal }),
    enabled: Boolean(projectId),
  });
  const regressions = useInfiniteQuery({
    queryKey: ["regressions", projectId, view.value, severity, taxonomy, agent, merged],
    queryFn: ({ pageParam, signal }) =>
      api<RegressionPage>(
        withQuery(
          "/regressions/candidates",
          queryOf<"listRegressions">({
            project_id: projectId,
            status: view.statuses.join(","),
            severity,
            taxonomy: FAILURE_LABELS.find((l) => l === taxonomy),
            agent,
            include_merged: merged ? "true" : undefined,
            limit: PAGE_SIZE,
            cursor: pageParam,
          }),
        ),
        { signal },
      ),
    initialPageParam: "",
    getNextPageParam: (last) => last.next_cursor || undefined,
    enabled: Boolean(projectId),
  });
  const items = regressions.data?.pages.flatMap((p) => p.items) ?? [];
  const filtered = view.value !== "open" || severity || taxonomy || agent || merged;

  return (
    <div className="space-y-4">
      <PageHeader
        title="Regressions"
        description="Production failures the miner grouped: decide which become regression tests every future release runs."
        actions={
          <Button
            size="sm"
            onClick={() => void regressions.refetch()}
            disabled={regressions.isFetching || !projectId}
          >
            <RefreshCw
              className={`h-4 w-4 ${regressions.isFetching ? "animate-spin motion-reduce:animate-none" : ""}`}
              aria-hidden="true"
            />
            Refresh
          </Button>
        }
      />
      <Card className="flex flex-wrap items-end gap-3 p-3">
        {projectList.length > 1 ? (
          <div className="w-48">
            <Label htmlFor="reg-project">Project</Label>
            <Select
              id="reg-project"
              value={projectId}
              onChange={(e) => update({ project_id: e.target.value, agent: "" })}
            >
              {projectList.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </Select>
          </div>
        ) : null}
        <div className="w-48">
          <Label htmlFor="reg-view">Show</Label>
          <Select
            id="reg-view"
            value={view.value}
            onChange={(e) => update({ view: e.target.value === "open" ? "" : e.target.value })}
          >
            {STATUS_VIEWS.map((v) => (
              <option key={v.value} value={v.value}>
                {v.label}
              </option>
            ))}
          </Select>
        </div>
        <div className="w-36">
          <Label htmlFor="reg-severity">Severity</Label>
          <Select id="reg-severity" value={severity} onChange={(e) => update({ severity: e.target.value })}>
            <option value="">Any</option>
            {SEVERITIES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </Select>
        </div>
        <div className="w-56">
          <Label htmlFor="reg-taxonomy">Label</Label>
          <Select id="reg-taxonomy" value={taxonomy} onChange={(e) => update({ taxonomy: e.target.value })}>
            <option value="">Any</option>
            {FAILURE_LABELS.map((l) => (
              <option key={l} value={l}>
                {taxonomyLabel(l)}
              </option>
            ))}
          </Select>
        </div>
        <div className="w-56">
          <Label htmlFor="reg-agent">Agent</Label>
          <Select id="reg-agent" value={agent} onChange={(e) => update({ agent: e.target.value })}>
            <option value="">All agents</option>
            {(agents.data?.items ?? []).map((a) => (
              <option key={a.id} value={a.name}>
                {a.name}
              </option>
            ))}
          </Select>
        </div>
        <label className="flex items-center gap-2 pb-2 text-sm text-slate-700">
          <input
            type="checkbox"
            checked={merged}
            onChange={(e) => update({ merged: e.target.checked ? "1" : "" })}
          />
          Merged groups
        </label>
        {filtered ? (
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              const next = new URLSearchParams();
              if (params.get("project_id")) next.set("project_id", projectId);
              replace(next);
            }}
          >
            Clear filters
          </Button>
        ) : null}
      </Card>
      <Card>
        {projects.isPending || (Boolean(projectId) && regressions.isPending) ? (
          <div className="space-y-2 p-4" aria-busy="true" aria-label="Loading regressions">
            {Array.from({ length: 4 }, (_, i) => (
              <Skeleton key={i} className="h-12 w-full" />
            ))}
          </div>
        ) : (projects.error ?? regressions.error) ? (
          <div className="p-4">
            <ErrorState error={projects.error ?? regressions.error} />
          </div>
        ) : items.length === 0 ? (
          <EmptyState title={filtered ? "No regressions match" : "No regressions need a decision"}>
            The miner groups failed production traces here: an error outcome, a verified outcome that
            contradicts the agent, a duplicate side effect, a person&apos;s flag. Load the demo&apos;s
            incident with <code className="rounded bg-slate-100 px-1">make seed</code>.
          </EmptyState>
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[76rem] text-left text-sm">
                <caption className="sr-only">Regressions, most recently seen first</caption>
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
                  {items.map((r) => (
                    <RegressionRow key={r.id} r={r} now={now} />
                  ))}
                </tbody>
              </table>
            </div>
            <div className="flex items-center justify-between border-t border-slate-100 px-3 py-2 text-xs text-slate-500">
              <span aria-live="polite">
                Showing {items.length} {items.length === 1 ? "regression" : "regressions"}
              </span>
              {regressions.hasNextPage ? (
                <Button
                  size="sm"
                  onClick={() => void regressions.fetchNextPage()}
                  disabled={regressions.isFetchingNextPage}
                >
                  {regressions.isFetchingNextPage ? "Loading…" : "Load more"}
                </Button>
              ) : null}
            </div>
          </>
        )}
      </Card>
    </div>
  );
}
