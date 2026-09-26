"use client";

import { useQueries, useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";
import { PageHeader } from "@/components/common/page-header";
import { RiskBadge } from "@/components/traces/badges";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { api } from "@/lib/api";
import { formatDateTime, shortId } from "@/lib/format";
import type { Agent, AgentVersion, Project } from "@/lib/types";

function Versions({ agent }: { agent: Agent }) {
  const versions = useQuery({
    queryKey: ["agent-versions", agent.id],
    queryFn: ({ signal }) => api<{ items: AgentVersion[] }>(`/agents/${agent.id}/versions`, { signal }),
  });
  const [open, setOpen] = useState<string | null>(null);
  if (versions.isPending) return <Skeleton className="h-16" />;
  if (versions.isError) return <ErrorState error={versions.error} />;
  return (
    <div className="relative overflow-x-auto">
      <table className="w-full min-w-[36rem] text-left text-sm">
        <caption className="sr-only">Versions of {agent.name}</caption>
        <thead className="text-xs uppercase tracking-wide text-slate-500">
          <tr>
            <th scope="col" className="py-1.5 pr-3 font-medium">
              Version
            </th>
            <th scope="col" className="py-1.5 pr-3 font-medium">
              Model
            </th>
            <th scope="col" className="py-1.5 pr-3 font-medium">
              Prompt
            </th>
            <th scope="col" className="py-1.5 pr-3 font-medium">
              Tools
            </th>
            <th scope="col" className="py-1.5 pr-3 font-medium">
              Registered
            </th>
            <th scope="col" className="py-1.5 font-medium">
              Traces
            </th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {versions.data.items.map((v) => (
            <tr key={v.id} className="align-top">
              <td className="py-2 pr-3">
                <Badge tone="brand">v{v.version}</Badge>
              </td>
              <td className="py-2 pr-3 text-slate-700">{v.model_name ?? "—"}</td>
              <td className="py-2 pr-3">
                <code className="text-xs" title={v.prompt_sha256 ?? undefined}>
                  {shortId(v.prompt_sha256, 10)}
                </code>
              </td>
              <td className="py-2 pr-3">
                <button
                  type="button"
                  className="text-indigo-700 hover:underline"
                  aria-expanded={open === v.id}
                  onClick={() => setOpen(open === v.id ? null : v.id)}
                >
                  {v.manifest.tools?.length ?? 0} tools
                </button>
                {open === v.id ? (
                  <ul className="mt-1 space-y-1">
                    {(v.manifest.tools ?? []).map((t) => (
                      <li key={t.name} className="flex flex-wrap items-center gap-1 text-xs">
                        <code>{t.name}</code>
                        <RiskBadge risk={t.risk} />
                        {t.approval_required_when ? (
                          <span className="text-slate-500">approval when {t.approval_required_when}</span>
                        ) : null}
                      </li>
                    ))}
                  </ul>
                ) : null}
              </td>
              <td className="py-2 pr-3 text-slate-600">
                <time dateTime={v.created_at}>{formatDateTime(v.created_at)}</time>
                <div className="text-xs text-slate-500">{v.created_by}</div>
              </td>
              <td className="py-2">
                <Link
                  href={`/traces?project_id=${v.project_id}&agent=${encodeURIComponent(agent.name)}&agent_version=${encodeURIComponent(v.version)}`}
                  className="text-indigo-700 hover:underline"
                >
                  View traces
                </Link>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function AgentsView() {
  const projects = useQuery({
    queryKey: ["projects"],
    queryFn: ({ signal }) => api<{ items: Project[] }>("/projects", { signal }),
  });
  const agentQueries = useQueries({
    queries: (projects.data?.items ?? []).map((p) => ({
      queryKey: ["agents", p.id],
      queryFn: ({ signal }: { signal: AbortSignal }) =>
        api<{ items: Agent[] }>(`/projects/${p.id}/agents`, { signal }),
    })),
  });

  if (projects.isPending) return <Skeleton className="h-40" />;
  if (projects.isError) return <ErrorState error={projects.error} />;
  return (
    <div className="space-y-4">
      <PageHeader
        title="Agents"
        description="Registered agents and their immutable versions (from agent manifests)."
      />
      {projects.data.items.map((p, i) => {
        const q = agentQueries[i];
        return (
          <section key={p.id} aria-labelledby={`project-${p.id}`} className="space-y-3">
            <h2 id={`project-${p.id}`} className="text-sm font-semibold text-slate-700">
              {p.name} <span className="font-normal text-slate-500">({p.slug})</span>
            </h2>
            {!q || q.isPending ? (
              <Skeleton className="h-24" />
            ) : q.isError ? (
              <ErrorState error={q.error} />
            ) : q.data.items.length === 0 ? (
              <Card>
                <EmptyState title="No agents registered">
                  Register a manifest with{" "}
                  <code className="rounded bg-slate-100 px-1">agenttwin manifest register</code> or run{" "}
                  <code className="rounded bg-slate-100 px-1">make seed</code> for the demo.
                </EmptyState>
              </Card>
            ) : (
              q.data.items.map((a) => (
                <Card key={a.id}>
                  <CardHeader>
                    <CardTitle>{a.name}</CardTitle>
                    <span className="text-xs text-slate-500">
                      {a.version_count} {a.version_count === 1 ? "version" : "versions"} · latest v
                      {a.latest_version}
                    </span>
                  </CardHeader>
                  <CardContent className="space-y-3">
                    {a.description ? <p className="text-sm text-slate-700">{a.description}</p> : null}
                    <Versions agent={a} />
                  </CardContent>
                </Card>
              ))
            )}
          </section>
        );
      })}
    </div>
  );
}
