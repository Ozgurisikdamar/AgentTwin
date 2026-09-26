"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, ExternalLink, Scale, UserCheck } from "lucide-react";
import Link from "next/link";
import { type FormEvent, useId, useState } from "react";
import { PageHeader } from "@/components/common/page-header";
import { useCan, useMe } from "@/components/shell/me-context";
import { FailureLabel, ResultBadge, SeverityBadge } from "@/components/simulations/badges";
import { RiskBadge } from "@/components/traces/badges";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { ApiError, api } from "@/lib/api";
import type {
  CaseComparison,
  Divergence,
  EvalCaseDetail,
  EvalExpectationResult,
  EvalRunDetail,
  ExpectationChange,
  JudgedExpectation,
  ReviewRequest,
  ReviewResponse,
  ReviewSide,
  SideSummary,
  TrajectoryStep,
} from "@/lib/api/evaluation";
import {
  CLASSIFICATION_LABEL,
  METRICS,
  alignSteps,
  formatDelta,
  formatMetric,
  isHumanReviewed,
  isReviewable,
  judgeLabel,
  latestReviews,
  pendingReviews,
  reviewKey,
} from "@/lib/evaluations";
import { formatDateTime, humanize } from "@/lib/format";
import { actorLabel, formatValue } from "@/lib/simulations";
import { useActionKey } from "@/lib/use-action-key";
import { cn } from "@/lib/utils";
import {
  CalibratedBadge,
  ClassificationBadge,
  ExpectationChangeBadge,
  NeedsReviewBadge,
  ReviewedBadge,
  SideStatusBadge,
  metricTone,
} from "./badges";

const SIDE_KEY: Record<ReviewSide, "baseline" | "candidate"> = {
  BASELINE: "baseline",
  CANDIDATE: "candidate",
};

function ResultCell({ status }: { status: string | null }) {
  if (!status) return <span className="text-xs text-slate-500">absent</span>;
  return <ResultBadge status={status as EvalExpectationResult["status"]} />;
}

function StepLine({ step }: { step: TrajectoryStep | null }) {
  if (!step) return <span className="text-xs text-slate-500">—</span>;
  return (
    <div className="space-y-1">
      <div className="flex items-baseline gap-2">
        <span className="font-mono text-xs text-slate-500">{step.index}</span>
        <code className="break-all text-xs text-slate-900">{step.label}</code>
      </div>
      <div className="flex flex-wrap gap-1 pl-5">
        {step.kind !== "TOOL" ? <Badge tone="info">{step.kind.toLowerCase()}</Badge> : null}
        <RiskBadge risk={step.risk} />
        {step.status && step.status !== "ok" ? <Badge tone="danger">{humanize(step.status)}</Badge> : null}
        {step.fault ? <Badge tone="warning">fault: {humanize(step.fault).toLowerCase()}</Badge> : null}
        {step.effect === "replayed" ? <Badge tone="info">replayed</Badge> : null}
      </div>
    </div>
  );
}

function DivergenceCard({ divergence, versions }: { divergence: Divergence; versions: [string, string] }) {
  return (
    <Card data-testid="divergence" data-kind={divergence.kind}>
      <CardHeader>
        <CardTitle>First divergence</CardTitle>
        <span className="text-xs text-slate-500">step {divergence.index}</span>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        <p className="text-slate-900">{divergence.summary}</p>
        {divergence.impact ? (
          <p
            className="rounded-md border border-rose-200 bg-rose-50 px-3 py-2 text-rose-900"
            data-testid="impact"
          >
            {divergence.impact}
          </p>
        ) : null}
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
          <div>
            <p className="mb-1 text-xs font-medium text-slate-500">Baseline v{versions[0]}</p>
            <StepLine step={divergence.baseline} />
          </div>
          <div>
            <p className="mb-1 text-xs font-medium text-slate-500">Candidate v{versions[1]}</p>
            <StepLine step={divergence.candidate} />
          </div>
        </div>
        {divergence.argument_changes.length ? (
          <ul className="space-y-0.5 text-xs text-slate-700" aria-label="Argument changes">
            {divergence.argument_changes.map((c) => (
              <li key={c.path}>
                <code>{c.path}</code> {c.change}:{" "}
                {"baseline" in c ? <code>{formatValue(c.baseline, 60)}</code> : null}
                {"baseline" in c && "candidate" in c ? " → " : null}
                {"candidate" in c ? <code>{formatValue(c.candidate, 60)}</code> : null}
              </li>
            ))}
          </ul>
        ) : null}
      </CardContent>
    </Card>
  );
}

function ExpectationsTable({
  changes,
  detail,
  canReview,
  onReview,
}: {
  changes: ExpectationChange[];
  detail: EvalCaseDetail;
  canReview: boolean;
  onReview: (side: ReviewSide, expectationId: string) => void;
}) {
  const pending = new Set(pendingReviews(detail));
  const reviews = latestReviews(detail.reviews);
  const resultOf = (side: ReviewSide, id: string) =>
    detail.results[SIDE_KEY[side]].find((r) => r.expectation.id === id);
  if (changes.length === 0) return <EmptyState title="No expectation was compared" />;
  return (
    <div className="relative overflow-x-auto">
      <table className="w-full min-w-[56rem] text-left text-sm">
        <caption className="sr-only">Expectations on both sides</caption>
        <thead className="border-b border-slate-100 text-xs uppercase tracking-wide text-slate-500">
          <tr>
            <th scope="col" className="px-3 py-2 font-medium">
              Expectation
            </th>
            <th scope="col" className="px-3 py-2 font-medium">
              Change
            </th>
            {(["BASELINE", "CANDIDATE"] as const).map((side) => (
              <th key={side} scope="col" className="px-3 py-2 font-medium">
                {humanize(side)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {changes.map((e) => (
            <tr
              key={e.id}
              className="align-top"
              data-testid="expectation-change"
              data-expectation={e.id}
              data-change={e.change}
            >
              <td className="px-3 py-2.5">
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="font-medium text-slate-900">{e.id}</span>
                  {e.critical ? <Badge tone="danger">critical</Badge> : null}
                </div>
                <code className="text-xs text-slate-500">{e.type}</code>
                {e.label && e.change === "broken" ? (
                  <div className="mt-1">
                    <FailureLabel label={e.label} />
                  </div>
                ) : null}
              </td>
              <td className="px-3 py-2.5">
                <ExpectationChangeBadge change={e.change} />
                {e.baseline_unverified ? (
                  <p className="mt-1 text-xs text-amber-900">the baseline could not evaluate it</p>
                ) : null}
              </td>
              {(["BASELINE", "CANDIDATE"] as const).map((side) => {
                const key = reviewKey(side, e.id);
                const result = resultOf(side, e.id);
                const status = side === "BASELINE" ? e.baseline : e.candidate;
                const reason = side === "BASELINE" ? e.baseline_reason : e.candidate_reason;
                const score = side === "BASELINE" ? e.baseline_score : e.candidate_score;
                const review = reviews.get(key);
                return (
                  <td key={side} className="max-w-md px-3 py-2.5" data-side={side} data-status={status ?? ""}>
                    <div className="flex flex-wrap items-center gap-1">
                      <ResultCell status={status} />
                      {score !== undefined ? (
                        <span className="text-xs text-slate-500">score {score.toFixed(2)}</span>
                      ) : null}
                      {result && isHumanReviewed(result) ? (
                        <ReviewedBadge
                          title={review ? `${review.status} by ${review.reviewer}` : undefined}
                        />
                      ) : null}
                      {pending.has(key) ? <NeedsReviewBadge /> : null}
                    </div>
                    {reason ? <p className="mt-1 text-xs text-slate-600">{reason}</p> : null}
                    {canReview && status && result && isReviewable(result) ? (
                      <Button
                        size="sm"
                        variant="ghost"
                        className="mt-1 h-7 px-2 text-xs"
                        onClick={() => onReview(side, e.id)}
                        aria-label={`Review ${e.id} on the ${side.toLowerCase()}`}
                      >
                        <UserCheck className="h-3.5 w-3.5" aria-hidden="true" />
                        Review
                      </Button>
                    ) : null}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ReviewForm({
  runId,
  scenario,
  side,
  expectationId,
  current,
  onClose,
}: {
  runId: string;
  scenario: string;
  side: ReviewSide;
  expectationId: string;
  current: EvalExpectationResult | undefined;
  onClose: (result?: ReviewResponse) => void;
}) {
  const qc = useQueryClient();
  const id = useId();
  const [status, setStatus] = useState<ReviewRequest["status"]>(current?.status === "PASS" ? "FAIL" : "PASS");
  const [note, setNote] = useState("");
  const key = useActionKey("review");
  const submit = useMutation({
    mutationFn: () =>
      api<ReviewResponse>(`/eval-runs/${runId}/cases/${encodeURIComponent(scenario)}/reviews`, {
        method: "POST",
        idempotencyKey: key.key,
        body: { side, expectation_id: expectationId, status, note: note.trim() } satisfies ReviewRequest,
      }),
    onSuccess: (res) => {
      void qc.invalidateQueries({ queryKey: ["eval-case", runId, scenario] });
      void qc.invalidateQueries({ queryKey: ["eval-run", runId] });
      void qc.invalidateQueries({ queryKey: ["review-queue"] });
      // The case may be classified again: the run list's counts and the
      // dataset's latest results move with it.
      void qc.invalidateQueries({ queryKey: ["eval-runs"] });
      void qc.invalidateQueries({ queryKey: ["dataset"] });
      onClose(res);
    },
    onSettled: (_data, error) => key.settle(error),
  });
  const valid = note.trim().length > 0 && note.length <= 2000;

  function send(e: FormEvent) {
    e.preventDefault();
    if (valid && !submit.isPending) submit.mutate();
  }

  return (
    <Card className="border-indigo-200" data-testid="review-form">
      <form onSubmit={send} aria-label={`Review ${expectationId}`}>
        <CardHeader>
          <CardTitle>
            Review <code>{expectationId}</code> on the {side.toLowerCase()}
          </CardTitle>
          <span className="text-xs text-slate-500">
            now {current?.status ?? "absent"} by {current?.evaluator ?? "—"}
          </span>
        </CardHeader>
        <CardContent className="space-y-3">
          {current?.reason ? <p className="text-sm text-slate-700">{current.reason}</p> : null}
          <fieldset>
            <legend className="mb-1 text-xs font-medium text-slate-600">Your verdict</legend>
            <div className="flex gap-4 text-sm">
              {(["PASS", "FAIL"] as const).map((s) => (
                <label key={s} className="flex items-center gap-1.5">
                  <input
                    type="radio"
                    name={`${id}-status`}
                    className="accent-indigo-600"
                    checked={status === s}
                    onChange={() => setStatus(s)}
                  />
                  {s === "PASS" ? "Pass" : "Fail"}
                </label>
              ))}
            </div>
          </fieldset>
          <div>
            <Label htmlFor={`${id}-note`}>Why (kept with the review and in the audit log)</Label>
            <textarea
              id={`${id}-note`}
              required
              maxLength={2000}
              rows={3}
              value={note}
              onChange={(e) => setNote(e.target.value)}
              className="w-full rounded-md border border-slate-300 bg-white px-2.5 py-2 text-sm text-slate-900 focus-visible:border-indigo-500 focus-visible:outline-2 focus-visible:outline-indigo-200"
            />
          </div>
          <p className="text-xs text-slate-500">
            The review replaces this result and the case is classified again. Reviews are kept; a later review
            of the same expectation replaces this one.
          </p>
          {submit.isError ? <ErrorState error={submit.error} /> : null}
          <div className="flex gap-2">
            <Button type="submit" variant="primary" size="sm" disabled={!valid || submit.isPending}>
              {submit.isPending ? "Saving…" : "Save review"}
            </Button>
            <Button type="button" variant="ghost" size="sm" onClick={() => onClose()}>
              Cancel
            </Button>
          </div>
        </CardContent>
      </form>
    </Card>
  );
}

function MetricsTable({ comparison }: { comparison: CaseComparison }) {
  return (
    <div className="relative overflow-x-auto">
      <table className="w-full text-left text-sm">
        <caption className="sr-only">Metrics of both sides</caption>
        <thead className="border-b border-slate-100 text-xs uppercase tracking-wide text-slate-500">
          <tr>
            <th scope="col" className="px-3 py-2 font-medium">
              Metric
            </th>
            <th scope="col" className="px-3 py-2 text-right font-medium">
              Baseline
            </th>
            <th scope="col" className="px-3 py-2 text-right font-medium">
              Candidate
            </th>
            <th scope="col" className="px-3 py-2 text-right font-medium">
              Δ
            </th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {METRICS.map((m) => {
            const d = comparison.metrics[m.name];
            if (!d) return null;
            return (
              <tr key={m.name} data-testid="metric" data-metric={m.name} data-change={d.change}>
                <th scope="row" className="px-3 py-1.5 font-normal text-slate-700">
                  {m.label}
                </th>
                <td className="px-3 py-1.5 text-right tabular-nums">{formatMetric(m.name, d.baseline)}</td>
                <td className="px-3 py-1.5 text-right tabular-nums">{formatMetric(m.name, d.candidate)}</td>
                <td className={cn("px-3 py-1.5 text-right tabular-nums", metricTone(d.change))}>
                  {formatDelta(m.name, d.delta)}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

const MATCH_STYLE: Record<string, string> = {
  same: "",
  changed: "bg-amber-50",
  baseline_only: "bg-rose-50/60",
  candidate_only: "bg-sky-50",
};

const MATCH_LABEL: Record<string, string> = {
  same: "same",
  changed: "changed",
  baseline_only: "baseline only",
  candidate_only: "candidate only",
};

function AlignedTrajectories({
  comparison,
  versions,
  divergenceIndex,
}: {
  comparison: CaseComparison;
  versions: [string, string];
  divergenceIndex: number | null;
}) {
  const rows = alignSteps(comparison.trajectories);
  if (rows.length === 0) return <EmptyState title="Neither side called a tool" />;
  return (
    <div className="relative overflow-x-auto">
      <table className="w-full min-w-[48rem] text-left text-sm">
        <caption className="sr-only">Both trajectories, step by step as aligned</caption>
        <thead className="border-b border-slate-100 text-xs uppercase tracking-wide text-slate-500">
          <tr>
            <th scope="col" className="w-1/2 px-3 py-2 font-medium">
              Baseline v{versions[0]}
            </th>
            <th scope="col" className="px-3 py-2 font-medium">
              <span className="sr-only">Match</span>
            </th>
            <th scope="col" className="w-1/2 px-3 py-2 font-medium">
              Candidate v{versions[1]}
            </th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {rows.map((row, i) => {
            const first =
              divergenceIndex !== null &&
              (row.baseline?.index === divergenceIndex || row.candidate?.index === divergenceIndex) &&
              row.match !== "same";
            return (
              <tr
                key={i}
                className={cn(
                  "align-top",
                  MATCH_STYLE[row.match],
                  first && "outline outline-2 outline-rose-300",
                )}
                data-testid="aligned-step"
                data-match={row.match}
              >
                <td className="px-3 py-2">
                  <StepLine step={row.baseline} />
                </td>
                <td className="px-1 py-2 text-center text-xs text-slate-500">
                  {row.match === "same" ? (
                    <ArrowRight className="mx-auto h-3.5 w-3.5" aria-label="same" />
                  ) : (
                    MATCH_LABEL[row.match]
                  )}
                </td>
                <td className="px-3 py-2">
                  <StepLine step={row.candidate} />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function StateChanges({ comparison }: { comparison: CaseComparison }) {
  const { added, removed } = comparison.tool_selection;
  return (
    <div className="space-y-3 text-sm">
      {added.length || removed.length ? (
        <div className="flex flex-wrap gap-1" data-testid="tool-selection">
          {added.map((t) => (
            <Badge key={`+${t}`} tone="info">
              + {t}
            </Badge>
          ))}
          {removed.map((t) => (
            <Badge key={`-${t}`} tone="warning">
              − {t}
            </Badge>
          ))}
        </div>
      ) : (
        <p className="text-slate-600">Both sides called the same tools.</p>
      )}
      {comparison.state_changes.length ? (
        <ul className="divide-y divide-slate-100" data-testid="state-changes">
          {comparison.state_changes.map((c) => (
            <li key={c.path} className="py-1.5" data-path={c.path} data-change={c.change}>
              <code className="text-xs font-medium text-slate-900">{c.path}</code>{" "}
              <span className="text-xs text-slate-500">{c.change}</span>
              <div className="mt-0.5 grid grid-cols-1 gap-1 text-xs md:grid-cols-2">
                <code className="break-all text-slate-600">
                  {"baseline" in c ? formatValue(c.baseline) : "—"}
                </code>
                <code className="break-all text-slate-900">
                  {"candidate" in c ? formatValue(c.candidate) : "—"}
                </code>
              </div>
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-slate-600">The final twin state is the same on both sides.</p>
      )}
    </div>
  );
}

function Verdicts({ verdicts, side }: { verdicts: JudgedExpectation[]; side: string }) {
  if (verdicts.length === 0) return null;
  return (
    <div>
      <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-500">{side}</h3>
      <ul className="mt-1 space-y-2">
        {verdicts.map((v) => (
          <li
            key={v.expectation_id}
            className="rounded-md border border-slate-100 p-2 text-sm"
            data-testid="verdict"
            data-expectation={v.expectation_id}
          >
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="font-medium text-slate-900">{v.expectation_id}</span>
              {v.verdict ? (
                <Badge tone={v.verdict.label === "pass" ? "success" : "danger"}>
                  {v.verdict.label} · {v.verdict.score.toFixed(2)}
                </Badge>
              ) : (
                <Badge tone="warning">not judged</Badge>
              )}
              {v.verdict ? (
                <span className="text-xs text-slate-500">confidence {v.verdict.confidence.toFixed(2)}</span>
              ) : null}
              {v.cached ? <Badge tone="neutral">reused</Badge> : null}
              <CalibratedBadge calibrated={v.judge.calibrated === "true"} />
            </div>
            {v.verdict ? <p className="mt-1 text-xs text-slate-700">{v.verdict.reason}</p> : null}
            {v.verdict?.evidence.length ? (
              <ul className="mt-1 space-y-0.5 text-xs" aria-label="Quoted evidence">
                {v.verdict.evidence.map((q, i) => (
                  <li key={i}>
                    <code className="text-slate-500">{q.ref}</code> “{q.quote}”
                  </li>
                ))}
              </ul>
            ) : null}
            {v.verdict && v.verdict.unsupported_quotes > 0 ? (
              <p className="mt-1 text-xs text-amber-900">
                {v.verdict.unsupported_quotes} quote(s) were not in the material and were dropped.
              </p>
            ) : null}
            <p className="mt-1 text-xs text-slate-500">{judgeLabel(v.judge)}</p>
          </li>
        ))}
      </ul>
    </div>
  );
}

function SideLinks({
  label,
  version,
  side,
  runId,
}: {
  label: string;
  version: string;
  side: SideSummary;
  runId: string | null | undefined;
}) {
  return (
    <div className="space-y-1 text-sm" data-testid="case-side" data-side={label} data-status={side.status}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs font-medium text-slate-500">
          {label} v{version}
        </span>
        <SideStatusBadge status={side.status} />
      </div>
      {side.reason ? <p className="text-xs text-slate-600">{side.reason}</p> : null}
      <div className="flex flex-wrap gap-3 text-xs">
        {runId && side.case_id ? (
          <Link
            href={`/simulations/${runId}/cases/${side.case_id}`}
            className="text-indigo-700 hover:underline"
          >
            Simulation case
          </Link>
        ) : null}
        {side.trace_id ? (
          <Link
            href={`/traces/${side.trace_id}`}
            className="inline-flex items-center gap-1 text-indigo-700 hover:underline"
          >
            <ExternalLink className="h-3 w-3" aria-hidden="true" />
            Trace
          </Link>
        ) : null}
      </div>
    </div>
  );
}

export function EvalCaseView({ runId, scenario }: { runId: string; scenario: string }) {
  const canReview = useCan("review.write");
  const me = useMe();
  const [reviewing, setReviewing] = useState<{ side: ReviewSide; id: string } | null>(null);
  const [outcome, setOutcome] = useState<ReviewResponse | null>(null);
  const run = useQuery({
    queryKey: ["eval-run", runId],
    queryFn: ({ signal }) => api<EvalRunDetail>(`/eval-runs/${runId}`, { signal }),
  });
  const detail = useQuery({
    queryKey: ["eval-case", runId, scenario],
    queryFn: ({ signal }) =>
      api<EvalCaseDetail>(`/eval-runs/${runId}/cases/${encodeURIComponent(scenario)}`, { signal }),
  });

  if (detail.isPending) {
    return (
      <div className="space-y-4" aria-busy="true" aria-label="Loading case">
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
        <EmptyState title="Case not found">
          The evaluation has no result for this scenario, or you do not have access to it.{" "}
          <Link href={`/evaluations/${runId}`} className="text-indigo-700 underline">
            Back to the evaluation
          </Link>
        </EmptyState>
      );
    }
    return <ErrorState error={e} />;
  }

  const d = detail.data;
  const c = d.comparison;
  const r = run.data?.run;
  const versions: [string, string] = [r?.baseline_version ?? "baseline", r?.candidate_version ?? "candidate"];
  const pending = pendingReviews(d);
  const reviewable = canReview && r?.status === "COMPLETED";
  const current = reviewing
    ? d.results[SIDE_KEY[reviewing.side]].find((x) => x.expectation.id === reviewing.id)
    : undefined;

  return (
    <div className="space-y-4">
      <PageHeader
        eyebrow={
          <span className="inline-flex flex-wrap items-center gap-1">
            <Link href="/evaluations" className="text-indigo-700 hover:underline">
              Evaluations
            </Link>
            <span aria-hidden="true">/</span>
            <Link href={`/evaluations/${runId}`} className="text-indigo-700 hover:underline">
              {r ? `${r.agent_name} v${r.baseline_version} → v${r.candidate_version}` : "evaluation"}
            </Link>
          </span>
        }
        title={
          <span className="inline-flex flex-wrap items-center gap-2">
            {c.scenario_name}
            <ClassificationBadge classification={c.classification} />
            <SeverityBadge severity={c.severity} />
            {d.reviewed ? <ReviewedBadge /> : null}
          </span>
        }
        description={<span data-testid="case-reason">{c.reason}</span>}
      />
      {outcome ? (
        <div
          role="status"
          data-testid="review-outcome"
          className="rounded-md border border-sky-200 bg-sky-50 px-4 py-3 text-sm text-sky-900"
        >
          Review saved.{" "}
          {outcome.previous_classification === outcome.classification
            ? `The case stays ${CLASSIFICATION_LABEL[outcome.classification].toLowerCase()}.`
            : `The case is now ${CLASSIFICATION_LABEL[outcome.classification].toLowerCase()} (was ${CLASSIFICATION_LABEL[outcome.previous_classification].toLowerCase()}).`}
        </div>
      ) : null}
      {pending.length ? (
        <div
          className="rounded-md border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900"
          role="status"
        >
          {pending.length === 1 ? "One result needs" : `${pending.length} results need`} a person: the judge
          could not grade it, or graded a critical expectation without being calibrated.
        </div>
      ) : null}

      <Card>
        <CardContent className="grid grid-cols-1 gap-4 pt-4 md:grid-cols-2">
          <SideLinks label="Baseline" version={versions[0]} side={c.baseline} runId={r?.baseline_run_id} />
          <SideLinks label="Candidate" version={versions[1]} side={c.candidate} runId={r?.candidate_run_id} />
        </CardContent>
      </Card>

      {c.divergence ? <DivergenceCard divergence={c.divergence} versions={versions} /> : null}

      {reviewing && reviewable ? (
        <ReviewForm
          key={`${reviewing.side}:${reviewing.id}`}
          runId={runId}
          scenario={scenario}
          side={reviewing.side}
          expectationId={reviewing.id}
          current={current}
          onClose={(res) => {
            setReviewing(null);
            if (res) setOutcome(res);
          }}
        />
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>Expectations</CardTitle>
          <span className="text-xs text-slate-500">baseline next to candidate</span>
        </CardHeader>
        <ExpectationsTable
          changes={c.expectations}
          detail={d}
          canReview={Boolean(reviewable)}
          onReview={(side, id) => {
            setOutcome(null);
            setReviewing({ side, id });
          }}
        />
      </Card>

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Metrics</CardTitle>
            <span className="text-xs text-slate-500">— is unknown, not zero</span>
          </CardHeader>
          <MetricsTable comparison={c} />
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Tools and final state</CardTitle>
            <RiskBadge risk={c.risk === "none" ? null : c.risk} />
          </CardHeader>
          <CardContent>
            <StateChanges comparison={c} />
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Trajectories</CardTitle>
          <span className="text-xs text-slate-500">aligned step by step</span>
        </CardHeader>
        <AlignedTrajectories
          comparison={c}
          versions={versions}
          divergenceIndex={c.divergence?.index ?? null}
        />
      </Card>

      {d.verdicts.baseline.length || d.verdicts.candidate.length ? (
        <Card>
          <CardHeader>
            <CardTitle>
              <span className="inline-flex items-center gap-1.5">
                <Scale className="h-4 w-4" aria-hidden="true" />
                Judge verdicts
              </span>
            </CardTitle>
            <Link href="/judges" className="text-xs text-indigo-700 hover:underline">
              Judge calibration
            </Link>
          </CardHeader>
          <CardContent className="grid grid-cols-1 gap-4 md:grid-cols-2">
            <Verdicts verdicts={d.verdicts.baseline} side={`Baseline v${versions[0]}`} />
            <Verdicts verdicts={d.verdicts.candidate} side={`Candidate v${versions[1]}`} />
          </CardContent>
        </Card>
      ) : null}

      {d.reviews.length ? (
        <Card>
          <CardHeader>
            <CardTitle>Reviews</CardTitle>
            <span className="text-xs text-slate-500">oldest first; the latest one counts</span>
          </CardHeader>
          <CardContent>
            <ol className="space-y-2 text-sm" aria-label="Reviews" data-testid="reviews">
              {d.reviews.map((v) => (
                <li key={v.id} className="flex flex-wrap items-baseline gap-x-2" data-testid="review">
                  <span className="w-44 shrink-0 font-mono text-xs text-slate-500">
                    {formatDateTime(v.created_at)}
                  </span>
                  <span className="font-medium text-slate-900">
                    {v.expectation_id} ({v.side.toLowerCase()})
                  </span>
                  <span className="text-slate-600">
                    {v.original_status} → {v.status}
                  </span>
                  <span className="text-slate-600">by {actorLabel(v.reviewer, me?.user?.id)}</span>
                  <span className="w-full pl-0 text-slate-700 md:pl-46">“{v.note}”</span>
                </li>
              ))}
            </ol>
          </CardContent>
        </Card>
      ) : null}
    </div>
  );
}
