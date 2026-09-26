import { AlertTriangle, CheckCircle2, MinusCircle, XCircle } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { KeyValue } from "@/components/ui/key-value";
import { formatDateTime, humanize } from "@/lib/format";
import type { Outcome, OutcomeStatus, Span, Trace } from "@/lib/types";
import { OutcomeBadge } from "./badges";

/** The agent's own outcome report, recorded as an outcome span (a claim). */
export function selfReport(spans: readonly Span[]): Span | undefined {
  return [...spans].reverse().find((s) => s.kind === "outcome");
}

function renderValue(v: unknown): string {
  if (v === undefined) return "—";
  return typeof v === "string" ? v : JSON.stringify(v);
}

/** Expected vs actual state, key by key (state verification evidence). */
export function StateComparison({
  expected,
  actual,
}: {
  expected: Record<string, unknown> | null | undefined;
  actual: Record<string, unknown> | null | undefined;
}) {
  const keys = [...new Set([...Object.keys(expected ?? {}), ...Object.keys(actual ?? {})])].sort();
  if (keys.length === 0) return null;
  return (
    <div className="relative overflow-x-auto">
      <table className="mt-3 w-full text-left text-xs">
        <caption className="mb-1 text-left text-xs font-medium text-slate-600">State verification</caption>
        <thead className="text-slate-500">
          <tr>
            <th scope="col" className="py-1 pr-2 font-medium">
              Field
            </th>
            <th scope="col" className="py-1 pr-2 font-medium">
              Expected
            </th>
            <th scope="col" className="py-1 pr-2 font-medium">
              Actual
            </th>
            <th scope="col" className="py-1 font-medium">
              Result
            </th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100 font-mono">
          {keys.map((k) => {
            const e = expected?.[k];
            const a = actual?.[k];
            const comparable = e !== undefined && e !== null;
            const match = comparable && JSON.stringify(e) === JSON.stringify(a);
            return (
              <tr key={k}>
                <td className="py-1 pr-2">{k}</td>
                <td className="py-1 pr-2">{renderValue(e)}</td>
                <td className="py-1 pr-2">{renderValue(a)}</td>
                <td className="py-1 font-sans">
                  {!comparable ? (
                    <span className="inline-flex items-center gap-1 text-slate-500">
                      <MinusCircle className="h-3.5 w-3.5" aria-hidden="true" />
                      not asserted
                    </span>
                  ) : match ? (
                    <span className="inline-flex items-center gap-1 text-emerald-800">
                      <CheckCircle2 className="h-3.5 w-3.5" aria-hidden="true" />
                      match
                    </span>
                  ) : (
                    <span className="inline-flex items-center gap-1 text-rose-800">
                      <XCircle className="h-3.5 w-3.5" aria-hidden="true" />
                      mismatch
                    </span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function OutcomeCard({
  trace,
  outcome,
  spans,
  className,
}: {
  trace: Trace;
  outcome: Outcome | null;
  spans: readonly Span[];
  className?: string;
}) {
  const report = selfReport(spans)?.attributes ?? {};
  const claimed = (outcome?.claimed_status ?? report.outcome_claimed ?? null) as OutcomeStatus | null;
  const status = (outcome?.status ??
    trace.outcome_status ??
    report.outcome_status ??
    null) as OutcomeStatus | null;
  const verified = outcome?.verified ?? trace.outcome_verified ?? false;
  const contradiction = outcome?.contradiction ?? (verified && !!claimed && !!status && claimed !== status);
  return (
    <Card className={className} data-testid="outcome-card">
      <CardHeader>
        <CardTitle>Final outcome</CardTitle>
        <OutcomeBadge status={status} verified={verified} />
      </CardHeader>
      <CardContent className="space-y-3">
        {contradiction ? (
          <div
            role="alert"
            className="flex gap-2 rounded-md border border-rose-200 bg-rose-50 p-3 text-sm text-rose-900"
          >
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
            <p>
              The agent claimed <strong>{humanize(claimed)}</strong>, but the evidence shows{" "}
              <strong>{humanize(status)}</strong>. Self-reports are never treated as verification.
            </p>
          </div>
        ) : null}
        {!outcome && !status ? (
          <p className="text-sm text-slate-600">No outcome has been reported for this run.</p>
        ) : (
          <KeyValue
            items={[
              {
                label: "Business outcome",
                value: humanize(outcome?.business_outcome ?? report.business_outcome),
              },
              { label: "Agent claimed", value: claimed ? humanize(claimed) : null },
              {
                label: "Evidence",
                value: verified
                  ? humanize(outcome?.verification_source ?? report.verification_source)
                  : "None — self-reported only",
              },
              {
                label: "Recorded by",
                value: outcome ? `${outcome.recorded_by} · ${formatDateTime(outcome.recorded_at)}` : null,
              },
              { label: "Notes", value: outcome?.notes },
            ]}
          />
        )}
        <StateComparison expected={outcome?.expected_state} actual={outcome?.actual_state} />
      </CardContent>
    </Card>
  );
}
