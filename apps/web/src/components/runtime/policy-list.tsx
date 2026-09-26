"use client";

import { useQuery } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import Link from "next/link";
import { PageHeader } from "@/components/common/page-header";
import { useCan, useMe } from "@/components/shell/me-context";
import { Badge } from "@/components/ui/badge";
import { buttonVariants } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input, Label } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { api, withQuery } from "@/lib/api";
import { type Policy, type PolicyPage, queryOf } from "@/lib/api/runtime";
import { formatRelative } from "@/lib/format";
import { actorLabel } from "@/lib/simulations";
import { useUrlQuery } from "@/lib/use-url-query";
import { ProjectPicker, useProject } from "./project-picker";

const TOOL = /^[A-Za-z0-9_.:-]{1,128}$/;

function PolicyRow({
  p,
  now,
  me,
  projectId,
}: {
  p: Policy;
  now: Date;
  me: string | undefined;
  projectId: string;
}) {
  return (
    <tr className="align-top" data-testid="policy-row" data-policy-id={p.id}>
      <td className="px-3 py-2.5">
        <Link
          href={`/policies/${p.id}?project_id=${projectId}`}
          className="font-medium text-indigo-700 hover:underline"
          aria-label={`Policy ${p.name}`}
        >
          {p.name}
        </Link>
        {p.description ? (
          <div className="mt-1 line-clamp-2 text-xs text-slate-500">{p.description}</div>
        ) : null}
      </td>
      <td className="px-3 py-2.5">
        <code>{p.tool}</code>
      </td>
      <td className="px-3 py-2.5">
        {p.active ? (
          <Badge tone="success">v{p.active.version} active</Badge>
        ) : (
          <Badge tone="neutral" title="The policy guards nothing until a version is activated.">
            Not active
          </Badge>
        )}
        {p.active && p.latest_version > p.active.version ? (
          <div className="mt-1 text-xs text-amber-900">v{p.latest_version} not active yet</div>
        ) : null}
      </td>
      <td className="px-3 py-2.5 text-slate-700">v{p.latest_version}</td>
      <td className="px-3 py-2.5 text-slate-700">
        {p.activated_at ? (
          <span title={p.activated_at}>
            {formatRelative(p.activated_at, now)}
            <div className="text-xs text-slate-500">by {actorLabel(p.activated_by, me)}</div>
          </span>
        ) : (
          "—"
        )}
      </td>
      <td className="px-3 py-2.5 text-slate-700" title={p.updated_at}>
        {formatRelative(p.updated_at, now)}
      </td>
    </tr>
  );
}

const COLUMNS = ["Policy", "Tool", "Guards", "Latest", "Activated", "Updated"];

export function PolicyList() {
  const { params, update } = useUrlQuery();
  const toolParam = params.get("tool") ?? "";
  const tool = TOOL.test(toolParam) ? toolParam : undefined;
  const canWrite = useCan("policy.write");
  const me = useMe();
  const now = new Date();
  const { projects, list, projectId } = useProject();

  const policies = useQuery({
    queryKey: ["policies", projectId, tool],
    queryFn: ({ signal }) =>
      api<PolicyPage>(withQuery("/policies", queryOf<"listPolicies">({ project_id: projectId, tool })), {
        signal,
      }),
    enabled: Boolean(projectId),
  });
  const items = policies.data?.items ?? [];

  return (
    <div className="space-y-4">
      <PageHeader
        title="Policies"
        description="What the runtime gateway decides for each tool call: allow, allow with limits, hold for a person's approval, or deny. A version is tested before it can be activated; one version of a policy is active at a time."
        actions={
          canWrite && projectId ? (
            <Link
              href={`/policies/new?project_id=${projectId}`}
              className={buttonVariants({ variant: "primary", size: "sm" })}
            >
              <Plus className="h-4 w-4" aria-hidden="true" />
              New policy
            </Link>
          ) : null
        }
      />
      <Card className="flex flex-wrap items-end gap-3 p-3">
        <ProjectPicker id="policy-project" list={list} projectId={projectId} />
        <div className="w-56">
          <Label htmlFor="policy-tool">Tool</Label>
          <Input
            key={toolParam}
            id="policy-tool"
            defaultValue={toolParam}
            placeholder="refund_payment"
            onBlur={(e) => update({ tool: e.target.value.trim() })}
            onKeyDown={(e) => {
              if (e.key === "Enter") update({ tool: e.currentTarget.value.trim() });
            }}
          />
        </div>
      </Card>
      <Card>
        {projects.isPending || (Boolean(projectId) && policies.isPending) ? (
          <div className="space-y-2 p-4" aria-busy="true" aria-label="Loading policies">
            {Array.from({ length: 3 }, (_, i) => (
              <Skeleton key={i} className="h-12 w-full" />
            ))}
          </div>
        ) : (projects.error ?? policies.error) ? (
          <div className="p-4">
            <ErrorState error={projects.error ?? policies.error} />
          </div>
        ) : items.length === 0 ? (
          <EmptyState title={tool ? `No policy guards ${tool}` : "No policies yet"}>
            A tool registered with the gateway and guarded by no active policy is allowed. Write one here, or
            deploy them as code with an API key holding{" "}
            <code className="rounded bg-slate-100 px-1">policies:deploy</code> (
            <code className="rounded bg-slate-100 px-1">make seed</code> does, for the demo agent).
          </EmptyState>
        ) : (
          <div className="relative overflow-x-auto">
            <table className="w-full min-w-[56rem] text-left text-sm">
              <caption className="sr-only">Policies</caption>
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
                {items.map((p) => (
                  <PolicyRow key={p.id} p={p} now={now} me={me?.user?.id} projectId={projectId} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
