"use client";

import { useInfiniteQuery, useMutation, useQuery } from "@tanstack/react-query";
import { GitCompareArrows, RefreshCw } from "lucide-react";
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
  type ChangeSetPage,
  type ChangeSetSummary,
  type CreatedChangeSet,
  queryOf,
} from "@/lib/api/control-plane";
import { changeCountsLine } from "@/lib/changes";
import { formatRelative } from "@/lib/format";
import { actorLabel } from "@/lib/simulations";
import type { Agent, AgentVersion, Project } from "@/lib/types";
import { useActionKey } from "@/lib/use-action-key";
import { useUrlQuery } from "@/lib/use-url-query";

const PAGE_SIZE = 25;

function ChangeSetRow({ cs, now, meUserId }: { cs: ChangeSetSummary; now: Date; meUserId?: string }) {
  return (
    <tr className="align-top" data-testid="change-set-row" data-change-set-id={cs.id}>
      <td className="px-3 py-2.5">
        <Link
          href={`/changes/${cs.id}`}
          className="font-medium text-indigo-700 hover:underline"
          aria-label={`Change set ${cs.agent_name} v${cs.base.version} to v${cs.candidate.version}`}
        >
          {cs.agent_name}
        </Link>
        <div className="mt-1 flex flex-wrap items-center gap-1 text-xs text-slate-500">
          <Badge>v{cs.base.version}</Badge>
          <span aria-hidden="true">→</span>
          <span className="sr-only">to</span>
          <Badge tone="brand">v{cs.candidate.version}</Badge>
        </div>
      </td>
      <td className="px-3 py-2.5 text-slate-800">{cs.title || <span className="text-slate-400">—</span>}</td>
      <td className="px-3 py-2.5">
        <span className="text-slate-800">{changeCountsLine(cs.summary)}</span>
        {cs.summary.breaking > 0 ? (
          <Badge tone="danger" className="ml-2">
            {cs.summary.breaking} breaking
          </Badge>
        ) : null}
      </td>
      <td className="px-3 py-2.5 text-slate-700">{actorLabel(cs.created_by, meUserId)}</td>
      <td className="px-3 py-2.5 text-slate-700" title={cs.created_at}>
        {formatRelative(cs.created_at, now)}
      </td>
    </tr>
  );
}

/** Compare two versions of an agent: the change set is computed and stored. */
function CompareForm({ projectId, onCancel }: { projectId: string; onCancel: () => void }) {
  const router = useRouter();
  const [agentName, setAgentName] = useState("");
  const [base, setBase] = useState("");
  const [candidate, setCandidate] = useState("");
  const [title, setTitle] = useState("");
  const createKey = useActionKey("change-set");

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
  // Default: the newest version against the one before it; a candidate the
  // user picks is compared with the version before that one.
  const chosenCandidate = versionList.some((v) => v.version === candidate)
    ? candidate
    : (versionList[0]?.version ?? "");
  const candidateAt = versionList.findIndex((v) => v.version === chosenCandidate);
  const chosenBase = versionList.some((v) => v.version === base)
    ? base
    : (versionList[candidateAt + 1]?.version ??
      versionList.find((v) => v.version !== chosenCandidate)?.version ??
      "");

  const create = useMutation({
    mutationFn: () =>
      api<CreatedChangeSet>(`/projects/${projectId}/change-sets`, {
        method: "POST",
        idempotencyKey: createKey.key,
        body: {
          agent: agent!.name,
          base_version: chosenBase,
          candidate_version: chosenCandidate,
          ...(title.trim() ? { title: title.trim() } : {}),
        } satisfies BodyOf<"createChangeSet">,
      }),
    onSuccess: (cs) => router.push(`/changes/${cs.id}`),
    onSettled: (_data, error) => createKey.settle(error),
  });

  const same = Boolean(chosenBase) && chosenBase === chosenCandidate;
  const ready = Boolean(agent && chosenBase && chosenCandidate && !same && title.length <= 200);

  function submit(e: FormEvent) {
    e.preventDefault();
    if (ready && !create.isPending) create.mutate();
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Compare versions</CardTitle>
        <span className="text-xs text-slate-500">
          Diffs two registered versions and computes which scenarios the change requires.
        </span>
      </CardHeader>
      <CardContent>
        <form className="space-y-3" onSubmit={submit} aria-label="Compare versions">
          {(agents.error ?? versions.error) ? <ErrorState error={agents.error ?? versions.error} /> : null}
          {create.isError ? <ErrorState error={create.error} /> : null}
          <div className="flex flex-wrap items-end gap-3">
            <div className="w-56">
              <Label htmlFor="cs-agent">Agent</Label>
              <Select
                id="cs-agent"
                value={agent?.name ?? ""}
                onChange={(e) => {
                  setAgentName(e.target.value);
                  setBase("");
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
              <Label htmlFor="cs-base">Base version</Label>
              <Select id="cs-base" value={chosenBase} onChange={(e) => setBase(e.target.value)}>
                {versionList.map((v) => (
                  <option key={v.id} value={v.version}>
                    {v.version}
                  </option>
                ))}
              </Select>
            </div>
            <div className="w-40">
              <Label htmlFor="cs-candidate">Candidate version</Label>
              <Select
                id="cs-candidate"
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
              <Label htmlFor="cs-title">Title (optional)</Label>
              <Input
                id="cs-title"
                value={title}
                maxLength={200}
                placeholder="e.g. Faster refunds"
                onChange={(e) => setTitle(e.target.value)}
              />
            </div>
          </div>
          {versionList.length === 1 ? (
            <p className="text-xs text-slate-500">This agent has one version; register another to compare.</p>
          ) : same ? (
            <p className="text-xs text-rose-700" role="alert">
              Pick two different versions.
            </p>
          ) : null}
          <div className="flex gap-2">
            <Button type="submit" variant="primary" size="sm" disabled={!ready || create.isPending}>
              <GitCompareArrows className="h-4 w-4" aria-hidden="true" />
              {create.isPending ? "Comparing…" : "Compare"}
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

export function ChangeSetList() {
  const { params, update, replace } = useUrlQuery();
  const agent = params.get("agent") ?? "";
  const canCompare = useCan("release.write");
  const me = useMe();
  const [comparing, setComparing] = useState(params.get("compare") === "1");
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
  const changeSets = useInfiniteQuery({
    queryKey: ["change-sets", projectId, agent],
    queryFn: ({ pageParam, signal }) =>
      api<ChangeSetPage>(
        withQuery(
          `/projects/${projectId}/change-sets`,
          queryOf<"listChangeSets">({ agent, limit: PAGE_SIZE, cursor: pageParam }),
        ),
        { signal },
      ),
    initialPageParam: "",
    getNextPageParam: (last) => last.next_cursor || undefined,
    enabled: Boolean(projectId),
  });
  const items = changeSets.data?.pages.flatMap((p) => p.items) ?? [];

  return (
    <div className="space-y-4">
      <PageHeader
        title="Changes"
        description="What changed between two versions of an agent, what the change reaches in the dependency graph, and which scenarios it requires."
        actions={
          <>
            <Button
              size="sm"
              onClick={() => void changeSets.refetch()}
              disabled={changeSets.isFetching || !projectId}
            >
              <RefreshCw
                className={`h-4 w-4 ${changeSets.isFetching ? "animate-spin motion-reduce:animate-none" : ""}`}
                aria-hidden="true"
              />
              Refresh
            </Button>
            {canCompare && !comparing ? (
              <Button size="sm" variant="primary" onClick={() => setComparing(true)} disabled={!projectId}>
                <GitCompareArrows className="h-4 w-4" aria-hidden="true" />
                Compare versions
              </Button>
            ) : null}
          </>
        }
      />
      {comparing && canCompare && projectId ? (
        <CompareForm projectId={projectId} onCancel={() => setComparing(false)} />
      ) : null}
      <Card className="flex flex-wrap items-end gap-3 p-3">
        {projectList.length > 1 ? (
          <div className="w-48">
            <Label htmlFor="cs-project">Project</Label>
            <Select
              id="cs-project"
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
          <Label htmlFor="cs-filter-agent">Agent</Label>
          <Select id="cs-filter-agent" value={agent} onChange={(e) => update({ agent: e.target.value })}>
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
        {projects.isPending || (Boolean(projectId) && changeSets.isPending) ? (
          <div className="space-y-2 p-4" aria-busy="true" aria-label="Loading change sets">
            {Array.from({ length: 4 }, (_, i) => (
              <Skeleton key={i} className="h-12 w-full" />
            ))}
          </div>
        ) : (projects.error ?? changeSets.error) ? (
          <div className="p-4">
            <ErrorState error={projects.error ?? changeSets.error} />
          </div>
        ) : items.length === 0 ? (
          <EmptyState title={agent ? "No change sets for this agent" : "No change sets yet"}>
            {canCompare ? (
              <>
                Compare two versions of an agent with{" "}
                <button
                  type="button"
                  className="text-indigo-700 underline"
                  onClick={() => setComparing(true)}
                >
                  Compare versions
                </button>
                ,{" "}
              </>
            ) : null}
            or load the demo workspace with <code className="rounded bg-slate-100 px-1">make seed</code>.
          </EmptyState>
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[48rem] text-left text-sm">
                <caption className="sr-only">Change sets, newest first</caption>
                <thead className="border-b border-slate-100 text-xs uppercase tracking-wide text-slate-500">
                  <tr>
                    <th scope="col" className="px-3 py-2 font-medium">
                      Agent
                    </th>
                    <th scope="col" className="px-3 py-2 font-medium">
                      Title
                    </th>
                    <th scope="col" className="px-3 py-2 font-medium">
                      Changes
                    </th>
                    <th scope="col" className="px-3 py-2 font-medium">
                      Created by
                    </th>
                    <th scope="col" className="px-3 py-2 font-medium">
                      Created
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {items.map((cs) => (
                    <ChangeSetRow key={cs.id} cs={cs} now={now} meUserId={me?.user?.id} />
                  ))}
                </tbody>
              </table>
            </div>
            <div className="flex items-center justify-between border-t border-slate-100 px-3 py-2 text-xs text-slate-500">
              <span aria-live="polite">
                Showing {items.length} {items.length === 1 ? "change set" : "change sets"}
              </span>
              {changeSets.hasNextPage ? (
                <Button
                  size="sm"
                  onClick={() => void changeSets.fetchNextPage()}
                  disabled={changeSets.isFetchingNextPage}
                >
                  {changeSets.isFetchingNextPage ? "Loading…" : "Load more"}
                </Button>
              ) : null}
            </div>
          </>
        )}
      </Card>
    </div>
  );
}
