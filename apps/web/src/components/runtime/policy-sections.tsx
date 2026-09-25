import { CheckCircle2, XCircle } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { KeyValue } from "@/components/ui/key-value";
import type { PolicyTestReport, Problem, Threshold } from "@/lib/api/runtime";
import {
  type PolicyShape,
  argumentRows,
  failModeMeaning,
  probeText,
  spanText,
  reportVerdict,
  thresholdText,
} from "@/lib/runtime";
import { EffectBadge, RiskBadge } from "./runtime-badges";

const textareaClass =
  "w-full rounded-md border border-slate-300 bg-white px-2.5 py-2 font-mono text-xs leading-5 text-slate-900 focus-visible:border-indigo-500 focus-visible:outline-2 focus-visible:outline-indigo-200";

/** The YAML (or JSON) of a policy, editable. */
export function DocumentEditor({
  id,
  value,
  onChange,
  label = "Policy document",
}: {
  id: string;
  value: string;
  onChange: (v: string) => void;
  label?: string;
}) {
  return (
    <div>
      <label htmlFor={id} className="mb-1 block text-xs font-medium text-slate-700">
        {label}
      </label>
      <textarea
        id={id}
        rows={Math.min(40, Math.max(14, value.split("\n").length + 1))}
        spellCheck={false}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className={textareaClass}
      />
    </div>
  );
}

/** The problems the gateway found in a document, or why it cannot be activated. */
export function ProblemList({ problems, title }: { problems: Problem[]; title: string }) {
  if (problems.length === 0) return null;
  return (
    <div role="alert" className="rounded-md border border-rose-200 bg-rose-50 p-3 text-sm">
      <p className="font-medium text-rose-900">{title}</p>
      <ul className="mt-1 space-y-0.5 text-rose-900" aria-label={title}>
        {problems.map((p, i) => (
          <li key={`${p.field}-${i}`} className="whitespace-pre-wrap">
            {p.field ? <code className="mr-1 text-xs">{p.field}</code> : null}
            {p.message}
          </li>
        ))}
      </ul>
    </div>
  );
}

/** What a version decides when no rule matches, and on an error. */
export function VersionSettings({ shape }: { shape: PolicyShape }) {
  return (
    <KeyValue
      className="text-xs"
      items={[
        { label: "Tool", value: <code>{shape.tool}</code> },
        { label: "No rule matched", value: <EffectBadge effect={shape.defaultEffect} /> },
        {
          label: "Fail mode",
          value: (
            <span>
              <code>{shape.failMode}</code> — {failModeMeaning(shape.failMode)}
            </span>
          ),
        },
        {
          label: "Approval expires",
          value:
            shape.approvalExpiresInSeconds === null
              ? null
              : `${spanText(shape.approvalExpiresInSeconds)} after the request`,
        },
      ]}
    />
  );
}

/** A version's rules, in the order they are evaluated. */
export function RulesTable({ shape }: { shape: PolicyShape }) {
  if (shape.rules.length === 0) {
    return <p className="text-sm text-slate-600">No rules: every call gets the default.</p>;
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[40rem] text-left text-sm" aria-label="Rules">
        <thead className="border-b border-slate-100 text-xs uppercase tracking-wide text-slate-500">
          <tr>
            {["#", "Rule", "When", "Effect", "Message"].map((c) => (
              <th key={c} scope="col" className="px-2 py-1.5 font-medium">
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {shape.rules.map((r, i) => (
            <tr key={`${r.name}-${i}`} className="align-top" data-testid="rule-row">
              <td className="px-2 py-1.5 text-slate-500">{i + 1}</td>
              <td className="px-2 py-1.5 font-medium text-slate-900">{r.name}</td>
              <td className="px-2 py-1.5">
                <code className="rounded bg-slate-100 px-1 text-xs">{r.when}</code>
              </td>
              <td className="px-2 py-1.5">
                <EffectBadge effect={r.effect} />
              </td>
              <td className="px-2 py-1.5 text-slate-700">{r.message}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function argsText(args: Record<string, unknown>): string {
  const rows = argumentRows(args);
  return rows.length === 0 ? "no arguments" : rows.map((r) => `${r.path}=${r.value}`).join(", ");
}

/** The tests a version carries: what it must decide. */
export function TestsTable({ shape }: { shape: PolicyShape }) {
  if (shape.tests.length === 0) {
    return <p className="text-sm text-slate-600">No tests. A version without tests cannot be activated.</p>;
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[40rem] text-left text-sm" aria-label="Tests">
        <thead className="border-b border-slate-100 text-xs uppercase tracking-wide text-slate-500">
          <tr>
            {["Test", "Action", "Expects"].map((c) => (
              <th key={c} scope="col" className="px-2 py-1.5 font-medium">
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {shape.tests.map((t, i) => (
            <tr key={`${t.name}-${i}`} className="align-top" data-testid="test-row">
              <td className="px-2 py-1.5 font-medium text-slate-900">{t.name}</td>
              <td className="px-2 py-1.5 font-mono text-xs text-slate-700">
                {argsText(t.args)}
                {Object.keys(t.context).length > 0 ? (
                  <div className="text-slate-500">context: {argsText(t.context)}</div>
                ) : null}
              </td>
              <td className="px-2 py-1.5">
                <EffectBadge effect={t.expect} />
                {t.rule ? <div className="mt-0.5 text-xs text-slate-500">by {t.rule}</div> : null}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** The numbers a version's rules compare against. */
export function ThresholdList({ thresholds }: { thresholds: Threshold[] }) {
  if (thresholds.length === 0) return null;
  return (
    <ul className="flex flex-wrap gap-1.5" aria-label="Thresholds">
      {thresholds.map((t, i) => (
        <li key={`${t.rule}-${i}`}>
          <code className="rounded bg-slate-100 px-1.5 py-0.5 text-xs" title={t.rule}>
            {thresholdText(t)}
          </code>
        </li>
      ))}
    </ul>
  );
}

/** What running a version's tests showed: each test, the boundaries, what no test decided. */
export function TestReportView({ report }: { report: PolicyTestReport }) {
  return (
    <Card data-testid="test-report" data-passed={report.passed}>
      <CardHeader>
        <CardTitle>Test report</CardTitle>
        <span className="flex items-center gap-2 text-xs text-slate-500">
          {report.tool_risk ? <RiskBadge risk={report.tool_risk} /> : null}
          <code title={report.spec_hash}>{report.spec_hash.slice(0, 12)}</code>
        </span>
      </CardHeader>
      <CardContent className="space-y-4 text-sm">
        <p
          className={report.passed && report.activatable ? "text-emerald-800" : "font-medium text-rose-800"}
          data-testid="report-verdict"
        >
          {reportVerdict(report)}
        </p>
        <ProblemList problems={report.activation_problems} title="Why it cannot be activated" />
        {report.results.length > 0 ? (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[44rem] text-left text-sm" aria-label="Test results">
              <thead className="border-b border-slate-100 text-xs uppercase tracking-wide text-slate-500">
                <tr>
                  {["", "Test", "Expected", "Decided", "Why"].map((c, i) => (
                    <th key={`${c}-${i}`} scope="col" className="px-2 py-1.5 font-medium">
                      {c || <span className="sr-only">Result</span>}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {report.results.map((r, i) => (
                  <tr
                    key={`${r.name}-${i}`}
                    className={r.passed ? "align-top" : "bg-rose-50/60 align-top"}
                    data-testid="result-row"
                    data-passed={r.passed}
                  >
                    <td className="px-2 py-1.5">
                      {r.passed ? (
                        <CheckCircle2 className="h-4 w-4 text-emerald-700" aria-label="passes" />
                      ) : (
                        <XCircle className="h-4 w-4 text-rose-700" aria-label="fails" />
                      )}
                    </td>
                    <td className="px-2 py-1.5 font-medium text-slate-900">{r.name}</td>
                    <td className="px-2 py-1.5">
                      <EffectBadge effect={r.expect} />
                      {r.expect_rule ? (
                        <div className="mt-0.5 text-xs text-slate-500">by {r.expect_rule}</div>
                      ) : null}
                    </td>
                    <td className="px-2 py-1.5">
                      <EffectBadge effect={r.decision.effect} />
                      <div className="mt-0.5 text-xs text-slate-500">
                        {r.decision.rule ? `by ${r.decision.rule}` : "the default"}
                      </div>
                    </td>
                    <td className="px-2 py-1.5 text-xs text-slate-700">{r.why ?? r.decision.message}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
        {report.undecided.length > 0 ? (
          <p className="text-xs text-amber-900" role="note">
            No test decides {report.undecided.map((r) => `“${r}”`).join(", ")}: add one so a change to{" "}
            {report.undecided.length === 1 ? "that rule" : "those rules"} cannot go unnoticed.
          </p>
        ) : null}
        {report.boundaries.length > 0 ? (
          <div>
            <p className="mb-1 text-xs font-medium uppercase tracking-wide text-slate-500">
              At the boundaries
            </p>
            <ul className="space-y-1.5" aria-label="Boundaries">
              {report.boundaries.map((b, i) => (
                <li key={`${b.rule}-${i}`} data-testid="boundary">
                  <code className="text-xs">{thresholdText(b)}</code> <Badge tone="neutral">{b.rule}</Badge>
                  <div className="mt-0.5 font-mono text-xs text-slate-600">
                    {b.probes.map(probeText).join(" · ")}
                  </div>
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}
