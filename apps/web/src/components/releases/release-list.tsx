"use client";

import { useInfiniteQuery, useMutation, useQuery } from "@tanstack/react-query";
import { Plus, RefreshCw, Rocket } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { type FormEvent, useState } from "react";
import { PageHeader } from "@/components/common/page-header";
import { useCan, useMe } from "@/components/shell/me-context";
import { sortVersions } from "@/components/simulations/new-simulation";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Label, Select } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { api, withQuery } from "@/lib/api";
import {
  type BodyOf,
  type CreatedRelease,
  type Release,
  type ReleasePage,
  queryOf,
} from "@/lib/api/control-plane";
import { changeCountsLine } from "@/lib/changes";
import { formatRelative } from "@/lib/format";
import { formatCostDelta, formatLatencyDelta } from "@/lib/releases";
import { actorLabel } from "@/lib/simulations";
import type { Agent, AgentVersion, Project } from "@/lib/types";
import { useActionKey } from "@/lib/use-action-key";
import { useUrlQuery } from "@/lib/use-url-query";
import { Delta, GateOutcomeBadge } from "./gate-badge";

const PAGE_SIZE = 25;

function Dash() {
  return <span className="text-slate-400">—</span>;
}

/** One release as the list shows it (spec §41.2). */
function ReleaseRow({ r, now, meUserId }: { r: Release; now: Date; meUserId?: string }) {
  const s = r.gate?.summary ?? null;
  return (
    <tr className="align-top" data-testid="release-row" data-release-id={r.id}>
      <td className="px-3 py-2.5">
        <Link
          href={`/releases/${r.id}`}
          className="font-medium text-indigo-700 hover:underline"
          aria-label={`Release ${r.agent.name} ${r.baseline.version} to ${r.candidate.version}`}
        >
          {r.agent.name}
        </Link>
        <div className="mt-1">
          <Badge tone="brand">v{r.candidate.version}</Badge>
        </div>
        {r.title ? <div className="mt-1 max-w-[16rem] truncate text-xs text-slate-500">{r.title}</div> : null}
      </td>
      <td className="px-3 py-2.5">
        <Badge>v{r.baseline.version}</Badge>
      </td>
      <td className="px-3 py-2.5 text-slate-800">
        {changeCountsLine(r.changes)}
        {r.changes.breaking > 0 ? (
          <Badge tone="danger" className="ml-2">
            {r.changes.breaking} breaking
          </Badge>
        ) : null}
      </td>
      <td className="px-3 py-2.5">
        <GateOutcomeBadge gate={r.gate} />
      </td>
      <td className="px-3 py-2.5 tabular-nums">
        {s ? (
          <span className={s.new_critical_failures > 0 ? "font-semibold text-rose-700" : "text-slate-700"}>
            {s.new_critical_failures}
          </span>
        ) : (
          <Dash />
        )}
      </td>
      <td className="px-3 py-2.5 tabular-nums text-slate-700">{s ? s.scenarios : <Dash />}</td>
      <td className="px-3 py-2.5">
        <Delta value={s?.cost_delta_usd} text={formatCostDelta(s?.cost_delta_usd)} />
      </td>
      <td className="px-3 py-2.5">
        <Delta value={s?.latency_p95_delta_ms} text={formatLatencyDelta(s?.latency_p95_delta_ms)} />
      </td>
      <td className="px-3 py-2.5 text-slate-700">{actorLabel(r.created_by, meUserId)}</td>
      <td className="px-3 py-2.5 text-slate-700" title={r.created_at}>
        {formatRelative(r.created_at, now)}
      </td>
    </tr>
  );
}

/** Release a candidate version against its baseline: created and evaluated at once. */
function NewReleaseForm({ projectId, onCancel }: { projectId: string; onCancel: () => void }) {
  const router = useRouter();
  const [agentName, setAgentName] = useState("");
  const [baseline, setBaseline] = useState("");
  const [candidate, setCandidate] = useState("");
  const [title, setTitle] = useState("");
  const createKey = useActionKey("release");

  const agents = useQuery({
    queryKey: ["project-agents", projectId],
    queryFn: ({ signal }) => api<{ items: Agent[] }>(`/projects/${projectId}/agents`, { signal }),
    enabled: Boolean(projectId),
  });
  const agentList = agents.data?.items ?? [];
  const agent = agentList.find((a) => a.name === agentName) ?? agentList[0];
  const versions = useQuery({
    queryKey: ["agent-versions", agent?.id],
    queryFn: ({ signal }) => api<{ items: AgentVersion[] }>(`/agents/${agent!.id}/versions`, { signal }),
    enabled: Boolean(agent),
  });
  const versionList = sortVersions(versions.data?.items ?? []);
  // Default: the newest version as the candidate, the one before it as the baseline.
  const chosenCandidate = versionList.some((v) => v.version === candidate)
    ? candidate
    : (versionList[0]?.version ?? "");
  const candidateAt = versionList.findIndex((v) => v.version === chosenCandidate);
  const chosenBaseline = versionList.some((v) => v.version === baseline)
    ? baseline
    : (versionList[candidateAt + 1]?.version ??
      versionList.find((v) => v.version !== chosenCandidate)?.version ??
      "");

  const create = useMutation({
    mutationFn: () =>
      api<CreatedRelease>("/releases", {
        method: "POST",
        idempotencyKey: createKey.key,
        body: {
          project_id: projectId,
          agent: agent!.name,
          baseline_version: chosenBaseline,
          candidate_version: chosenCandidate,
          ...(title.trim() ? { title: title.trim() } : {}),
        } satisfies BodyOf<"createRelease">,
      }),
    onSuccess: (created) => router.push(`/releases/${created.release.id}`),
    onSettled: (_data, error) => createKey.settle(error),
  });

  const same = Boolean(chosenBaseline) && chosenBaseline === chosenCandidate;
  const ready = Boolean(agent && chosenBaseline && chosenCandidate && !same && title.length <= 200);

  function submit(e: FormEvent) {
    e.preventDefault();
    if (ready && !create.isPending) create.mutate();
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>New release</CardTitle>
        <span className="text-xs text-slate-500">
          Simulates the scenarios the change requires on both versions and gates the candidate.
        </span>
      </CardHeader>
      <CardContent>
        <form className="space-y-3" onSubmit={submit} aria-label="New release">
          {(agents.error ?? versions.error) ? <ErrorState error={agents.error ?? versions.error} /> : null}
          {create.isError ? <ErrorState error={create.error} /> : null}
          <div className="flex flex-wrap items-end gap-3">
            <div className="w-56">
              <Label htmlFor="rel-agent">Agent</Label>
              <Select
                id="rel-agent"
                value={agent?.name ?? ""}
                onChange={(e) => {
                  setAgentName(e.target.value);
                  setBaseline("");
                  setCandidate("");
                }}
              >
                {agentList.length === 0 ? <option value="">No agents registered</option> : null}
                {agentList.map((a) => (
                  <option key={a.id} value={a.name}>
                    {a.name}
                  </option>
                ))}
              </Select>
            </div>
            <div className="w-40">
              <Label htmlFor="rel-baseline">Baseline (in use)</Label>
              <Select id="rel-baseline" value={chosenBaseline} onChange={(e) => setBaseline(e.target.value)}>
                {versionList.map((v) => (
                  <option key={v.id} value={v.version}>
                    {v.version}
                  </option>
                ))}
              </Select>
            </div>
            <div className="w-40">
              <Label htmlFor="rel-candidate">Candidate</Label>
              <Select
                id="rel-candidate"
                value={chosenCandidate}
                onChange={(e) => setCandidate(e.target.value)}
              >
                {versionList.map((v) => (
                  <option key={v.id} value={v.version}>
                    {v.version}
                  </option>
                ))}
              </Select>
            </div>
            <div className="min-w-[14rem] flex-1">
              <Label htmlFor="rel-title">Title (optional)</Label>
              <Input
                id="rel-title"
                value={title}
                maxLength={200}
                placeholder="e.g. Refund flow rewrite"
                onChange={(e) => setTitle(e.target.value)}
              />
            </div>
          </div>
          {versionList.length === 1 ? (
            <p className="text-xs text-slate-500">
              This agent has one version; register another to release it.
            </p>
          ) : same ? (
            <p className="text-xs text-rose-700" role="alert">
              Pick two different versions.
            </p>
          ) : null}
          <div className="flex gap-2">
            <Button type="submit" variant="primary" size="sm" disabled={!ready || create.isPending}>
              <Rocket className="h-4 w-4" aria-hidden="true" />
              {create.isPending ? "Creating…" : "Create and evaluate"}
            </Button>
            <Button type="button" size="sm" variant="ghost" onClick={onCancel}>
              Cancel
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  );
}

const COLUMNS = [
  "Candidate",
  "Baseline",
  "Changed components",
  "Gate",
  "Critical failures",
  "Impacted scenarios",
  "Cost delta",
  "Latency delta (p95)",
  "Created by",
  "Date",
];

export function ReleaseList() {
  const { params, update, replace } = useUrlQuery();
  const agent = params.get("agent") ?? "";
  const canRelease = useCan("release.write");
  const me = useMe();
  const [creating, setCreating] = useState(params.get("new") === "1");
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
  const releases = useInfiniteQuery({
    queryKey: ["releases", projectId, agent],
    queryFn: ({ pageParam, signal }) =>
      api<ReleasePage>(
        withQuery(
          "/releases",
          queryOf<"listReleases">({ project_id: projectId, agent, limit: PAGE_SIZE, cursor: pageParam }),
        ),
        { signal },
      ),
    initialPageParam: "",
    getNextPageParam: (last) => last.next_cursor || undefined,
    enabled: Boolean(projectId),
    // A release being evaluated changes its gate: look again while one is.
    refetchInterval: (q) =>
      q.state.data?.pages.some((p) => p.items.some((r) => r.gate?.effective_outcome === "PENDING"))
        ? 5_000
        : false,
  });
  const items = releases.data?.pages.flatMap((p) => p.items) ?? [];

  return (
    <div className="space-y-4">
      <PageHeader
        title="Releases"
        description="Candidate versions gated against their baseline: what the change reaches, what was simulated, and whether it may ship."
        actions={
          <>
            <Button
              size="sm"
              onClick={() => void releases.refetch()}
              disabled={releases.isFetching || !projectId}
            >
              <RefreshCw
                className={`h-4 w-4 ${releases.isFetching ? "animate-spin motion-reduce:animate-none" : ""}`}
                aria-hidden="true"
              />
              Refresh
            </Button>
            {canRelease && !creating ? (
              <Button size="sm" variant="primary" onClick={() => setCreating(true)} disabled={!projectId}>
                <Plus className="h-4 w-4" aria-hidden="true" />
                New release
              </Button>
            ) : null}
          </>
        }
      />
      {creating && canRelease && projectId ? (
        <NewReleaseForm projectId={projectId} onCancel={() => setCreating(false)} />
      ) : null}
      <Card className="flex flex-wrap items-end gap-3 p-3">
        {projectList.length > 1 ? (
          <div className="w-48">
            <Label htmlFor="rel-project">Project</Label>
            <Select
              id="rel-project"
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
        <div className="w-56">
          <Label htmlFor="rel-filter-agent">Agent</Label>
          <Select id="rel-filter-agent" value={agent} onChange={(e) => update({ agent: e.target.value })}>
            <option value="">All agents</option>
            {(agents.data?.items ?? []).map((a) => (
              <option key={a.id} value={a.name}>
                {a.name}
              </option>
            ))}
          </Select>
        </div>
        {agent ? (
          <Button size="sm" variant="ghost" onClick={() => replace(new URLSearchParams())}>
            Clear filters
          </Button>
        ) : null}
      </Card>
      <Card>
        {projects.isPending || (Boolean(projectId) && releases.isPending) ? (
          <div className="space-y-2 p-4" aria-busy="true" aria-label="Loading releases">
            {Array.from({ length: 4 }, (_, i) => (
              <Skeleton key={i} className="h-12 w-full" />
            ))}
          </div>
        ) : (projects.error ?? releases.error) ? (
          <div className="p-4">
            <ErrorState error={projects.error ?? releases.error} />
          </div>
        ) : items.length === 0 ? (
          <EmptyState title={agent ? "No releases of this agent" : "No releases yet"}>
            Gate a release from CI with{" "}
            <code className="rounded bg-slate-100 px-1">agenttwin release check</code>
            {canRelease ? (
              <>
                , create one with{" "}
                <button type="button" className="text-indigo-700 underline" onClick={() => setCreating(true)}>
                  New release
                </button>
              </>
            ) : null}
            , or load the demo workspace with <code className="rounded bg-slate-100 px-1">make seed</code>.
          </EmptyState>
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[72rem] text-left text-sm">
                <caption className="sr-only">Releases, newest first</caption>
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
                    <ReleaseRow key={r.id} r={r} now={now} meUserId={me?.user?.id} />
                  ))}
                </tbody>
              </table>
            </div>
            <div className="flex items-center justify-between border-t border-slate-100 px-3 py-2 text-xs text-slate-500">
              <span aria-live="polite">
                Showing {items.length} {items.length === 1 ? "release" : "releases"}
              </span>
              {releases.hasNextPage ? (
                <Button
                  size="sm"
                  onClick={() => void releases.fetchNextPage()}
                  disabled={releases.isFetchingNextPage}
                >
                  {releases.isFetchingNextPage ? "Loading…" : "Load more"}
                </Button>
              ) : null}
            </div>
          </>
        )}
      </Card>
    </div>
  );
}
