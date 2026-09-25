"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, RotateCcw, Square } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { PageHeader } from "@/components/common/page-header";
import { useCan, useMe } from "@/components/shell/me-context";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { CopyButton } from "@/components/ui/copy-button";
import { KeyValue } from "@/components/ui/key-value";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { ApiError, api } from "@/lib/api";
import { countOf, formatDateTime, formatDuration, formatPercent, humanize, shortId } from "@/lib/format";
import {
  actorLabel,
  isRunActive,
  isRunCancellable,
  passRate,
  runDurationMs,
  runVerdictSummary,
  sortCases,
} from "@/lib/simulations";
import type { SimulationCase, SimulationRun, SimulationRunDetail } from "@/lib/types";
import { useActionKey } from "@/lib/use-action-key";
import { CaseStatusBadge, FailureLabel, RunStatusBadge, SeverityBadge } from "./badges";
import { RunProgress } from "./run-progress";

const ACTIVE_INTERVAL_MS = 1_500;

function useTicker(active: boolean): Date {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    if (!active) return;
    const t = setInterval(() => setNow(new Date()), 1_000);
    return () => clearInterval(t);
  }, [active]);
  return now;
}

function CasesTable({ run, cases }: { run: SimulationRun; cases: SimulationCase[] }) {
  if (cases.length === 0) return <EmptyState title="This run has no scenarios" />;
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[52rem] text-left text-sm">
        <caption className="sr-only">Scenarios of this run, failures first</caption>
        <thead className="border-b border-slate-100 text-xs uppercase tracking-wide text-slate-500">
          <tr>
            <th scope="col" className="px-3 py-2 font-medium">
              Scenario
            </th>
            <th scope="col" className="px-3 py-2 font-medium">
              Verdict
            </th>
            <th scope="col" className="px-3 py-2 font-medium">
              Why
            </th>
            <th scope="col" className="px-3 py-2 text-right font-medium">
              Tool calls
            </th>
            <th scope="col" className="px-3 py-2 text-right font-medium">
              Duration
            </th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {sortCases(cases).map((c) => (
            <tr
              key={c.id}
              className="align-top"
              data-testid="case-row"
              data-scenario={c.scenario_name}
              data-status={c.status}
            >
              <td className="px-3 py-2.5">
                <Link
                  href={`/simulations/${run.id}/cases/${c.id}`}
                  className="font-medium text-indigo-700 hover:underline"
                >
                  {c.scenario_name}
                </Link>
                <div className="mt-1">
                  <SeverityBadge severity={c.severity} />
                </div>
              </td>
              <td className="px-3 py-2.5">
                <CaseStatusBadge status={c.status} />
              </td>
              <td className="max-w-xl px-3 py-2.5">
                {c.labels.length ? (
                  <div className="mb-1 flex flex-wrap gap-1">
                    {c.labels.map((l) => (
                      <FailureLabel key={l} label={l} />
                    ))}
                  </div>
                ) : null}
                <p className="text-xs text-slate-600">{c.error ?? c.reason ?? "—"}</p>
              </td>
              <td className="px-3 py-2.5 text-right tabular-nums">{c.call_count}</td>
              <td className="px-3 py-2.5 text-right tabular-nums">{formatDuration(c.latency_ms)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function SimulationDetail({ runId }: { runId: string }) {
  const qc = useQueryClient();
  const router = useRouter();
  const canRun = useCan("simulation.run");
  const me = useMe();
  const detail = useQuery({
    queryKey: ["simulation", runId],
    queryFn: ({ signal }) => api<SimulationRunDetail>(`/simulations/${runId}`, { signal }),
    refetchInterval: (q) =>
      q.state.data && isRunActive(q.state.data.run.status) ? ACTIVE_INTERVAL_MS : false,
  });
  const run = detail.data?.run;
  const active = run ? isRunActive(run.status) : false;
  const now = useTicker(active);
  const cancelKey = useActionKey("cancel");
  const rerunKey = useActionKey("rerun");

  const cancel = useMutation({
    mutationFn: () =>
      api<{ run: SimulationRun }>(`/simulations/${runId}/cancel`, {
        method: "POST",
        idempotencyKey: cancelKey.key,
      }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["simulation", runId] }),
    onSettled: (_data, error) => cancelKey.settle(error),
  });
  const again = useMutation({
    mutationFn: () => {
      if (!run) throw new Error("run not loaded");
      const scenarios =
        run.pinning?.scenarios?.map((s) => s.scenario) ?? detail.data?.cases.map((c) => c.scenario_name);
      return api<{ run: SimulationRun }>("/simulations", {
        method: "POST",
        idempotencyKey: rerunKey.key,
        body: {
          project_id: run.project_id,
          agent: run.agent_name,
          agent_version: run.agent_version,
          scenarios,
          seed: run.pinning?.seed,
        },
      });
    },
    onSuccess: (res) => router.push(`/simulations/${res.run.id}`),
    onSettled: (_data, error) => rerunKey.settle(error),
  });

  if (detail.isPending) {
    return (
      <div className="space-y-4" aria-busy="true" aria-label="Loading simulation">
        <Skeleton className="h-10 w-80" />
        <Skeleton className="h-40 w-full" />
        <Skeleton className="h-72 w-full" />
      </div>
    );
  }
  if (detail.isError) {
    const e = detail.error;
    if (e instanceof ApiError && e.status === 404) {
      return (
        <EmptyState title="Simulation not found">
          It does not exist or you do not have access to its project.{" "}
          <Link href="/simulations" className="text-indigo-700 underline">
            Back to simulations
          </Link>
        </EmptyState>
      );
    }
    return <ErrorState error={e} />;
  }

  const { cases, transitions } = detail.data;
  const r = detail.data.run;
  const pin = r.pinning ?? {};
  const rate = passRate(r);
  const evaluators = Object.entries(pin.evaluators ?? {});

  return (
    <div className="space-y-4">
      <PageHeader
        eyebrow={
          <span className="inline-flex items-center gap-1">
            <Link href="/simulations" className="text-indigo-700 hover:underline">
              Simulations
            </Link>
            <span aria-hidden="true">/</span>
            <code data-testid="run-id">{r.id}</code>
            <CopyButton value={r.id} label="run id" />
          </span>
        }
        title={
          <span className="inline-flex flex-wrap items-center gap-2">
            {r.agent_name}
            <Badge tone="brand">v{r.agent_version}</Badge>
            <RunStatusBadge status={r.status} />
          </span>
        }
        description={
          <span data-testid="run-summary">
            {runVerdictSummary(r)} of {countOf(r.case_count, "scenario")}
            {r.critical_failures > 0 ? ` · ${countOf(r.critical_failures, "critical scenario")} failed` : ""}
            {r.cancel_requested && active ? " · cancelling…" : ""}
          </span>
        }
        actions={
          canRun ? (
            <>
              {isRunCancellable(r) ? (
                <Button
                  size="sm"
                  variant="danger"
                  onClick={() => cancel.mutate()}
                  disabled={cancel.isPending}
                >
                  <Square className="h-4 w-4" aria-hidden="true" />
                  {cancel.isPending ? "Cancelling…" : "Cancel run"}
                </Button>
              ) : null}
              {!active ? (
                <Button size="sm" onClick={() => again.mutate()} disabled={again.isPending}>
                  <RotateCcw className="h-4 w-4" aria-hidden="true" />
                  {again.isPending ? "Starting…" : "Run again"}
                </Button>
              ) : null}
            </>
          ) : null
        }
      />
      {cancel.isError ? <ErrorState error={cancel.error} /> : null}
      {again.isError ? <ErrorState error={again.error} /> : null}
      {r.error ? (
        <ErrorState error={{ message: `The run did not finish: ${r.error}`, code: "RUN_FAILED" }} />
      ) : null}

      <div className="grid gap-4 xl:grid-cols-3">
        <Card className="xl:col-span-2">
          <CardHeader>
            <CardTitle>Result</CardTitle>
            <span className="text-xs text-slate-500">
              {rate === null ? "not evaluated yet" : `${formatPercent(rate, 0)} passed`}
            </span>
          </CardHeader>
          <CardContent className="space-y-4">
            <RunProgress run={r} />
            <KeyValue
              items={[
                { label: "Requested by", value: actorLabel(r.requested_by, me?.user.id) },
                { label: "Created", value: formatDateTime(r.created_at) },
                { label: "Duration", value: formatDuration(runDurationMs(r, now)) },
                {
                  // Failed scenarios of critical severity (a case counts failed critical expectations).
                  label: "Critical scenarios failed",
                  value:
                    r.critical_failures > 0 ? <Badge tone="danger">{r.critical_failures}</Badge> : "none",
                },
                {
                  label: "Attempts",
                  value: r.attempts > 1 ? `${r.attempts} (a worker was lost)` : r.attempts,
                },
              ]}
            />
          </CardContent>
        </Card>
        <Card data-testid="pinning-card">
          <CardHeader>
            <CardTitle>Pinned inputs</CardTitle>
            <span className="text-xs text-slate-500">reproducible with the same seed</span>
          </CardHeader>
          <CardContent>
            <KeyValue
              items={[
                {
                  label: "Seed",
                  value: pin.seed !== undefined ? <code className="text-xs">{pin.seed}</code> : null,
                },
                { label: "Engine", value: pin.engine },
                { label: "Model", value: pin.agent?.model_name },
                {
                  label: "Manifest",
                  value: pin.agent?.manifest_sha256 ? (
                    <code className="text-xs" title={pin.agent.manifest_sha256}>
                      {shortId(pin.agent.manifest_sha256, 12)}
                    </code>
                  ) : null,
                },
                {
                  label: "Prompt",
                  value: pin.agent?.prompt_sha256 ? (
                    <code className="text-xs" title={pin.agent.prompt_sha256}>
                      {shortId(pin.agent.prompt_sha256, 12)}
                    </code>
                  ) : null,
                },
                { label: "Commit", value: pin.agent?.commit_sha ? shortId(pin.agent.commit_sha, 12) : null },
                { label: "Evaluators", value: evaluators.length ? `${evaluators.length} pinned` : null },
              ]}
            />
            {pin.scenarios?.length ? (
              <details className="mt-3 text-xs">
                <summary className="cursor-pointer text-slate-700">
                  {pin.scenarios.length} scenario versions and twins
                </summary>
                <ul className="mt-2 space-y-1">
                  {pin.scenarios.map((s) => (
                    <li
                      key={s.scenario_version_id}
                      className="flex flex-wrap items-center gap-x-2 text-slate-600"
                    >
                      <span className="font-medium text-slate-800">{s.scenario}</span>
                      <code title={s.spec_hash}>{shortId(s.spec_hash, 10)}</code>
                      <span>
                        {s.twin} v{s.twin_version}
                      </span>
                      <span>seed {s.seed}</span>
                    </li>
                  ))}
                </ul>
              </details>
            ) : null}
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Scenarios</CardTitle>
          <span className="text-xs text-slate-500">failures first</span>
        </CardHeader>
        <CasesTable run={r} cases={cases} />
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Lifecycle</CardTitle>
        </CardHeader>
        <CardContent>
          <ol className="space-y-2 text-sm" aria-label="Status history">
            {transitions.map((t) => (
              <li key={t.seq} className="flex flex-wrap items-baseline gap-x-2">
                <span className="w-44 shrink-0 font-mono text-xs text-slate-500">{formatDateTime(t.at)}</span>
                <span className="font-medium text-slate-900">{humanize(t.to_status)}</span>
                {t.reason ? <span className="text-slate-600">{t.reason}</span> : null}
              </li>
            ))}
          </ol>
          {cases.some((c) => c.trace_id) ? (
            <p className="mt-3 text-xs text-slate-500">
              <ExternalLink className="mr-1 inline h-3 w-3" aria-hidden="true" />
              Every scenario run is also a trace: open a scenario to jump to its trace.
            </p>
          ) : null}
        </CardContent>
      </Card>
    </div>
  );
}
