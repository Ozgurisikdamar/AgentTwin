"use client";

import { useQuery } from "@tanstack/react-query";
import { Play, RefreshCw } from "lucide-react";
import Link from "next/link";
import { PageHeader } from "@/components/common/page-header";
import { useCan, useMe } from "@/components/shell/me-context";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { CopyButton } from "@/components/ui/copy-button";
import { KeyValue } from "@/components/ui/key-value";
import { Skeleton } from "@/components/ui/skeleton";
import { ErrorState } from "@/components/ui/states";
import { api } from "@/lib/api";
import type { ChangeImpact, ChangeSet } from "@/lib/api/control-plane";
import { changeCountsLine, impactHeadline, simulateHref } from "@/lib/changes";
import { formatDateTime, shortId } from "@/lib/format";
import { actorLabel } from "@/lib/simulations";
import { ChangeItems } from "./change-items";
import {
  AffectedComponents,
  ImpactStatus,
  LinkedControls,
  RequiredScenarios,
  RiskyChanges,
} from "./impact-sections";

function DeclaredAndGit({ cs }: { cs: ChangeSet }) {
  if (!cs.git && cs.declared.length === 0) return null;
  return (
    <div className="space-y-1 border-t border-slate-100 px-4 py-3 text-xs text-slate-600">
      {cs.git ? (
        <p>
          From CI: commits <span className="font-mono">{cs.git.base_commit?.slice(0, 12) ?? "—"}</span> →{" "}
          <span className="font-mono">{cs.git.candidate_commit?.slice(0, 12) ?? "—"}</span>
          {cs.git.changed_files?.length ? `, ${cs.git.changed_files.length} changed files` : ""}.
        </p>
      ) : null}
      {cs.declared.length ? (
        <p>
          Declared by the author:{" "}
          {cs.declared
            .map((d) => `${d.kind} ${d.name} ${d.change}${d.summary ? ` (${d.summary})` : ""}`)
            .join("; ")}
          .
        </p>
      ) : null}
    </div>
  );
}

function ImpactSummary({
  impact,
  isFetching,
  onRecompute,
}: {
  impact: ChangeImpact;
  isFetching: boolean;
  onRecompute: () => void;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Impact</CardTitle>
        <Button size="sm" onClick={onRecompute} disabled={isFetching} aria-label="Recompute the impact">
          <RefreshCw
            className={`h-4 w-4 ${isFetching ? "animate-spin motion-reduce:animate-none" : ""}`}
            aria-hidden="true"
          />
          Recompute
        </Button>
      </CardHeader>
      <CardContent className="space-y-3">
        <p className="text-base font-semibold text-slate-900" data-testid="impact-headline">
          {impactHeadline(impact)}
        </p>
        <ImpactStatus impact={impact} />
        <p className="text-xs text-slate-500" title={impact.computed_at}>
          Computed {formatDateTime(impact.computed_at)} from the current graph and scenario library.
        </p>
      </CardContent>
    </Card>
  );
}

export function ChangeSetDetail({ changeSetId }: { changeSetId: string }) {
  const me = useMe();
  const canRun = useCan("simulation.run");
  const changeSet = useQuery({
    queryKey: ["change-set", changeSetId],
    queryFn: ({ signal }) => api<ChangeSet>(`/change-sets/${changeSetId}`, { signal }),
    staleTime: Number.POSITIVE_INFINITY, // stored once, never edited
  });
  const impact = useQuery({
    queryKey: ["change-set-impact", changeSetId],
    queryFn: ({ signal }) => api<ChangeImpact>(`/change-sets/${changeSetId}/impact`, { signal }),
    enabled: changeSet.isSuccess,
    staleTime: 30_000,
  });

  if (changeSet.isPending) {
    return (
      <div className="space-y-4" aria-busy="true" aria-label="Loading the change set">
        <Skeleton className="h-10 w-2/3" />
        <Skeleton className="h-40 w-full" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }
  if (changeSet.isError) {
    return (
      <div className="space-y-3">
        <Link href="/changes" className="text-sm text-indigo-700 hover:underline">
          ← Changes
        </Link>
        <ErrorState error={changeSet.error} />
      </div>
    );
  }

  const cs = changeSet.data;
  const data = impact.data;
  const runnable = data ? data.scenarios.filter((s) => s.in_library).length : 0;

  return (
    <div className="space-y-4">
      <PageHeader
        eyebrow={
          <Link href={`/changes?project_id=${cs.project_id}`} className="text-indigo-700 hover:underline">
            Changes
          </Link>
        }
        title={
          <span className="flex flex-wrap items-center gap-2">
            {cs.agent_name}
            <Badge>v{cs.base.version}</Badge>
            <span aria-hidden="true" className="text-slate-400">
              →
            </span>
            <span className="sr-only">to</span>
            <Badge tone="brand">v{cs.candidate.version}</Badge>
          </span>
        }
        description={cs.title || undefined}
        actions={
          canRun && data && runnable > 0 ? (
            <Link
              href={simulateHref(data)}
              className={buttonVariants({ variant: "primary", size: "sm" })}
              data-testid="simulate-required"
            >
              <Play className="h-4 w-4" aria-hidden="true" />
              Simulate {runnable} required {runnable === 1 ? "scenario" : "scenarios"}
            </Link>
          ) : null
        }
      />
      <div className="grid gap-4 xl:grid-cols-3">
        <Card>
          <CardHeader>
            <CardTitle>Change set</CardTitle>
          </CardHeader>
          <CardContent>
            <KeyValue
              items={[
                { label: "Agent", value: cs.agent_name },
                { label: "Base", value: `v${cs.base.version}` },
                { label: "Candidate", value: `v${cs.candidate.version}` },
                { label: "Changes", value: changeCountsLine(cs.summary) },
                {
                  label: "Breaking",
                  value: cs.summary.breaking ? <Badge tone="danger">{cs.summary.breaking}</Badge> : "none",
                },
                { label: "Created by", value: actorLabel(cs.created_by, me?.user?.id) },
                { label: "Created", value: formatDateTime(cs.created_at) },
                {
                  label: "Content hash",
                  value: (
                    <span className="inline-flex items-center gap-1">
                      <span className="font-mono">{shortId(cs.content_sha256, 12)}</span>
                      <CopyButton value={cs.content_sha256} label="content hash" />
                    </span>
                  ),
                },
              ]}
            />
          </CardContent>
        </Card>
        <div className="xl:col-span-2">
          {impact.isPending ? (
            <Card>
              <CardHeader>
                <CardTitle>Impact</CardTitle>
              </CardHeader>
              <CardContent className="space-y-2" aria-busy="true" aria-label="Computing the impact">
                <Skeleton className="h-6 w-1/2" />
                <Skeleton className="h-16 w-full" />
              </CardContent>
            </Card>
          ) : impact.isError ? (
            <Card>
              <CardHeader>
                <CardTitle>Impact</CardTitle>
                <Button size="sm" onClick={() => void impact.refetch()}>
                  Try again
                </Button>
              </CardHeader>
              <CardContent>
                <ErrorState error={impact.error} />
              </CardContent>
            </Card>
          ) : data ? (
            <ImpactSummary
              impact={data}
              isFetching={impact.isFetching}
              onRecompute={() => void impact.refetch()}
            />
          ) : null}
        </div>
      </div>
      <Card>
        <CardHeader>
          <CardTitle>What changed</CardTitle>
          <span className="text-xs text-slate-500">
            {cs.summary.items} {cs.summary.items === 1 ? "change" : "changes"} between v{cs.base.version} and
            v{cs.candidate.version}
          </span>
        </CardHeader>
        <ChangeItems items={cs.items} />
        <DeclaredAndGit cs={cs} />
      </Card>
      {data ? (
        <>
          <RequiredScenarios impact={data} />
          <div
            className={
              data.graph?.policies.length || data.graph?.evaluators.length ? "grid gap-4 xl:grid-cols-2" : ""
            }
          >
            <RiskyChanges impact={data} />
            <LinkedControls impact={data} />
          </div>
          <AffectedComponents impact={data} />
        </>
      ) : null}
    </div>
  );
}
