"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { RotateCcw, Square } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { PageHeader } from "@/components/common/page-header";
import { useCan, useMe } from "@/components/shell/me-context";
import { RunStatusBadge, SeverityBadge } from "@/components/simulations/badges";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { CopyButton } from "@/components/ui/copy-button";
import { KeyValue } from "@/components/ui/key-value";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { ApiError, api } from "@/lib/api";
import type {
  BodyOf,
  ClassCounts,
  EvalCaseSummary,
  EvalRun,
  EvalRunDetail,
  EvalRunResponse,
  EvalRunSummary,
} from "@/lib/api/evaluation";
import {
  CLASSIFICATIONS,
  CLASSIFICATION_LABEL,
  SIDE_TOTALS,
  asClassification,
  budgetLabel,
  countsSummary,
  evalRunVerdict,
  formatTotal,
  judgeLabel,
  sortComparedCases,
  suiteLabel,
  totalChange,
} from "@/lib/evaluations";
import { formatDateTime, formatDuration, humanize } from "@/lib/format";
import { actorLabel, isRunActive, isRunCancellable, runDurationMs } from "@/lib/simulations";
import { useActionKey } from "@/lib/use-action-key";
import { useUrlQuery } from "@/lib/use-url-query";
import { cn } from "@/lib/utils";
import { CalibratedBadge, ClassificationBadge, ReviewedBadge, SideStatusBadge } from "./badges";

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

const VERDICT_STYLE = {
  danger: "border-rose-200 bg-rose-50 text-rose-900",
  warning: "border-amber-200 bg-amber-50 text-amber-900",
  success: "border-emerald-200 bg-emerald-50 text-emerald-900",
  info: "border-sky-200 bg-sky-50 text-sky-900",
  neutral: "border-slate-200 bg-slate-50 text-slate-800",
} as const;

const TILE_STYLE: Record<keyof ClassCounts, string> = {
  NEW_CRITICAL_FAILURE: "border-rose-300 bg-rose-50 text-rose-900",
  REGRESSED: "border-rose-200 bg-rose-50/60 text-rose-900",
  INCOMPLETE: "border-amber-200 bg-amber-50 text-amber-900",
  IMPROVED: "border-emerald-200 bg-emerald-50 text-emerald-900",
  UNCHANGED: "border-slate-200 bg-white text-slate-800",
};

function caseHref(runId: string, scenario: string): string {
  return `/evaluations/${runId}/cases/${encodeURIComponent(scenario)}`;
}

/** The five counts as filters for the cases table. */
function CountTiles({
  counts,
  active,
  onPick,
}: {
  counts: ClassCounts;
  active: string;
  onPick: (value: string) => void;
}) {
  return (
    <div className="grid grid-cols-2 gap-2 sm:grid-cols-5" role="group" aria-label="Cases by classification">
      {CLASSIFICATIONS.map((c) => {
        const on = active === c;
        return (
          <button
            key={c}
            type="button"
            aria-pressed={on}
            onClick={() => onPick(on ? "" : c)}
            data-testid="count-tile"
            data-classification={c}
            className={cn(
              "rounded-md border px-3 py-2 text-left transition-shadow hover:shadow-sm focus-visible:outline-2 focus-visible:outline-indigo-600",
              TILE_STYLE[c],
              on && "ring-2 ring-indigo-500",
              counts[c] === 0 && "opacity-60",
            )}
          >
            <span className="block text-2xl font-semibold tabular-nums">{counts[c]}</span>
            <span className="block text-xs">{CLASSIFICATION_LABEL[c]}</span>
          </button>
        );
      })}
    </div>
  );
}

function Findings({ runId, summary }: { runId: string; summary: EvalRunSummary }) {
  const groups: { title: string; tone: "danger" | "warning" | "success"; names: string[] }[] = [
    { title: "Regressed", tone: "danger", names: summary.regressed },
    { title: "Could not be compared", tone: "warning", names: summary.incomplete },
    { title: "Improved", tone: "success", names: summary.improved },
  ];
  if (!summary.new_critical_failures.length && groups.every((g) => g.names.length === 0)) {
    return <p className="text-sm text-slate-600">Every case ends the same on both sides.</p>;
  }
  return (
    <div className="space-y-3 text-sm">
      {summary.new_critical_failures.length ? (
        <div data-testid="new-critical-failures">
          <h3 className="text-xs font-semibold uppercase tracking-wide text-rose-800">
            New critical failures
          </h3>
          <ul className="mt-1 space-y-1">
            {summary.new_critical_failures.map((f) => (
              <li key={f.scenario_name} className="flex flex-wrap items-center gap-2">
                <Link
                  href={caseHref(runId, f.scenario_name)}
                  className="font-medium text-indigo-700 hover:underline"
                >
                  {f.scenario_name}
                </Link>
                {f.expectations.map((e) => (
                  <Badge key={e} tone="danger">
                    {e}
                  </Badge>
                ))}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {groups
        .filter((g) => g.names.length)
        .map((g) => (
          <div key={g.title}>
            <h3
              className={cn(
                "text-xs font-semibold uppercase tracking-wide",
                g.tone === "danger"
                  ? "text-rose-800"
                  : g.tone === "warning"
                    ? "text-amber-900"
                    : "text-emerald-800",
              )}
            >
              {g.title}
            </h3>
            <ul className="mt-1 flex flex-wrap gap-x-3 gap-y-1">
              {g.names.map((n) => (
                <li key={n}>
                  <Link href={caseHref(runId, n)} className="text-indigo-700 hover:underline">
                    {n}
                  </Link>
                </li>
              ))}
            </ul>
          </div>
        ))}
    </div>
  );
}

const CHANGE_TEXT = {
  worse: "text-rose-700 font-medium",
  better: "text-emerald-700 font-medium",
  changed: "text-sky-800",
  same: "text-slate-700",
  unknown: "text-slate-500",
} as const;

function SideTotalsTable({ run, summary }: { run: EvalRun; summary: EvalRunSummary }) {
  return (
    <table className="w-full text-left text-sm">
      <caption className="sr-only">Totals of each side</caption>
      <thead className="border-b border-slate-100 text-xs uppercase tracking-wide text-slate-500">
        <tr>
          <th scope="col" className="px-3 py-2 font-medium">
            Metric
          </th>
          <th scope="col" className="px-3 py-2 text-right font-medium">
            Baseline v{run.baseline_version}
          </th>
          <th scope="col" className="px-3 py-2 text-right font-medium">
            Candidate v{run.candidate_version}
          </th>
        </tr>
      </thead>
      <tbody className="divide-y divide-slate-100">
        {SIDE_TOTALS.map((t) => {
          const change = totalChange(t.name, summary.baseline, summary.candidate);
          return (
            <tr key={t.name} data-testid="side-total" data-metric={t.name} data-change={change}>
              <th scope="row" className="px-3 py-1.5 font-normal text-slate-700">
                {t.label}
              </th>
              <td className="px-3 py-1.5 text-right tabular-nums text-slate-700">
                {formatTotal(summary.baseline, t.name)}
              </td>
              <td className={cn("px-3 py-1.5 text-right tabular-nums", CHANGE_TEXT[change])}>
                {formatTotal(summary.candidate, t.name)}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

const SLICE_TITLES: Record<string, string> = {
  severity: "Severity",
  risk: "Riskiest tool",
  tool: "Tool",
  tag: "Tag",
};

/** Where the candidate breaks: the slices with a case that is not unchanged. */
function Slices({ summary }: { summary: EvalRunSummary }) {
  const rows: { kind: string; key: string; counts: ClassCounts }[] = [];
  for (const kind of ["severity", "risk", "tool", "tag"] as const) {
    for (const [key, counts] of Object.entries(summary.slices[kind] ?? {})) {
      if (CLASSIFICATIONS.some((c) => c !== "UNCHANGED" && counts[c] > 0)) rows.push({ kind, key, counts });
    }
  }
  const failures = Object.entries(summary.slices.failure_class ?? {}).filter(
    ([, v]) => v.baseline !== v.candidate,
  );
  if (!rows.length && !failures.length) {
    return <p className="text-sm text-slate-600">No slice changed.</p>;
  }
  return (
    <div className="space-y-4">
      {rows.length ? (
        <table className="w-full text-left text-sm">
          <caption className="sr-only">Slices with a changed case</caption>
          <thead className="border-b border-slate-100 text-xs uppercase tracking-wide text-slate-500">
            <tr>
              <th scope="col" className="px-3 py-2 font-medium">
                Slice
              </th>
              <th scope="col" className="px-3 py-2 font-medium">
                Cases
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {rows.map((r) => (
              <tr key={`${r.kind}:${r.key}`} data-testid="slice" data-slice={`${r.kind}:${r.key}`}>
                <th scope="row" className="px-3 py-1.5 font-normal">
                  <span className="text-xs text-slate-500">{SLICE_TITLES[r.kind]}</span>{" "}
                  <span className="font-medium text-slate-900">{r.key}</span>
                </th>
                <td className="px-3 py-1.5">
                  <div className="flex flex-wrap gap-1">
                    {CLASSIFICATIONS.filter((c) => r.counts[c] > 0).map((c) => (
                      <span key={c} className="text-xs text-slate-700">
                        <ClassificationBadge classification={c} /> {r.counts[c]}
                      </span>
                    ))}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
      {failures.length ? (
        <div>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-500">
            Failures by class (baseline → candidate)
          </h3>
          <ul className="mt-1 flex flex-wrap gap-2 text-sm">
            {failures.map(([label, v]) => (
              <li key={label} data-testid="failure-class" data-label={label}>
                <Badge tone={v.candidate > v.baseline ? "danger" : "success"}>
                  {humanize(label)}: {v.baseline} → {v.candidate}
                </Badge>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}

function CasesTable({ run, cases }: { run: EvalRun; cases: EvalCaseSummary[] }) {
  if (cases.length === 0) return <EmptyState title="No case matches" />;
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[56rem] text-left text-sm">
        <caption className="sr-only">Compared cases, worst first</caption>
        <thead className="border-b border-slate-100 text-xs uppercase tracking-wide text-slate-500">
          <tr>
            <th scope="col" className="px-3 py-2 font-medium">
              Scenario
            </th>
            <th scope="col" className="px-3 py-2 font-medium">
              Classification
            </th>
            <th scope="col" className="px-3 py-2 font-medium">
              Why
            </th>
            <th scope="col" className="px-3 py-2 font-medium">
              v{run.baseline_version}
            </th>
            <th scope="col" className="px-3 py-2 font-medium">
              v{run.candidate_version}
            </th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {cases.map((c) => (
            <tr
              key={c.scenario_name}
              className="align-top"
              data-testid="compared-case"
              data-scenario={c.scenario_name}
              data-classification={c.classification}
            >
              <td className="px-3 py-2.5">
                <Link
                  href={caseHref(run.id, c.scenario_name)}
                  className="font-medium text-indigo-700 hover:underline"
                >
                  {c.scenario_name}
                </Link>
                <div className="mt-1 flex flex-wrap gap-1">
                  <SeverityBadge severity={c.severity} />
                  {c.reviewed ? <ReviewedBadge /> : null}
                </div>
              </td>
              <td className="px-3 py-2.5">
                <ClassificationBadge classification={c.classification} />
              </td>
              <td className="max-w-xl px-3 py-2.5 text-xs text-slate-700">{c.reason}</td>
              <td className="px-3 py-2.5">
                <SideStatusBadge status={c.baseline.status} />
              </td>
              <td className="px-3 py-2.5">
                <SideStatusBadge status={c.candidate.status} />
                {c.candidate.labels.length ? (
                  <div className="mt-1 text-xs text-rose-800">
                    {c.candidate.labels.map(humanize).join(", ")}
                  </div>
                ) : null}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function EvalRunDetailView({ runId }: { runId: string }) {
  const qc = useQueryClient();
  const router = useRouter();
  const canRun = useCan("eval.run");
  const me = useMe();
  const { params, update } = useUrlQuery();
  const filter = asClassification(params.get("class") ?? "") ?? "";
  const detail = useQuery({
    queryKey: ["eval-run", runId],
    queryFn: ({ signal }) => api<EvalRunDetail>(`/eval-runs/${runId}`, { signal }),
    refetchInterval: (q) =>
      q.state.data && isRunActive(q.state.data.run.status) ? ACTIVE_INTERVAL_MS : false,
  });
  const run = detail.data?.run;
  const active = run ? isRunActive(run.status) : false;
  const now = useTicker(active);
  const cancelKey = useActionKey("eval-cancel");
  const againKey = useActionKey("eval-again");

  const cancel = useMutation({
    mutationFn: () =>
      api<EvalRunResponse>(`/eval-runs/${runId}/cancel`, { method: "POST", idempotencyKey: cancelKey.key }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["eval-run", runId] }),
    onSettled: (_data, error) => cancelKey.settle(error),
  });
  const again = useMutation({
    mutationFn: () => {
      if (!run) throw new Error("run not loaded");
      const s = run.selection;
      const body: BodyOf<"startEvalRun"> = {
        project_id: run.project_id,
        agent: run.agent_name,
        baseline_version: run.baseline_version,
        candidate_version: run.candidate_version,
        ...(s.dataset
          ? { dataset_id: s.dataset.id, dataset_version: s.dataset.version }
          : { scenarios: s.scenarios, tags: s.tags }),
        ...((run.pinning?.seed ?? run.seed) != null ? { seed: run.pinning?.seed ?? run.seed } : {}),
      };
      return api<EvalRunResponse>("/eval-runs", { method: "POST", idempotencyKey: againKey.key, body });
    },
    onSuccess: (res) => router.push(`/evaluations/${res.run.id}`),
    onSettled: (_data, error) => againKey.settle(error),
  });

  if (detail.isPending) {
    return (
      <div className="space-y-4" aria-busy="true" aria-label="Loading evaluation">
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
        <EmptyState title="Evaluation not found">
          It does not exist or you do not have access to its project.{" "}
          <Link href="/evaluations" className="text-indigo-700 underline">
            Back to evaluations
          </Link>
        </EmptyState>
      );
    }
    return <ErrorState error={e} />;
  }

  const { summary, transitions } = detail.data;
  const r = detail.data.run;
  const verdict = evalRunVerdict(r);
  const sorted = sortComparedCases(detail.data.cases);
  const shown = filter ? sorted.filter((c) => c.classification === filter) : sorted;

  return (
    <div className="space-y-4">
      <PageHeader
        eyebrow={
          <span className="inline-flex items-center gap-1">
            <Link href="/evaluations" className="text-indigo-700 hover:underline">
              Evaluations
            </Link>
            <span aria-hidden="true">/</span>
            <code data-testid="eval-run-id">{r.id}</code>
            <CopyButton value={r.id} label="evaluation id" />
          </span>
        }
        title={
          <span className="inline-flex flex-wrap items-center gap-2">
            {r.agent_name}
            <Badge tone="neutral" title="Baseline">
              v{r.baseline_version}
            </Badge>
            <span aria-hidden="true" className="text-slate-400">
              →
            </span>
            <Badge tone="brand" title="Candidate">
              v{r.candidate_version}
            </Badge>
            <RunStatusBadge status={r.status} />
          </span>
        }
        description={
          <span data-testid="eval-summary">
            {r.status === "COMPLETED" ? countsSummary(r.counts) : `${r.case_count} cases`} ·{" "}
            {suiteLabel(r.selection)}
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
                  {cancel.isPending ? "Cancelling…" : "Cancel evaluation"}
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
        <ErrorState
          error={{ message: `The evaluation did not finish: ${r.error}`, code: "EVAL_RUN_FAILED" }}
        />
      ) : null}
      {verdict ? (
        <div
          role="status"
          data-testid="eval-verdict"
          data-tone={verdict.tone}
          className={cn("rounded-md border px-4 py-3 text-sm font-medium", VERDICT_STYLE[verdict.tone])}
        >
          {verdict.text}
        </div>
      ) : active ? (
        <div role="status" className={cn("rounded-md border px-4 py-3 text-sm", VERDICT_STYLE.info)}>
          {r.status === "EVALUATING"
            ? "Both simulations finished; comparing and judging the cases…"
            : "Running the baseline and the candidate on the same pinned scenarios…"}
        </div>
      ) : null}

      <div className="grid gap-4 xl:grid-cols-3">
        <Card className="xl:col-span-2">
          <CardHeader>
            <CardTitle>Result</CardTitle>
            <span className="text-xs text-slate-500">pick a count to filter the cases</span>
          </CardHeader>
          <CardContent className="space-y-4">
            <CountTiles counts={r.counts} active={filter} onPick={(v) => update({ class: v })} />
            {summary ? <Findings runId={r.id} summary={summary} /> : null}
          </CardContent>
        </Card>
        <Card data-testid="eval-pinning">
          <CardHeader>
            <CardTitle>Pinned inputs</CardTitle>
            <span className="text-xs text-slate-500">both sides, same seed</span>
          </CardHeader>
          <CardContent>
            <KeyValue
              items={[
                {
                  label: "Suite",
                  value: r.selection.dataset ? (
                    <Link
                      href={`/datasets/${r.selection.dataset.id}`}
                      className="text-indigo-700 hover:underline"
                    >
                      {suiteLabel(r.selection)}
                    </Link>
                  ) : (
                    suiteLabel(r.selection)
                  ),
                },
                {
                  label: "Seed",
                  value:
                    (r.pinning?.seed ?? r.seed) != null ? (
                      <code className="text-xs">{r.pinning?.seed ?? r.seed}</code>
                    ) : null,
                },
                {
                  label: "Baseline run",
                  value: r.baseline_run_id ? (
                    <Link
                      href={`/simulations/${r.baseline_run_id}`}
                      className="text-indigo-700 hover:underline"
                    >
                      v{r.baseline_version} simulation
                    </Link>
                  ) : null,
                },
                {
                  label: "Candidate run",
                  value: r.candidate_run_id ? (
                    <Link
                      href={`/simulations/${r.candidate_run_id}`}
                      className="text-indigo-700 hover:underline"
                    >
                      v{r.candidate_version} simulation
                    </Link>
                  ) : null,
                },
                {
                  label: "Judge",
                  value: r.judge ? (
                    <span className="inline-flex flex-wrap items-center gap-1">
                      <span className="text-xs">{judgeLabel(r.judge)}</span>
                      <CalibratedBadge calibrated={r.judge.calibrated === "true"} />
                    </span>
                  ) : null,
                },
                { label: "Judge budget", value: r.budget ? budgetLabel(r.budget) : null },
                { label: "Requested by", value: actorLabel(r.requested_by, me?.user?.id) },
                { label: "Created", value: formatDateTime(r.created_at) },
                { label: "Duration", value: formatDuration(runDurationMs(r, now)) },
                {
                  label: "Attempts",
                  value: r.attempts > 1 ? `${r.attempts} (a worker was lost)` : r.attempts,
                },
              ]}
            />
            {r.judge?.kind === "deterministic-fake" ? (
              <p className="mt-3 text-xs text-amber-900">
                Semantic expectations were graded by the deterministic keyword judge, not a language model.
              </p>
            ) : null}
          </CardContent>
        </Card>
      </div>

      {summary ? (
        <div className="grid gap-4 xl:grid-cols-2">
          <Card>
            <CardHeader>
              <CardTitle>Side by side</CardTitle>
              <span className="text-xs text-slate-500">unknown usage is not a zero</span>
            </CardHeader>
            <SideTotalsTable run={r} summary={summary} />
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>Where it changed</CardTitle>
            </CardHeader>
            <CardContent>
              <Slices summary={summary} />
            </CardContent>
          </Card>
        </div>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>Cases</CardTitle>
          <span className="text-xs text-slate-500">
            {filter ? (
              <>
                {CLASSIFICATION_LABEL[filter]} only ·{" "}
                <button
                  type="button"
                  className="text-indigo-700 underline"
                  onClick={() => update({ class: "" })}
                >
                  show all
                </button>
              </>
            ) : (
              "worst first"
            )}
          </span>
        </CardHeader>
        {detail.data.cases.length === 0 ? (
          <EmptyState
            title={active ? "The cases appear when both sides have finished" : "This evaluation has no cases"}
          />
        ) : (
          <CasesTable run={r} cases={shown} />
        )}
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Lifecycle</CardTitle>
        </CardHeader>
        <CardContent>
          <ol className="space-y-2 text-sm" aria-label="Status history">
            {transitions.map((t) => (
              <li key={`${t.at}-${t.to_status}`} className="flex flex-wrap items-baseline gap-x-2">
                <span className="w-44 shrink-0 font-mono text-xs text-slate-500">{formatDateTime(t.at)}</span>
                <span className="font-medium text-slate-900">{humanize(t.to_status)}</span>
                {t.reason ? <span className="text-slate-600">{t.reason}</span> : null}
              </li>
            ))}
          </ol>
        </CardContent>
      </Card>
    </div>
  );
}
