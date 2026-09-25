"use client";

import { useInfiniteQuery } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";
import { PageHeader } from "@/components/common/page-header";
import { useCan } from "@/components/shell/me-context";
import { SeverityBadge } from "@/components/simulations/badges";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input, Label, Select } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { api, withQuery } from "@/lib/api";
import { queryOf } from "@/lib/api/simulation";
import { formatRelative } from "@/lib/format";
import { SEVERITIES, asSeverity } from "@/lib/simulations";
import type { Scenario, ScenarioPage } from "@/lib/types";
import { useUrlQuery } from "@/lib/use-url-query";

const PAGE_SIZE = 50;

function ScenarioRow({ s, now }: { s: Scenario; now: Date }) {
  return (
    <tr className="align-top" data-testid="scenario-row" data-scenario={s.name}>
      <td className="max-w-md px-3 py-2.5">
        <Link href={`/scenarios/${s.id}`} className="font-medium text-indigo-700 hover:underline">
          {s.name}
        </Link>
        {s.archived ? (
          <Badge className="ml-2" tone="neutral">
            archived
          </Badge>
        ) : null}
        {s.description ? <p className="mt-0.5 line-clamp-2 text-xs text-slate-600">{s.description}</p> : null}
      </td>
      <td className="px-3 py-2.5">
        <SeverityBadge severity={s.severity} />
      </td>
      <td className="whitespace-nowrap px-3 py-2.5 text-slate-700">
        {s.agent ?? "—"}
        {s.twin ? <div className="text-xs text-slate-500">twin {s.twin}</div> : null}
      </td>
      <td className="px-3 py-2.5">
        <span className="flex max-w-56 flex-wrap gap-1">
          {s.tags.map((t) => (
            <Badge key={t}>{t}</Badge>
          ))}
        </span>
      </td>
      <td className="px-3 py-2.5 text-right tabular-nums text-slate-700">{s.expectation_count ?? "—"}</td>
      <td className="px-3 py-2.5 text-right tabular-nums text-slate-700">{s.fault_count ?? "—"}</td>
      <td className="px-3 py-2.5 text-right tabular-nums text-slate-700">v{s.latest_version}</td>
      <td className="whitespace-nowrap px-3 py-2.5 text-slate-600" title={s.updated_at}>
        {formatRelative(s.updated_at, now)}
      </td>
    </tr>
  );
}

export function ScenarioList() {
  const { params, update: setParams, replace } = useUrlQuery();
  const canWrite = useCan("scenario.write");
  const severity = params.get("severity") ?? "";
  const tag = params.get("tag") ?? "";
  const q = params.get("q") ?? "";
  const archived = params.get("include_archived") === "1";
  const [search, setSearch] = useState(q);
  const [tagInput, setTagInput] = useState(tag);
  const [now] = useState(() => new Date());
  // The URL can change without typing (back/forward): follow it.
  const [seen, setSeen] = useState({ q, tag });
  if (seen.q !== q || seen.tag !== tag) {
    setSeen({ q, tag });
    // Only when the URL disagrees with what is typed (keeps a trailing space).
    if (search.trim() !== q) setSearch(q);
    if (tagInput.trim() !== tag) setTagInput(tag);
  }

  // Typing searches after a short pause instead of on every key.
  useEffect(() => {
    const value = search.trim();
    if (value === q) return;
    const t = setTimeout(() => setParams({ q: value }), 300);
    return () => clearTimeout(t);
  }, [search, q, setParams]);

  const scenarios = useInfiniteQuery({
    queryKey: ["scenarios-list", severity, tag, q, archived],
    queryFn: ({ pageParam, signal }) => {
      const params = queryOf<"listScenarios">({
        limit: PAGE_SIZE,
        severity: asSeverity(severity),
        tag,
        q,
        include_archived: archived ? "1" : undefined,
        cursor: pageParam,
      });
      return api<ScenarioPage>(withQuery("/scenarios", params), { signal });
    },
    initialPageParam: "",
    getNextPageParam: (last) => last.next_cursor || undefined,
  });
  const items = scenarios.data?.pages.flatMap((p) => p.items) ?? [];
  const filtered = Boolean(severity || tag || q || archived);

  return (
    <div className="space-y-4">
      <PageHeader
        title="Scenarios"
        description="Versioned tests of agent behavior: an input, a tool twin with optional injected faults, and expectations on the trajectory and the final state."
        actions={
          canWrite ? (
            <Link href="/scenarios/new" className={buttonVariants({ variant: "primary", size: "sm" })}>
              <Plus className="h-4 w-4" aria-hidden="true" />
              New scenario
            </Link>
          ) : null
        }
      />
      <Card className="flex flex-wrap items-end gap-3 p-3">
        <div className="w-64">
          <Label htmlFor="sc-search">Search</Label>
          <Input
            id="sc-search"
            type="search"
            placeholder="Name or description"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
        <div className="w-40">
          <Label htmlFor="sc-severity">Severity</Label>
          <Select id="sc-severity" value={severity} onChange={(e) => setParams({ severity: e.target.value })}>
            <option value="">Any severity</option>
            {SEVERITIES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </Select>
        </div>
        <div className="w-40">
          <Label htmlFor="sc-tag">Tag</Label>
          <Input
            id="sc-tag"
            placeholder="e.g. refunds"
            value={tagInput}
            onChange={(e) => setTagInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") setParams({ tag: tagInput.trim() });
            }}
            onBlur={() => {
              if (tagInput.trim() !== tag) setParams({ tag: tagInput.trim() });
            }}
          />
        </div>
        <label className="flex h-9 items-center gap-2 text-sm text-slate-700">
          <input
            type="checkbox"
            className="h-4 w-4 accent-indigo-600"
            checked={archived}
            onChange={(e) => setParams({ include_archived: e.target.checked ? "1" : "" })}
          />
          Show archived
        </label>
        {filtered ? (
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              setSearch("");
              setTagInput("");
              replace(new URLSearchParams());
            }}
          >
            Clear filters
          </Button>
        ) : null}
      </Card>
      <Card>
        {scenarios.isPending ? (
          <div className="space-y-2 p-4" aria-busy="true" aria-label="Loading scenarios">
            {Array.from({ length: 6 }, (_, i) => (
              <Skeleton key={i} className="h-10 w-full" />
            ))}
          </div>
        ) : scenarios.isError ? (
          <div className="p-4">
            <ErrorState error={scenarios.error} />
          </div>
        ) : items.length === 0 ? (
          <EmptyState title={filtered ? "No scenarios match these filters" : "No scenarios yet"}>
            {filtered ? (
              "Clear the filters to see every scenario."
            ) : (
              <>
                Describe a conversation, the tool state it starts from and what must be true at the end
                {canWrite ? (
                  <>
                    {" "}
                    —{" "}
                    <Link href="/scenarios/new" className="text-indigo-700 underline">
                      write the first scenario
                    </Link>
                  </>
                ) : null}
                , or load the demo suite with <code className="rounded bg-slate-100 px-1">make seed</code>.
              </>
            )}
          </EmptyState>
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[56rem] text-left text-sm">
                <caption className="sr-only">Scenarios by name</caption>
                <thead className="border-b border-slate-100 text-xs uppercase tracking-wide text-slate-500">
                  <tr>
                    <th scope="col" className="px-3 py-2 font-medium">
                      Scenario
                    </th>
                    <th scope="col" className="px-3 py-2 font-medium">
                      Severity
                    </th>
                    <th scope="col" className="px-3 py-2 font-medium">
                      Agent
                    </th>
                    <th scope="col" className="px-3 py-2 font-medium">
                      Tags
                    </th>
                    <th scope="col" className="px-3 py-2 text-right font-medium">
                      Expectations
                    </th>
                    <th scope="col" className="px-3 py-2 text-right font-medium">
                      Faults
                    </th>
                    <th scope="col" className="px-3 py-2 text-right font-medium">
                      Version
                    </th>
                    <th scope="col" className="px-3 py-2 font-medium">
                      Updated
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {items.map((s) => (
                    <ScenarioRow key={s.id} s={s} now={now} />
                  ))}
                </tbody>
              </table>
            </div>
            <div className="flex items-center justify-between border-t border-slate-100 px-3 py-2 text-xs text-slate-500">
              <span aria-live="polite">
                Showing {items.length} {items.length === 1 ? "scenario" : "scenarios"}
              </span>
              {scenarios.hasNextPage ? (
                <Button
                  size="sm"
                  onClick={() => void scenarios.fetchNextPage()}
                  disabled={scenarios.isFetchingNextPage}
                >
                  {scenarios.isFetchingNextPage ? "Loading…" : "Load more"}
                </Button>
              ) : null}
            </div>
          </>
        )}
      </Card>
    </div>
  );
}
