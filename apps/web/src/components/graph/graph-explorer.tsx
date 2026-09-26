"use client";

import { useQuery } from "@tanstack/react-query";
import { RefreshCw, Search } from "lucide-react";
import dynamic from "next/dynamic";
import Link from "next/link";
import { type FormEvent, useEffect, useMemo, useState } from "react";
import { PageHeader } from "@/components/common/page-header";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Label, Select } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { api, withQuery } from "@/lib/api";
import type { ChangeImpact } from "@/lib/api/control-plane";
import { type ComponentPage, type GraphView, queryOf } from "@/lib/api/graph";
import { kindLabel, relationPhrase } from "@/lib/changes";
import { UUID } from "@/lib/ids";
import {
  type EvidenceFilter,
  KIND_ORDER,
  type RiskFilter,
  confidencePercent,
  edgeEvidence,
  filterView,
  refKey,
} from "@/lib/graph";
import type { Project } from "@/lib/types";
import { useUrlQuery } from "@/lib/use-url-query";
import { ComponentPanel } from "./component-panel";

const GraphCanvas = dynamic(() => import("./graph-canvas"), {
  ssr: false,
  loading: () => <Skeleton className="h-full w-full" />,
});

const LIMIT = 150;
const DEPTHS = [1, 2, 3, 4] as const;
const EVIDENCE: { value: EvidenceFilter; label: string }[] = [
  { value: "all", label: "All evidence" },
  { value: "observed", label: "Observed in traffic" },
  { value: "declared", label: "Declared (manifests, imports, mappings)" },
];
const RISK: { value: RiskFilter; label: string }[] = [
  { value: "all", label: "Every tool" },
  { value: "writes", label: "Tools that write" },
  { value: "irreversible", label: "Irreversible or admin tools" },
];

function asEvidence(v: string | null): EvidenceFilter {
  return v === "observed" || v === "declared" ? v : "all";
}
function asRisk(v: string | null): RiskFilter {
  return v === "writes" || v === "irreversible" ? v : "all";
}
function asDepth(v: string | null, fallback: number): number {
  const n = Number(v);
  return Number.isInteger(n) && n >= 1 && n <= 4 ? n : fallback;
}

/** The kinds a `kinds` parameter names (unknown names dropped). */
export function parseKinds(v: string | null): string[] {
  const known = new Set<string>(KIND_ORDER);
  return (v ?? "")
    .split(",")
    .map((k) => k.trim().toUpperCase())
    .filter((k, i, all) => known.has(k) && all.indexOf(k) === i);
}

function Legend() {
  return (
    <ul className="flex flex-wrap items-center gap-3 text-xs text-slate-600" aria-label="Legend">
      <li className="flex items-center gap-1">
        <span className="inline-block h-0.5 w-5 bg-emerald-600" aria-hidden="true" /> observed in traffic
      </li>
      <li className="flex items-center gap-1">
        <span className="inline-block h-0.5 w-5 bg-slate-500" aria-hidden="true" /> declared
      </li>
      <li className="flex items-center gap-1">
        <span className="inline-block h-0 w-5 border-t-2 border-dashed border-amber-600" aria-hidden="true" />{" "}
        inferred (not certain)
      </li>
      <li className="flex items-center gap-1">
        <span className="inline-block h-3 w-3 rounded border-2 border-indigo-600" aria-hidden="true" /> focus
      </li>
    </ul>
  );
}

function RelationshipTable({ view }: { view: GraphView }) {
  const byId = new Map(view.nodes.map((n) => [n.id, n]));
  const rows = [...view.edges].sort(
    (a, b) =>
      (byId.get(a.from)?.label ?? "").localeCompare(byId.get(b.from)?.label ?? "") ||
      a.type.localeCompare(b.type) ||
      (byId.get(a.to)?.label ?? "").localeCompare(byId.get(b.to)?.label ?? ""),
  );
  return (
    <details className="rounded-lg border border-slate-200 bg-white shadow-sm">
      <summary className="cursor-pointer px-4 py-3 text-sm font-semibold text-slate-900">
        All {rows.length} relationships shown, as a table
      </summary>
      <div className="relative overflow-x-auto border-t border-slate-100">
        <table className="w-full min-w-[40rem] text-left text-sm" data-testid="relationship-table">
          <caption className="sr-only">Relationships between the shown components</caption>
          <thead className="text-xs uppercase tracking-wide text-slate-500">
            <tr>
              <th scope="col" className="px-3 py-2 font-medium">
                From
              </th>
              <th scope="col" className="px-3 py-2 font-medium">
                Relationship
              </th>
              <th scope="col" className="px-3 py-2 font-medium">
                To
              </th>
              <th scope="col" className="px-3 py-2 font-medium">
                Evidence
              </th>
              <th scope="col" className="px-3 py-2 text-right font-medium">
                Confidence
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {rows.map((e) => {
              const from = byId.get(e.from);
              const to = byId.get(e.to);
              return (
                <tr key={e.id} data-type={e.type}>
                  <td className="px-3 py-1.5">
                    <span className="text-xs text-slate-500">{kindLabel(from?.kind)}</span>{" "}
                    <span className="font-mono">{from?.label}</span>
                  </td>
                  <td className="px-3 py-1.5 text-slate-700">{relationPhrase(e.type)}</td>
                  <td className="px-3 py-1.5">
                    <span className="text-xs text-slate-500">{kindLabel(to?.kind)}</span>{" "}
                    <span className="font-mono">{to?.label}</span>
                  </td>
                  <td className="px-3 py-1.5 text-xs text-slate-600">
                    {edgeEvidence(e)} ({e.sources.map((s) => s.toLowerCase()).join(", ")})
                  </td>
                  <td className="px-3 py-1.5 text-right tabular-nums">{confidencePercent(e.confidence)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </details>
  );
}

function FindComponent({ projectId, onPick }: { projectId: string; onPick: (id: string) => void }) {
  const [text, setText] = useState("");
  const [query, setQuery] = useState("");
  const found = useQuery({
    queryKey: ["graph-components", projectId, query],
    queryFn: ({ signal }) =>
      api<ComponentPage>(
        withQuery(
          "/graph/components",
          queryOf<"listComponents">({ project_id: projectId, q: query, limit: 20 }),
        ),
        { signal },
      ),
    enabled: Boolean(projectId && query),
  });
  function submit(e: FormEvent) {
    e.preventDefault();
    setQuery(text.trim());
  }
  return (
    <div className="min-w-[14rem] flex-1">
      <form onSubmit={submit} role="search" aria-label="Component search">
        <Label htmlFor="graph-find">Find a component</Label>
        <div className="flex gap-1">
          <Input
            id="graph-find"
            value={text}
            maxLength={200}
            placeholder="refund_payment, payments-api…"
            onChange={(e) => setText(e.target.value)}
          />
          <Button type="submit" size="icon" aria-label="Search the graph">
            <Search className="h-4 w-4" aria-hidden="true" />
          </Button>
        </div>
      </form>
      {query ? (
        found.isPending ? (
          <p className="mt-1 text-xs text-slate-500">Searching…</p>
        ) : found.isError ? (
          <ErrorState error={found.error} className="mt-1" />
        ) : found.data.items.length === 0 ? (
          <p className="mt-1 text-xs text-slate-500">No component matches “{query}”.</p>
        ) : (
          <ul
            className="mt-1 max-h-48 overflow-y-auto rounded border border-slate-200 text-sm"
            aria-label="Matches"
          >
            {found.data.items.map((c) => (
              <li key={c.id}>
                <button
                  type="button"
                  className="flex w-full items-center gap-2 px-2 py-1 text-left hover:bg-slate-50"
                  onClick={() => {
                    onPick(c.id);
                    setQuery("");
                  }}
                >
                  <span className="text-xs text-slate-500">{kindLabel(c.kind)}</span>
                  <span className="truncate font-mono">{c.label}</span>
                </button>
              </li>
            ))}
          </ul>
        )
      ) : null}
    </div>
  );
}

export function GraphExplorer() {
  const { params, update } = useUrlQuery();
  const changeSetId = UUID.test(params.get("change_set") ?? "")
    ? params.get("change_set")!.toLowerCase()
    : "";
  const focusParam = UUID.test(params.get("focus") ?? "") ? params.get("focus")!.toLowerCase() : "";
  const kindParam = params.get("kind") ?? "";
  const keyParam = params.get("key") ?? "";
  const depth = asDepth(params.get("depth"), changeSetId ? 4 : 2);
  const kinds = parseKinds(params.get("kinds"));
  const evidence = asEvidence(params.get("evidence"));
  const risk = asRisk(params.get("risk"));
  const blastOnly = Boolean(changeSetId) && params.get("only") !== "all";
  const selectedParam = UUID.test(params.get("selected") ?? "") ? params.get("selected")!.toLowerCase() : "";

  const projects = useQuery({
    queryKey: ["projects"],
    queryFn: ({ signal }) => api<{ items: Project[] }>("/projects", { signal }),
    staleTime: 60_000,
  });
  const projectList = projects.data?.items ?? [];
  const projectId = params.get("project_id") || projectList[0]?.id || "";

  const impact = useQuery({
    queryKey: ["change-set-impact", changeSetId],
    queryFn: ({ signal }) => api<ChangeImpact>(`/change-sets/${changeSetId}/impact`, { signal }),
    enabled: Boolean(changeSetId),
    staleTime: 30_000,
  });
  const impactMap = useMemo(() => {
    const affected = impact.data?.graph?.affected ?? [];
    return affected.length ? new Map(affected.map((a) => [refKey(a.component), a.severity])) : null;
  }, [impact.data]);

  // A component named by kind and key (links from other pages), or the
  // change set's first changed component: found by its key, then the URL
  // carries its id.
  const seed = impact.data?.graph?.seeds[0]?.component;
  const wanted =
    !focusParam && kindParam && keyParam
      ? { kind: kindParam, key: keyParam }
      : !focusParam && seed
        ? { kind: seed.kind, key: seed.key }
        : null;
  const resolved = useQuery({
    queryKey: ["graph-resolve", projectId, wanted?.kind, wanted?.key],
    queryFn: async ({ signal }) => {
      const page = await api<ComponentPage>(
        withQuery(
          "/graph/components",
          queryOf<"listComponents">({
            project_id: projectId,
            kind: wanted!.kind as ComponentPage["items"][number]["kind"],
            q: wanted!.key,
            limit: 200,
          }),
        ),
        { signal },
      );
      return page.items.find((c) => c.key === wanted!.key) ?? null;
    },
    enabled: Boolean(projectId && wanted),
  });
  useEffect(() => {
    if (resolved.data) update({ focus: resolved.data.id, kind: "", key: "", selected: resolved.data.id });
  }, [resolved.data, update]);

  const waitingForFocus = Boolean(wanted) && (resolved.isPending || Boolean(resolved.data));
  const graph = useQuery({
    queryKey: ["graph", projectId, focusParam, depth, kinds.join(",")],
    queryFn: ({ signal }) =>
      api<GraphView>(
        withQuery(
          "/graph",
          queryOf<"getGraph">({
            project_id: projectId,
            focus: focusParam || undefined,
            depth,
            limit: LIMIT,
            kinds: kinds.length ? kinds.join(",") : undefined,
          }),
        ),
        { signal },
      ),
    enabled: Boolean(projectId) && !waitingForFocus && (!changeSetId || !impact.isPending),
  });

  const only = useMemo(
    () => (blastOnly && impactMap ? new Set(impactMap.keys()) : null),
    [blastOnly, impactMap],
  );
  const view = useMemo(
    () => (graph.data ? filterView(graph.data, { evidence, risk, only }) : null),
    [graph.data, evidence, risk, only],
  );
  const focusIds = new Set(view?.focus.map((f) => f.id) ?? []);
  const selected =
    selectedParam && view?.nodes.some((n) => n.id === selectedParam)
      ? selectedParam
      : (view?.focus[0]?.id ?? "");
  const totals = graph.data?.totals;
  const totalComponents = totals ? Object.values(totals.components).reduce((a, b) => a + b, 0) : 0;
  const knownKinds = totals
    ? KIND_ORDER.filter((k) => (totals.components[k] ?? 0) > 0 || kinds.includes(k))
    : [];

  const toggleKind = (kind: string, on: boolean) => {
    const next = on ? [...kinds, kind] : kinds.filter((k) => k !== kind);
    update({ kinds: next.join(",") });
  };

  return (
    <div className="space-y-4">
      <PageHeader
        title="Dependency graph"
        description="What each agent version uses and what those tools reach, with the evidence for every relationship. The graph opens on the latest version of each agent; center it on any component."
        actions={
          <Button size="sm" onClick={() => void graph.refetch()} disabled={graph.isFetching || !projectId}>
            <RefreshCw
              className={`h-4 w-4 ${graph.isFetching ? "animate-spin motion-reduce:animate-none" : ""}`}
              aria-hidden="true"
            />
            Refresh
          </Button>
        }
      />
      {changeSetId ? (
        <div
          className="flex flex-wrap items-center gap-2 rounded-md border border-indigo-200 bg-indigo-50 p-3 text-sm text-indigo-900"
          data-testid="blast-radius-banner"
        >
          {impact.isError ? (
            <span>The change set&apos;s impact could not be computed.</span>
          ) : impact.data ? (
            <span>
              Blast radius of{" "}
              <Link href={`/changes/${changeSetId}`} className="font-medium underline">
                {impact.data.agent} v{impact.data.base_version} → v{impact.data.candidate_version}
              </Link>
              : {impact.data.graph?.affected_count ?? 0} components, outlined by severity.
            </span>
          ) : (
            <span>Loading the change set&apos;s blast radius…</span>
          )}
          <label className="ml-auto flex items-center gap-2 text-xs">
            <input
              type="checkbox"
              className="h-4 w-4 accent-indigo-600"
              checked={blastOnly}
              onChange={(e) => update({ only: e.target.checked ? "" : "all" })}
            />
            Only the blast radius
          </label>
        </div>
      ) : null}
      <Card className="flex flex-wrap items-end gap-3 p-3">
        {projectList.length > 1 ? (
          <div className="w-44">
            <Label htmlFor="graph-project">Project</Label>
            <Select
              id="graph-project"
              value={projectId}
              onChange={(e) =>
                update({ project_id: e.target.value, focus: "", selected: "", change_set: "" })
              }
            >
              {projectList.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </Select>
          </div>
        ) : null}
        {projectId ? (
          <FindComponent projectId={projectId} onPick={(id) => update({ focus: id, selected: id })} />
        ) : null}
        <div className="w-28">
          <Label htmlFor="graph-depth">Depth</Label>
          <Select id="graph-depth" value={String(depth)} onChange={(e) => update({ depth: e.target.value })}>
            {DEPTHS.map((d) => (
              <option key={d} value={d}>
                {d} {d === 1 ? "step" : "steps"}
              </option>
            ))}
          </Select>
        </div>
        <div className="w-56">
          <Label htmlFor="graph-evidence">Evidence</Label>
          <Select
            id="graph-evidence"
            value={evidence}
            onChange={(e) => update({ evidence: e.target.value === "all" ? "" : e.target.value })}
          >
            {EVIDENCE.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </Select>
        </div>
        <div className="w-52">
          <Label htmlFor="graph-risk">Risk tier</Label>
          <Select
            id="graph-risk"
            value={risk}
            onChange={(e) => update({ risk: e.target.value === "all" ? "" : e.target.value })}
          >
            {RISK.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </Select>
        </div>
        {focusParam ? (
          <Button
            size="sm"
            variant="ghost"
            onClick={() => update({ focus: "", selected: "", change_set: "" })}
          >
            Back to the agents
          </Button>
        ) : null}
        {knownKinds.length ? (
          <fieldset className="w-full">
            <legend className="text-xs font-medium text-slate-700">
              Kinds {kinds.length ? `(${kinds.length} shown)` : "(all)"}
            </legend>
            <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1">
              {knownKinds.map((k) => (
                <label key={k} className="flex items-center gap-1 text-xs text-slate-700">
                  <input
                    type="checkbox"
                    className="h-3.5 w-3.5 accent-indigo-600"
                    checked={kinds.includes(k)}
                    onChange={(e) => toggleKind(k, e.target.checked)}
                  />
                  {kindLabel(k)} <span className="text-slate-500">{totals?.components[k] ?? 0}</span>
                </label>
              ))}
            </div>
          </fieldset>
        ) : null}
      </Card>
      {projects.error ? <ErrorState error={projects.error} /> : null}
      {wanted && resolved.isSuccess && !resolved.data ? (
        <EmptyState title="The graph does not know this component">
          {kindLabel(wanted.kind)} <span className="font-mono">{wanted.key}</span> has not been seen in this
          project&apos;s graph. Registering an agent version or importing a tool catalog adds it.
        </EmptyState>
      ) : graph.isError ? (
        <ErrorState error={graph.error} />
      ) : !view ? (
        <Skeleton className="h-[36rem] w-full" />
      ) : view.nodes.length === 0 ? (
        <EmptyState title="The graph is empty">
          Register an agent version (its manifest names its tools) or run{" "}
          <code className="rounded bg-slate-100 px-1">make seed</code> to load the demo workspace.
        </EmptyState>
      ) : (
        <>
          <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-slate-600">
            <p aria-live="polite" data-testid="graph-summary">
              Showing {view.nodes.length} of the project&apos;s {totalComponents} components and{" "}
              {view.edges.length} of {totals?.edges ?? 0} relationships, {depth}{" "}
              {depth === 1 ? "step" : "steps"} from{" "}
              {view.focus.length === 1 ? (
                <span className="font-mono">{view.focus[0]!.label}</span>
              ) : (
                `the latest version of ${view.focus.length} agents`
              )}
              .
            </p>
            <Legend />
          </div>
          {graph.data?.truncated ? (
            <p className="text-xs text-amber-800">
              More components are within reach than the {LIMIT} shown, closest first. Narrow the kinds or the
              depth.
            </p>
          ) : null}
          <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
            <div
              className="h-[36rem] overflow-hidden rounded-lg border border-slate-200 bg-white xl:col-span-2"
              data-testid="graph-canvas"
              role="region"
              aria-label="Dependency graph"
            >
              <GraphCanvas
                view={view}
                selectedId={selected || null}
                onSelect={(id) => update({ selected: id })}
                impact={impactMap}
              />
            </div>
            <div>
              {selected ? (
                <ComponentPanel
                  componentId={selected}
                  projectId={projectId}
                  isFocus={focusIds.has(selected)}
                  onSelect={(id) =>
                    view.nodes.some((n) => n.id === id)
                      ? update({ selected: id })
                      : update({ focus: id, selected: id })
                  }
                  onFocus={(id) => update({ focus: id, selected: id })}
                />
              ) : (
                <Card>
                  <CardHeader>
                    <CardTitle>Component</CardTitle>
                  </CardHeader>
                  <CardContent className="text-sm text-slate-600">
                    Select a component to see its relationships.
                  </CardContent>
                </Card>
              )}
            </div>
          </div>
          <RelationshipTable view={view} />
          {kinds.length ? (
            <p className="text-xs text-slate-500">
              <Badge>kinds</Badge> Other kinds are neither shown nor walked through.
            </p>
          ) : null}
        </>
      )}
    </div>
  );
}
