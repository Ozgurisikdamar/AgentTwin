"use client";

import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { Pause, Play, RefreshCw } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { PageHeader } from "@/components/common/page-header";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { api, withQuery } from "@/lib/api";
import { type TraceFilters, activeFilterCount, filtersToParams, parseFilters } from "@/lib/trace-filters";
import type { FacetsResponse, Project, TracePage } from "@/lib/types";
import { useUrlQuery } from "@/lib/use-url-query";
import { TraceFiltersBar } from "./trace-filters-bar";
import { TraceTable } from "./trace-table";

const PAGE_SIZE = 50;
const LIVE_INTERVAL_MS = 5_000;

function useNow(intervalMs: number): Date {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), intervalMs);
    return () => clearInterval(t);
  }, [intervalMs]);
  return now;
}

export function TraceExplorer() {
  const { params, replace } = useUrlQuery();
  const filters = useMemo(() => parseFilters(params), [params]);
  const filterKey = filtersToParams(filters).toString();
  const [live, setLive] = useState(true);
  const now = useNow(15_000);

  const setFilters = (next: TraceFilters) => replace(filtersToParams(next));

  const projects = useQuery({
    queryKey: ["projects"],
    queryFn: ({ signal }) => api<{ items: Project[] }>("/projects", { signal }),
    staleTime: 60_000,
  });
  const facets = useQuery({
    queryKey: ["trace-facets", filters.project_id ?? ""],
    queryFn: ({ signal }) =>
      api<FacetsResponse>(
        withQuery(
          "/traces/facets",
          new URLSearchParams(filters.project_id ? { project_id: filters.project_id } : {}),
        ),
        { signal },
      ),
    staleTime: 30_000,
  });
  const traces = useInfiniteQuery({
    queryKey: ["traces", filterKey],
    queryFn: ({ pageParam, signal }) => {
      const params = filtersToParams(filters);
      params.set("limit", String(PAGE_SIZE));
      if (pageParam) params.set("cursor", pageParam);
      return api<TracePage>(withQuery("/traces", params), { signal });
    },
    initialPageParam: "",
    getNextPageParam: (last) => last.next_cursor || undefined,
    refetchInterval: live ? LIVE_INTERVAL_MS : false,
  });
  const items = traces.data?.pages.flatMap((p) => p.items) ?? [];
  const filtered = activeFilterCount(filters) > 0;

  return (
    <div className="space-y-4">
      <PageHeader
        title="Traces"
        description="Every agent run with its model calls, tool calls, policy decisions and outcome evidence."
        actions={
          <>
            <Button size="sm" onClick={() => void traces.refetch()} disabled={traces.isFetching}>
              <RefreshCw
                className={`h-4 w-4 ${traces.isFetching ? "animate-spin" : ""}`}
                aria-hidden="true"
              />
              Refresh
            </Button>
            <Button size="sm" onClick={() => setLive((v) => !v)} aria-pressed={live}>
              {live ? (
                <Pause className="h-4 w-4" aria-hidden="true" />
              ) : (
                <Play className="h-4 w-4" aria-hidden="true" />
              )}
              {live ? "Live" : "Paused"}
            </Button>
          </>
        }
      />
      <TraceFiltersBar
        filters={filters}
        onChange={setFilters}
        facets={facets.data?.facets}
        projects={projects.data?.items ?? []}
      />
      <Card>
        {traces.isPending ? (
          <div className="space-y-2 p-4" aria-busy="true" aria-label="Loading traces">
            {Array.from({ length: 6 }, (_, i) => (
              <Skeleton key={i} className="h-10 w-full" />
            ))}
          </div>
        ) : traces.isError ? (
          <div className="p-4">
            <ErrorState error={traces.error} />
          </div>
        ) : items.length === 0 ? (
          <EmptyState title={filtered ? "No traces match these filters" : "No traces yet"}>
            {filtered ? (
              "Widen the filters or clear them to see all traces."
            ) : (
              <>
                Instrument an agent with the AgentTwin SDK, or run the demo:{" "}
                <code className="rounded bg-slate-100 px-1">make demo</code>
              </>
            )}
          </EmptyState>
        ) : (
          <>
            <TraceTable traces={items} now={now} />
            <div className="flex items-center justify-between border-t border-slate-100 px-3 py-2 text-xs text-slate-500">
              <span aria-live="polite">
                Showing {items.length} {items.length === 1 ? "trace" : "traces"}
                {live ? " · live" : ""}
              </span>
              {traces.hasNextPage ? (
                <Button
                  size="sm"
                  onClick={() => void traces.fetchNextPage()}
                  disabled={traces.isFetchingNextPage}
                >
                  {traces.isFetchingNextPage ? "Loading…" : "Load more"}
                </Button>
              ) : null}
            </div>
          </>
        )}
      </Card>
    </div>
  );
}
