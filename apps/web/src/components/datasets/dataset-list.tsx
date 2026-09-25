"use client";

import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";
import { PageHeader } from "@/components/common/page-header";
import { useCan, useMe } from "@/components/shell/me-context";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input, Label, Select } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { api, withQuery } from "@/lib/api";
import { type DatasetListItem, type DatasetPage, queryOf } from "@/lib/api/evaluation";
import { formatRelative } from "@/lib/format";
import { actorLabel } from "@/lib/simulations";
import type { Project } from "@/lib/types";
import { useUrlQuery } from "@/lib/use-url-query";

const PAGE_SIZE = 50;

function DatasetRow({ dataset, meUserId }: { dataset: DatasetListItem; meUserId?: string }) {
  return (
    <tr className="align-top" data-testid="dataset-row" data-dataset={dataset.name}>
      <td className="px-3 py-2.5">
        <Link href={`/datasets/${dataset.id}`} className="font-medium text-indigo-700 hover:underline">
          {dataset.name}
        </Link>
        {dataset.archived ? (
          <Badge tone="neutral" className="ml-2">
            archived
          </Badge>
        ) : null}
        {dataset.description ? (
          <p className="mt-0.5 max-w-xl text-xs text-slate-600">{dataset.description}</p>
        ) : null}
        {dataset.tags.length ? (
          <div className="mt-1 flex flex-wrap gap-1">
            {dataset.tags.map((t) => (
              <Badge key={t} tone="neutral">
                {t}
              </Badge>
            ))}
          </div>
        ) : null}
      </td>
      <td className="px-3 py-2.5 text-right tabular-nums">{dataset.case_count}</td>
      <td className="px-3 py-2.5 text-right tabular-nums">v{dataset.latest_version}</td>
      <td className="px-3 py-2.5 text-slate-700">{actorLabel(dataset.created_by, meUserId)}</td>
      <td className="px-3 py-2.5 text-slate-700" title={dataset.updated_at}>
        {formatRelative(dataset.updated_at)}
      </td>
    </tr>
  );
}

export function DatasetList() {
  const { params, update, replace } = useUrlQuery();
  const projectId = params.get("project_id") ?? "";
  const archived = params.get("archived") === "1";
  const q = params.get("q") ?? "";
  const [text, setText] = useState(q);
  const canWrite = useCan("scenario.write");
  const me = useMe();

  // The search box moves the URL once typing pauses.
  useEffect(() => {
    if (text.trim() === q) return;
    const t = setTimeout(() => update({ q: text.trim() }), 300);
    return () => clearTimeout(t);
  }, [text, q, update]);

  const projects = useQuery({
    queryKey: ["projects"],
    queryFn: ({ signal }) => api<{ items: Project[] }>("/projects", { signal }),
    staleTime: 60_000,
  });
  const datasets = useInfiniteQuery({
    queryKey: ["datasets", projectId, q, archived],
    queryFn: ({ pageParam, signal }) =>
      api<DatasetPage>(
        withQuery(
          "/datasets",
          queryOf<"listDatasets">({
            project_id: projectId,
            q,
            include_archived: archived || undefined,
            limit: PAGE_SIZE,
            cursor: pageParam,
          }),
        ),
        { signal },
      ),
    initialPageParam: "",
    getNextPageParam: (last) => last.next_cursor || undefined,
  });
  const items = datasets.data?.pages.flatMap((p) => p.items) ?? [];
  const filtered = Boolean(projectId || q || archived);
  const projectList = projects.data?.items ?? [];

  return (
    <div className="space-y-4">
      <PageHeader
        title="Datasets"
        description="Named, versioned sets of scenarios. An evaluation pins the dataset version it ran, so a result can always be traced back to its cases."
        actions={
          canWrite ? (
            <Link
              href={projectId ? `/datasets/new?project_id=${projectId}` : "/datasets/new"}
              className={buttonVariants({ variant: "primary", size: "sm" })}
            >
              <Plus className="h-4 w-4" aria-hidden="true" />
              New dataset
            </Link>
          ) : null
        }
      />
      <Card className="flex flex-wrap items-end gap-3 p-3">
        {projectList.length > 1 ? (
          <div className="w-48">
            <Label htmlFor="ds-project">Project</Label>
            <Select
              id="ds-project"
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
        <div className="w-64">
          <Label htmlFor="ds-search">Search</Label>
          <Input
            id="ds-search"
            type="search"
            placeholder="name or description"
            value={text}
            onChange={(e) => setText(e.target.value)}
          />
        </div>
        <label className="flex items-center gap-2 pb-2 text-sm text-slate-700">
          <input
            type="checkbox"
            className="h-4 w-4 accent-indigo-600"
            checked={archived}
            onChange={(e) => update({ archived: e.target.checked ? "1" : "" })}
          />
          Include archived
        </label>
        {filtered ? (
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              setText("");
              replace(new URLSearchParams());
            }}
          >
            Clear filters
          </Button>
        ) : null}
      </Card>
      <Card>
        {datasets.isPending ? (
          <div className="space-y-2 p-4" aria-busy="true" aria-label="Loading datasets">
            {Array.from({ length: 3 }, (_, i) => (
              <Skeleton key={i} className="h-12 w-full" />
            ))}
          </div>
        ) : datasets.isError ? (
          <div className="p-4">
            <ErrorState error={datasets.error} />
          </div>
        ) : items.length === 0 ? (
          <EmptyState title={filtered ? "No dataset matches" : "No datasets yet"}>
            {filtered ? (
              "Clear the filters to see every dataset."
            ) : (
              <>
                Group the scenarios a release must pass
                {canWrite ? (
                  <>
                    {" "}
                    with{" "}
                    <Link href="/datasets/new" className="text-indigo-700 underline">
                      New dataset
                    </Link>
                  </>
                ) : null}
                .
              </>
            )}
          </EmptyState>
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[48rem] text-left text-sm">
                <caption className="sr-only">Datasets by name</caption>
                <thead className="border-b border-slate-100 text-xs uppercase tracking-wide text-slate-500">
                  <tr>
                    <th scope="col" className="px-3 py-2 font-medium">
                      Dataset
                    </th>
                    <th scope="col" className="px-3 py-2 text-right font-medium">
                      Cases
                    </th>
                    <th scope="col" className="px-3 py-2 text-right font-medium">
                      Version
                    </th>
                    <th scope="col" className="px-3 py-2 font-medium">
                      Created by
                    </th>
                    <th scope="col" className="px-3 py-2 font-medium">
                      Changed
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {items.map((d) => (
                    <DatasetRow key={d.id} dataset={d} meUserId={me?.user?.id} />
                  ))}
                </tbody>
              </table>
            </div>
            {datasets.hasNextPage ? (
              <div className="border-t border-slate-100 px-3 py-2 text-right">
                <Button
                  size="sm"
                  onClick={() => void datasets.fetchNextPage()}
                  disabled={datasets.isFetchingNextPage}
                >
                  {datasets.isFetchingNextPage ? "Loading…" : "Load more"}
                </Button>
              </div>
            ) : null}
          </>
        )}
      </Card>
    </div>
  );
}
