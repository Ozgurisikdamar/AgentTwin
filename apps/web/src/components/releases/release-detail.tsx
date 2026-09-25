"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCw, RotateCw, ShieldAlert } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";
import { ChangeItems } from "@/components/changes/change-items";
import {
  AffectedComponents,
  ImpactStatus,
  LinkedControls,
  RiskyChanges,
} from "@/components/changes/impact-sections";
import { PageHeader } from "@/components/common/page-header";
import { useCan, useMe } from "@/components/shell/me-context";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Label, Select } from "@/components/ui/input";
import { KeyValue } from "@/components/ui/key-value";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { api, withQuery } from "@/lib/api";
import {
  type ChangeSet,
  type Release,
  type ReleaseDetail as ReleaseDetailData,
  type ReleaseGate,
  queryOf,
} from "@/lib/api/control-plane";
import { changeCountsLine } from "@/lib/changes";
import { formatDateTime, shortId } from "@/lib/format";
import { formatCostDelta, formatLatencyDelta, gateBadge, overrideState } from "@/lib/releases";
import { actorLabel } from "@/lib/simulations";
import { useActionKey } from "@/lib/use-action-key";
import { useUrlQuery } from "@/lib/use-url-query";
import { Delta, GateOutcomeBadge } from "./gate-badge";
import {
  AuditSection,
  EvidenceByRule,
  EvidenceSection,
  OverrideBanner,
  OverrideForm,
  SuiteSection,
  SummarySection,
  WhySection,
} from "./gate-sections";

const POLL_MS = 5_000;

const TABS = [
  ["summary", "Summary"],
  ["changes", "Changes"],
  ["blast-radius", "Blast radius"],
  ["simulations", "Simulations"],
  ["evals", "Evals"],
  ["evidence", "Evidence"],
  ["audit", "Audit"],
] as const;
type Tab = (typeof TABS)[number][0];

function isTab(v: string | null): v is Tab {
  return TABS.some(([t]) => t === v);
}

/** The change set between the two versions (the Changes tab). */
function ChangesTab({ release }: { release: Release }) {
  const cs = useQuery({
    queryKey: ["change-set", release.change_set_id],
    queryFn: ({ signal }) => api<ChangeSet>(`/change-sets/${release.change_set_id}`, { signal }),
    staleTime: Number.POSITIVE_INFINITY, // stored once, never edited
  });
  return (
    <Card>
      <CardHeader>
        <CardTitle>What changed</CardTitle>
        <Link href={`/changes/${release.change_set_id}`} className="text-xs text-indigo-700 hover:underline">
          Open the change set
        </Link>
      </CardHeader>
      {cs.isPending ? (
        <CardContent>
          <Skeleton className="h-24 w-full" />
        </CardContent>
      ) : cs.isError ? (
        <CardContent>
          <ErrorState error={cs.error} />
        </CardContent>
      ) : (
        <ChangeItems items={cs.data.items} />
      )}
    </Card>
  );
}

/** What the change reaches, as it was when the revision was requested (the Blast radius tab). */
function BlastRadiusTab({ gate }: { gate: ReleaseGate }) {
  const impact = gate.impact;
  return (
    <div className="space-y-4">
      <Card>
        <CardContent className="space-y-2 pt-4">
          <ImpactStatus impact={impact} />
          <p className="text-xs text-slate-500">
            As computed when revision {gate.revision} was requested ({formatDateTime(impact.computed_at)});
            later changes to the graph do not rewrite it.
          </p>
        </CardContent>
      </Card>
      <div
        className={
          impact.graph?.policies.length || impact.graph?.evaluators.length ? "grid gap-4 xl:grid-cols-2" : ""
        }
      >
        <RiskyChanges impact={impact} />
        <LinkedControls impact={impact} />
      </div>
      <AffectedComponents impact={impact} />
    </div>
  );
}

/** Every revision, newest first; one is shown at a time. */
function RevisionPicker({
  detail,
  revision,
  onPick,
}: {
  detail: ReleaseDetailData;
  revision: number;
  onPick: (r: number) => void;
}) {
  if (detail.revisions.length < 2) return null;
  return (
    <div className="w-72">
      <Label htmlFor="rel-revision">Revision</Label>
      <Select id="rel-revision" value={String(revision)} onChange={(e) => onPick(Number(e.target.value))}>
        {detail.revisions.map((r, i) => {
          const b = gateBadge(r);
          return (
            <option key={r.revision} value={r.revision}>
              {`Revision ${r.revision}${i === 0 ? " (latest)" : ""} · ${b.label}${b.note ? ` (${b.note})` : ""} · ${formatDateTime(r.requested_at)}`}
            </option>
          );
        })}
      </Select>
    </div>
  );
}

function ReleaseFacts({ release, meUserId }: { release: Release; meUserId?: string }) {
  const ci = release.ci_url && /^https?:\/\//i.test(release.ci_url) ? release.ci_url : null;
  return (
    <KeyValue
      className="text-xs"
      items={[
        { label: "Changes", value: changeCountsLine(release.changes) },
        { label: "Commit", value: release.commit_sha ? <code>{release.commit_sha}</code> : null },
        {
          label: "CI run",
          value: ci ? (
            <a
              href={ci}
              target="_blank"
              rel="noopener noreferrer"
              className="text-indigo-700 hover:underline"
            >
              {ci}
            </a>
          ) : (
            release.ci_url
          ),
        },
        {
          label: "Created",
          value: `${formatDateTime(release.created_at)} by ${actorLabel(release.created_by, meUserId)}`,
        },
        { label: "Release id", value: <code>{release.id}</code> },
      ]}
    />
  );
}

export function ReleaseDetail({ releaseId }: { releaseId: string }) {
  const me = useMe();
  const qc = useQueryClient();
  const canEvaluate = useCan("release.write");
  const canOverride = useCan("release.override");
  const { params, update } = useUrlQuery();
  const tabParam = params.get("tab");
  const tab: Tab = isTab(tabParam) ? tabParam : "summary";
  const [overriding, setOverriding] = useState(false);
  const evaluateKey = useActionKey("release-evaluate");

  const detail = useQuery({
    queryKey: ["release", releaseId],
    queryFn: ({ signal }) => api<ReleaseDetailData>(`/releases/${releaseId}`, { signal }),
    refetchInterval: (q) => (q.state.data?.release.gate?.effective_outcome === "PENDING" ? POLL_MS : false),
  });
  const latest = detail.data?.revisions[0]?.revision ?? 0;
  const asked = Number(params.get("revision"));
  const revision =
    Number.isInteger(asked) && detail.data?.revisions.some((r) => r.revision === asked) ? asked : latest;
  const gate = useQuery({
    queryKey: ["release-gate", releaseId, revision],
    queryFn: ({ signal }) =>
      api<ReleaseGate>(withQuery(`/releases/${releaseId}/gate`, queryOf<"getReleaseGate">({ revision })), {
        signal,
      }),
    enabled: revision > 0,
    refetchInterval: (q) => (q.state.data?.status === "EVALUATING" ? POLL_MS : false),
  });
  // A gate that decided while the page watched it makes the release's
  // summary (header, history, list) stale.
  const decidedAt = gate.data?.decided_at;
  useEffect(() => {
    if (!decidedAt) return;
    void qc.invalidateQueries({ queryKey: ["release", releaseId] });
    void qc.invalidateQueries({ queryKey: ["releases"] });
  }, [decidedAt, qc, releaseId]);

  const evaluate = useMutation({
    mutationFn: () =>
      api<ReleaseGate>(`/releases/${releaseId}/evaluate`, {
        method: "POST",
        idempotencyKey: evaluateKey.key,
      }),
    onSuccess: (g) => {
      qc.setQueryData(["release-gate", releaseId, g.revision], g);
      void qc.invalidateQueries({ queryKey: ["release", releaseId] });
      void qc.invalidateQueries({ queryKey: ["releases"] });
      setOverriding(false);
      update({ revision: "" });
    },
    onSettled: (_d, error) => evaluateKey.settle(error),
  });

  if (detail.isPending) {
    return (
      <div className="space-y-4" aria-busy="true" aria-label="Loading the release">
        <Skeleton className="h-10 w-2/3" />
        <Skeleton className="h-40 w-full" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }
  if (detail.isError) {
    return (
      <div className="space-y-3">
        <Link href="/releases" className="text-sm text-indigo-700 hover:underline">
          ← Releases
        </Link>
        <ErrorState error={detail.error} />
      </div>
    );
  }

  const release = detail.data.release;
  const g = gate.data;
  const summary = detail.data.revisions.find((r) => r.revision === revision) ?? null;
  const override = g ? overrideState(g, latest) : { possible: false, reason: null };
  const reviewerBlocked = me?.principal.role === "reviewer" && g && !g.policy.allow_reviewer_override;
  const s = g?.summary ?? null;

  return (
    <div className="space-y-4">
      <PageHeader
        eyebrow={
          <Link
            href={`/releases?project_id=${release.project_id}`}
            className="text-indigo-700 hover:underline"
          >
            Releases
          </Link>
        }
        title={
          <span className="flex flex-wrap items-center gap-2" data-testid="release-header">
            {release.agent.name}
            <Badge>v{release.baseline.version}</Badge>
            <span aria-hidden="true" className="text-slate-400">
              →
            </span>
            <span className="sr-only">to</span>
            <Badge tone="brand">v{release.candidate.version}</Badge>
            <GateOutcomeBadge gate={summary} className="ml-1" />
          </span>
        }
        description={release.title || undefined}
        actions={
          <>
            {canOverride && override.possible && !overriding && !reviewerBlocked ? (
              <Button size="sm" variant="danger" onClick={() => setOverriding(true)}>
                <ShieldAlert className="h-4 w-4" aria-hidden="true" />
                Override
              </Button>
            ) : null}
            {canEvaluate ? (
              <Button
                size="sm"
                variant={latest ? "secondary" : "primary"}
                onClick={() => evaluate.mutate()}
                disabled={evaluate.isPending || summary?.effective_outcome === "PENDING"}
              >
                <RotateCw className="h-4 w-4" aria-hidden="true" />
                {evaluate.isPending ? "Requesting…" : latest ? "Evaluate again" : "Evaluate"}
              </Button>
            ) : null}
          </>
        }
      />
      {evaluate.isError ? <ErrorState error={evaluate.error} /> : null}
      <div className="flex flex-wrap items-end justify-between gap-3">
        <RevisionPicker
          detail={detail.data}
          revision={revision}
          onPick={(r) => update({ revision: r === latest ? "" : String(r) })}
        />
        {revision && revision !== latest ? (
          <p className="text-xs text-slate-600" role="note">
            An earlier revision, kept as it was decided.{" "}
            <button
              type="button"
              className="text-indigo-700 underline"
              onClick={() => update({ revision: "" })}
            >
              Show the latest
            </button>
          </p>
        ) : null}
      </div>

      {latest === 0 ? (
        <Card>
          <EmptyState title="This release was not evaluated">
            {canEvaluate
              ? "Evaluate it to simulate the scenarios its change requires and gate the candidate."
              : null}
          </EmptyState>
        </Card>
      ) : gate.isPending ? (
        <Skeleton className="h-40 w-full" />
      ) : gate.isError ? (
        <Card>
          <CardHeader>
            <CardTitle>Gate</CardTitle>
            <Button size="sm" onClick={() => void gate.refetch()}>
              <RefreshCw className="h-4 w-4" aria-hidden="true" />
              Try again
            </Button>
          </CardHeader>
          <CardContent>
            <ErrorState error={gate.error} />
          </CardContent>
        </Card>
      ) : g ? (
        <>
          {g.status === "EVALUATING" ? (
            <div
              role="status"
              className="rounded-md border border-slate-200 bg-slate-50 p-4 text-sm text-slate-800"
            >
              <p className="font-medium">Evaluating revision {g.revision}…</p>
              <p className="mt-1 text-slate-600">
                Both versions run the {g.suite.length} required{" "}
                {g.suite.length === 1 ? "scenario" : "scenarios"}; the gate decides when the evaluation
                reports.
                {g.eval_run_id ? (
                  <>
                    {" "}
                    <Link href={`/evaluations/${g.eval_run_id}`} className="text-indigo-700 underline">
                      Follow the eval run
                    </Link>
                    .
                  </>
                ) : null}
              </p>
            </div>
          ) : null}
          <OverrideBanner gate={g} meUserId={me?.user?.id} />
          {overriding && override.possible ? (
            <OverrideForm
              release={release}
              gate={g}
              onCancel={() => setOverriding(false)}
              onDone={() => setOverriding(false)}
            />
          ) : null}
          {canOverride && override.possible && reviewerBlocked ? (
            <p className="text-xs text-slate-600" role="note">
              The project&apos;s gate policy does not let reviewers override; an owner or administrator can.
            </p>
          ) : null}
          <div className="grid gap-4 xl:grid-cols-3">
            <div className="xl:col-span-2">
              {g.decision ? (
                <WhySection gate={g} onShowEvidence={() => update({ tab: "evals" })} />
              ) : (
                <Card>
                  <CardContent className="pt-4 text-sm text-slate-600">
                    The decision appears here when the evaluation reports.
                  </CardContent>
                </Card>
              )}
            </div>
            <Card>
              <CardHeader>
                <CardTitle>Release</CardTitle>
              </CardHeader>
              <CardContent className="space-y-3">
                <dl className="grid grid-cols-2 gap-2 text-sm">
                  <div>
                    <dt className="text-xs text-slate-500">Critical failures</dt>
                    <dd className="text-lg font-semibold tabular-nums">
                      {s ? s.new_critical_failures : "—"}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-xs text-slate-500">Impacted scenarios</dt>
                    <dd className="text-lg font-semibold tabular-nums">{s ? s.scenarios : g.suite.length}</dd>
                  </div>
                  <div>
                    <dt className="text-xs text-slate-500">Cost delta</dt>
                    <dd className="text-lg font-semibold">
                      <Delta value={s?.cost_delta_usd} text={formatCostDelta(s?.cost_delta_usd)} />
                    </dd>
                  </div>
                  <div>
                    <dt className="text-xs text-slate-500">Latency delta (p95)</dt>
                    <dd className="text-lg font-semibold">
                      <Delta
                        value={s?.latency_p95_delta_ms}
                        text={formatLatencyDelta(s?.latency_p95_delta_ms)}
                      />
                    </dd>
                  </div>
                </dl>
                <ReleaseFacts release={release} meUserId={me?.user?.id} />
              </CardContent>
            </Card>
          </div>
          <Tabs value={tab} onValueChange={(v) => update({ tab: v === "summary" ? "" : v })}>
            <TabsList aria-label="Release details" className="flex-wrap">
              {TABS.map(([value, label]) => (
                <TabsTrigger key={value} value={value}>
                  {label}
                </TabsTrigger>
              ))}
            </TabsList>
            <TabsContent value="summary">
              <SummarySection gate={g} meUserId={me?.user?.id} />
              <RevisionHistory
                detail={detail.data}
                current={revision}
                latest={latest}
                onPick={(r) => update({ revision: r === latest ? "" : String(r) })}
              />
            </TabsContent>
            <TabsContent value="changes">
              <ChangesTab release={release} />
            </TabsContent>
            <TabsContent value="blast-radius">
              <BlastRadiusTab gate={g} />
            </TabsContent>
            <TabsContent value="simulations">
              <SuiteSection gate={g} />
            </TabsContent>
            <TabsContent value="evals">
              <EvidenceByRule gate={g} projectId={release.project_id} />
            </TabsContent>
            <TabsContent value="evidence">
              <EvidenceSection release={release} gate={g} />
            </TabsContent>
            <TabsContent value="audit">
              <AuditSection releaseId={release.id} />
            </TabsContent>
          </Tabs>
        </>
      ) : null}
    </div>
  );
}

/** Every evaluation of the release (spec §122): its gate at the time and any override. */
function RevisionHistory({
  detail,
  current,
  latest,
  onPick,
}: {
  detail: ReleaseDetailData;
  current: number;
  latest: number;
  onPick: (r: number) => void;
}) {
  const me = useMe();
  return (
    <Card className="mt-4">
      <CardHeader>
        <CardTitle>History</CardTitle>
        <span className="text-xs text-slate-500">
          Each evaluation is a revision; decisions are never rewritten.
        </span>
      </CardHeader>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[40rem] text-left text-sm">
          <caption className="sr-only">Revisions, newest first</caption>
          <thead className="border-b border-slate-100 text-xs uppercase tracking-wide text-slate-500">
            <tr>
              <th scope="col" className="px-3 py-2 font-medium">
                Revision
              </th>
              <th scope="col" className="px-3 py-2 font-medium">
                Gate
              </th>
              <th scope="col" className="px-3 py-2 font-medium">
                Risk
              </th>
              <th scope="col" className="px-3 py-2 font-medium">
                Requested
              </th>
              <th scope="col" className="px-3 py-2 font-medium">
                Decided
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {detail.revisions.map((r) => (
              <tr
                key={r.revision}
                data-testid="revision-row"
                aria-current={r.revision === current ? "true" : undefined}
              >
                <td className="px-3 py-2">
                  {r.revision === current ? (
                    <span className="font-medium">Revision {r.revision}</span>
                  ) : (
                    <button
                      type="button"
                      className="text-indigo-700 hover:underline"
                      onClick={() => onPick(r.revision)}
                    >
                      Revision {r.revision}
                    </button>
                  )}
                  {r.revision === latest ? (
                    <span className="ml-1 text-xs text-slate-500">(latest)</span>
                  ) : null}
                </td>
                <td className="px-3 py-2">
                  <GateOutcomeBadge gate={r} />
                </td>
                <td className="px-3 py-2 tabular-nums">{r.risk_index ?? "—"}</td>
                <td className="px-3 py-2 text-slate-700">
                  {formatDateTime(r.requested_at)} · {actorLabel(r.requested_by, me?.user?.id)}
                </td>
                <td className="px-3 py-2 text-slate-700">
                  {formatDateTime(r.decided_at)}
                  {r.eval_run_id ? (
                    <span className="ml-1 text-xs text-slate-400">run {shortId(r.eval_run_id)}</span>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}
