"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, FlaskConical, GitMerge, RotateCcw, SlidersHorizontal, XCircle } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { PageHeader } from "@/components/common/page-header";
import { useCan, useMe } from "@/components/shell/me-context";
import { SeverityBadge } from "@/components/simulations/badges";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { KeyValue } from "@/components/ui/key-value";
import { Skeleton } from "@/components/ui/skeleton";
import { ErrorState } from "@/components/ui/states";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { api } from "@/lib/api";
import type {
  Regression,
  RegressionDetail as RegressionDetailData,
  RegressionResponse,
} from "@/lib/api/evaluation";
import { formatDateTime, shortId } from "@/lib/format";
import {
  availableActions,
  canTriage,
  regressionActor,
  statusMeaning,
  taxonomyLabel,
} from "@/lib/regressions";
import { useActionKey } from "@/lib/use-action-key";
import { useUrlQuery } from "@/lib/use-url-query";
import { RegressionStatusBadge } from "./regression-badges";
import {
  DraftSection,
  FailuresSection,
  HistorySection,
  MergeForm,
  StatusForm,
  TriageForm,
} from "./regression-sections";

const TABS = [
  ["failures", "Failures"],
  ["test", "Regression test"],
  ["history", "History"],
] as const;
type Tab = (typeof TABS)[number][0];

function isTab(v: string | null): v is Tab {
  return TABS.some(([t]) => t === v);
}

type Panel = "triage" | "dismiss" | "reopen" | "merge" | null;

/** What went wrong, from the representative failure. */
function WhatWentWrong({ regression: r }: { regression: Regression }) {
  const relabelled = r.taxonomy !== r.suggested_taxonomy;
  const reseverity = r.severity !== r.suggested_severity;
  return (
    <Card>
      <CardHeader>
        <CardTitle>What went wrong</CardTitle>
        <Link
          href={`/traces/${r.representative_trace_id}`}
          className="text-xs text-indigo-700 hover:underline"
        >
          Open the representative trace
        </Link>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        <ul className="space-y-1.5" aria-label="Evidence">
          {r.evidence.map((e) => {
            const [label, ...rest] = e.split(": ");
            return (
              <li key={e} className="flex flex-wrap gap-2">
                {rest.length ? (
                  <>
                    <Badge tone={label === r.taxonomy ? "danger" : "neutral"}>{taxonomyLabel(label)}</Badge>
                    <span className="text-slate-800">{rest.join(": ")}</span>
                  </>
                ) : (
                  <span className="text-slate-800">{e}</span>
                )}
              </li>
            );
          })}
        </ul>
        <KeyValue
          className="text-xs"
          items={[
            {
              label: "Label",
              value: (
                <>
                  {taxonomyLabel(r.taxonomy)}
                  {relabelled ? (
                    <span className="text-slate-500"> (suggested {taxonomyLabel(r.suggested_taxonomy)})</span>
                  ) : null}
                </>
              ),
            },
            {
              label: "Also",
              value: r.secondary.length ? r.secondary.map((s) => taxonomyLabel(s)).join(", ") : null,
            },
            {
              label: "Severity",
              value: (
                <>
                  {r.severity}
                  {reseverity ? (
                    <span className="text-slate-500"> (suggested {r.suggested_severity})</span>
                  ) : null}
                  {r.severity_reason ? <span className="text-slate-500"> · {r.severity_reason}</span> : null}
                </>
              ),
            },
            { label: "Component", value: r.component ? <code>{r.component}</code> : null },
          ]}
        />
      </CardContent>
    </Card>
  );
}

function Facts({ regression: r }: { regression: Regression }) {
  const me = useMe();
  return (
    <Card>
      <CardHeader>
        <CardTitle>Regression</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <dl className="grid grid-cols-2 gap-2 text-sm">
          <div>
            <dt className="text-xs text-slate-500">Failures</dt>
            <dd className="text-lg font-semibold tabular-nums" data-testid="occurrence-count">
              {r.occurrence_count}
            </dd>
          </div>
          <div>
            <dt className="text-xs text-slate-500">Versions</dt>
            <dd className="flex flex-wrap gap-1 pt-1">
              {r.versions.map((v) => (
                <Badge key={v}>v{v}</Badge>
              ))}
            </dd>
          </div>
        </dl>
        <KeyValue
          className="text-xs"
          items={[
            { label: "Agent", value: r.agent },
            { label: "First seen", value: formatDateTime(r.first_seen) },
            { label: "Last seen", value: formatDateTime(r.last_seen) },
            { label: "Environments", value: r.environments.join(", ") },
            { label: "Assignee", value: r.assignee ? regressionActor(r.assignee, me?.user?.id) : null },
            { label: "Tags", value: r.tags.length ? r.tags.join(", ") : null },
            { label: "Triaged by", value: r.triaged_by ? regressionActor(r.triaged_by, me?.user?.id) : null },
            { label: "Cluster", value: <code title={r.fingerprint}>{r.fingerprint.slice(0, 12)}</code> },
            { label: "Regression id", value: <code>{r.id}</code> },
          ]}
        />
      </CardContent>
    </Card>
  );
}

export function RegressionDetail({ regressionId }: { regressionId: string }) {
  const qc = useQueryClient();
  const canReview = useCan("review.write");
  const canPromote = useCan("regression.promote");
  const { params, update } = useUrlQuery();
  const tabParam = params.get("tab");
  const tab: Tab = isTab(tabParam) ? tabParam : "failures";
  const [panel, setPanel] = useState<Panel>(null);
  const confirmKey = useActionKey("regression-confirm");

  const detail = useQuery({
    queryKey: ["regression", regressionId],
    queryFn: ({ signal }) => api<RegressionDetailData>(`/regressions/${regressionId}`, { signal }),
  });
  const confirm = useMutation({
    mutationFn: () =>
      api<RegressionResponse>(`/regressions/${regressionId}/confirm`, {
        method: "POST",
        idempotencyKey: confirmKey.key,
        body: {},
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["regression", regressionId] });
      void qc.invalidateQueries({ queryKey: ["regressions"] });
    },
    onSettled: (_d, error) => confirmKey.settle(error),
  });

  if (detail.isPending) {
    return (
      <div className="space-y-4" aria-busy="true" aria-label="Loading the regression">
        <Skeleton className="h-10 w-2/3" />
        <Skeleton className="h-40 w-full" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }
  if (detail.isError) {
    return (
      <div className="space-y-3">
        <Link href="/regressions" className="text-sm text-indigo-700 hover:underline">
          ← Regressions
        </Link>
        <ErrorState error={detail.error} />
      </div>
    );
  }

  const r = detail.data.regression;
  const actions = availableActions(r);
  const close = () => setPanel(null);

  return (
    <div className="space-y-4">
      <PageHeader
        eyebrow={
          <Link href={`/regressions?project_id=${r.project_id}`} className="text-indigo-700 hover:underline">
            Regressions
          </Link>
        }
        title={
          <span className="flex flex-wrap items-center gap-2" data-testid="regression-header">
            {r.title}
            <RegressionStatusBadge status={r.status} />
            <SeverityBadge severity={r.severity} />
          </span>
        }
        description={statusMeaning(r.status)}
        actions={
          <>
            {canPromote && actions.includes("promote") ? (
              <Button
                size="sm"
                variant="primary"
                onClick={() => {
                  setPanel(null);
                  update({ tab: "test" });
                }}
              >
                <FlaskConical className="h-4 w-4" aria-hidden="true" />
                Promote to test
              </Button>
            ) : null}
            {canReview && actions.includes("confirm") ? (
              <Button size="sm" onClick={() => confirm.mutate()} disabled={confirm.isPending}>
                <Check className="h-4 w-4" aria-hidden="true" />
                {confirm.isPending ? "Confirming…" : "Confirm"}
              </Button>
            ) : null}
            {canReview && canTriage(r) ? (
              <Button size="sm" onClick={() => setPanel(panel === "triage" ? null : "triage")}>
                <SlidersHorizontal className="h-4 w-4" aria-hidden="true" />
                Triage
              </Button>
            ) : null}
            {canReview && actions.includes("merge") ? (
              <Button size="sm" onClick={() => setPanel(panel === "merge" ? null : "merge")}>
                <GitMerge className="h-4 w-4" aria-hidden="true" />
                Merge
              </Button>
            ) : null}
            {canReview && actions.includes("reopen") ? (
              <Button size="sm" onClick={() => setPanel(panel === "reopen" ? null : "reopen")}>
                <RotateCcw className="h-4 w-4" aria-hidden="true" />
                Reopen
              </Button>
            ) : null}
            {canReview && actions.includes("dismiss") ? (
              <Button
                size="sm"
                variant="danger"
                onClick={() => setPanel(panel === "dismiss" ? null : "dismiss")}
              >
                <XCircle className="h-4 w-4" aria-hidden="true" />
                Dismiss
              </Button>
            ) : null}
          </>
        }
      />
      {confirm.isError ? <ErrorState error={confirm.error} /> : null}
      {r.merged_into ? (
        <div
          role="note"
          className="rounded-md border border-slate-200 bg-slate-50 p-3 text-sm text-slate-800"
        >
          Merged into{" "}
          <Link href={`/regressions/${r.merged_into}`} className="text-indigo-700 underline">
            regression {shortId(r.merged_into)}
          </Link>
          : its failures are there now.
        </div>
      ) : null}
      {panel === "triage" ? <TriageForm regression={r} onDone={close} /> : null}
      {panel === "dismiss" || panel === "reopen" ? (
        <StatusForm key={panel} regression={r} action={panel} onDone={close} />
      ) : null}
      {panel === "merge" ? <MergeForm regression={r} onDone={close} /> : null}
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
        <div className="xl:col-span-2">
          <WhatWentWrong regression={r} />
        </div>
        <Facts regression={r} />
      </div>
      <Tabs value={tab} onValueChange={(v) => update({ tab: v === "failures" ? "" : v })}>
        <TabsList aria-label="Regression details">
          {TABS.map(([value, label]) => (
            <TabsTrigger key={value} value={value}>
              {label}
            </TabsTrigger>
          ))}
        </TabsList>
        <TabsContent value="failures">
          <FailuresSection detail={detail.data} />
        </TabsContent>
        <TabsContent value="test">
          <DraftSection regression={r} canPromote={canPromote && actions.includes("promote")} />
        </TabsContent>
        <TabsContent value="history">
          <HistorySection detail={detail.data} />
        </TabsContent>
      </Tabs>
    </div>
  );
}
