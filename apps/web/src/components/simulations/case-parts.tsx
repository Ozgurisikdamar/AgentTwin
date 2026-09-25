/** Presentational parts of a simulation case: expectations, trajectory, faults and state. */
import { BookOpen, ChevronRight, Wrench } from "lucide-react";
import { RiskBadge } from "@/components/traces/badges";
import { Badge } from "@/components/ui/badge";
import { EmptyState } from "@/components/ui/states";
import { describeStep, formatParams, formatValue, groupStateChanges } from "@/lib/simulations";
import type { CaseStep, ExpectationResult, ScenarioFault, StateChange } from "@/lib/types";
import { FailureLabel, ResultBadge } from "./badges";

export function JsonBlock({ value, label }: { value: unknown; label: string }) {
  return (
    <pre
      aria-label={label}
      className="max-h-72 overflow-auto whitespace-pre-wrap break-words rounded border border-slate-100 bg-slate-50 p-2 font-mono text-xs text-slate-800"
    >
      {JSON.stringify(value, null, 2) ?? "—"}
    </pre>
  );
}

/** "tool_call:2" -> a link to that trajectory step; other references as text. */
function EvidenceRef({ refId }: { refId: string }) {
  const m = /^tool_call:(\d+)$/.exec(refId);
  if (m) {
    return (
      <a href={`#step-${m[1]}`} className="font-mono text-indigo-700 hover:underline">
        step {m[1]}
      </a>
    );
  }
  return <code className="text-slate-600">{refId}</code>;
}

export function ExpectationResults({ results }: { results: ExpectationResult[] }) {
  if (results.length === 0) return <EmptyState title="No expectation was evaluated" />;
  return (
    <ul className="divide-y divide-slate-100" data-testid="expectations">
      {results.map((r, i) => (
        <li
          key={`${r.expectation.id}-${i}`}
          className="flex flex-col gap-1.5 px-4 py-3"
          data-testid="expectation"
          data-expectation={r.expectation.id}
          data-status={r.status}
        >
          <div className="flex flex-wrap items-center gap-2">
            <ResultBadge status={r.status} />
            <span className="font-medium text-slate-900">{r.expectation.id}</span>
            <code className="text-xs text-slate-500">{r.expectation.type}</code>
            {r.critical ? <Badge tone="danger">critical</Badge> : null}
            {r.status === "FAIL" && r.label ? <FailureLabel label={r.label} /> : null}
          </div>
          <p className="text-sm text-slate-700">{r.reason}</p>
          {r.evidence?.length ? (
            <ul className="space-y-0.5 text-xs text-slate-600" aria-label="Evidence">
              {r.evidence.map((e, j) => (
                // The spaces between the parts are for assistive technology and
                // copied text; the flex gap spaces them visually.
                <li key={`${e.ref ?? e.kind}-${j}`} className="flex flex-wrap gap-x-2" data-testid="evidence">
                  {e.ref ? (
                    <>
                      <EvidenceRef refId={e.ref} />{" "}
                    </>
                  ) : null}
                  <span>{e.detail}</span>
                  {e.expected !== undefined ? (
                    <>
                      {" "}
                      <span className="text-slate-500">
                        expected <code className="text-slate-700">{formatValue(e.expected, 80)}</code>
                      </span>
                    </>
                  ) : null}
                  {e.actual !== undefined ? (
                    <>
                      {" "}
                      <span className="text-slate-500">
                        actual <code className="text-slate-700">{formatValue(e.actual, 80)}</code>
                      </span>
                    </>
                  ) : null}
                </li>
              ))}
            </ul>
          ) : null}
        </li>
      ))}
    </ul>
  );
}

const FACT_TONE = {
  neutral: "neutral",
  success: "success",
  warning: "warning",
  danger: "danger",
  info: "info",
} as const;

function StepRow({ step }: { step: CaseStep }) {
  const view = describeStep(step);
  const r = step.record;
  const isTool = step.kind === "tool_call";
  const Icon = isTool ? Wrench : BookOpen;
  const changes = (r.changes ?? []) as StateChange[];
  return (
    <li
      id={`step-${step.seq}`}
      className="scroll-mt-20"
      data-testid="trajectory-step"
      data-tool={step.tool ?? ""}
    >
      <details className="group" open={Boolean(r.fault)}>
        <summary className="flex cursor-pointer list-none flex-wrap items-center gap-2 px-4 py-2.5 hover:bg-slate-50">
          <ChevronRight
            className="h-4 w-4 shrink-0 text-slate-400 transition-transform group-open:rotate-90 motion-reduce:transition-none"
            aria-hidden="true"
          />
          <span className="w-6 shrink-0 text-right font-mono text-xs text-slate-500">{step.seq}</span>
          <Icon className="h-4 w-4 shrink-0 text-slate-500" aria-hidden="true" />
          <span className="font-medium text-slate-900">{view.title}</span>
          {isTool ? <RiskBadge risk={(r.risk as string | null) ?? null} /> : null}
          {view.facts.map((f) => (
            <Badge key={f.label} tone={FACT_TONE[f.tone]}>
              {f.label}
            </Badge>
          ))}
        </summary>
        <div className="grid gap-3 border-t border-slate-100 bg-white px-4 py-3 lg:grid-cols-2">
          {isTool ? (
            <>
              <div>
                <h4 className="mb-1 text-xs font-medium text-slate-600">Arguments</h4>
                <JsonBlock value={r.arguments ?? {}} label={`Arguments of step ${step.seq}`} />
              </div>
              <div>
                <h4 className="mb-1 text-xs font-medium text-slate-600">
                  What the twin answered{r.http_status ? ` (HTTP ${r.http_status})` : ""}
                </h4>
                <JsonBlock value={r.response ?? null} label={`Response of step ${step.seq}`} />
              </div>
              {changes.length ? (
                <div className="lg:col-span-2">
                  <h4 className="mb-1 text-xs font-medium text-slate-600">State changes by this call</h4>
                  <StateDiffTable changes={changes} caption={`State changes of step ${step.seq}`} />
                </div>
              ) : null}
            </>
          ) : (
            <div className="lg:col-span-2">
              <h4 className="mb-1 text-xs font-medium text-slate-600">Documents returned</h4>
              <ul className="flex flex-wrap gap-1.5">
                {(r.documents ?? []).map((d) => (
                  <li key={d.id}>
                    <Badge tone={d.trusted ? "neutral" : "warning"}>
                      {d.id}
                      {d.trusted ? "" : " · untrusted"}
                    </Badge>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      </details>
    </li>
  );
}

export function Trajectory({ steps }: { steps: CaseStep[] }) {
  if (steps.length === 0) {
    return <EmptyState title="The agent made no tool calls" />;
  }
  return (
    <ol className="divide-y divide-slate-100" data-testid="trajectory" aria-label="Trajectory">
      {steps.map((s) => (
        <StepRow key={s.seq} step={s} />
      ))}
    </ol>
  );
}

function whenText(when: Record<string, unknown> | undefined): string {
  if (!when || Object.keys(when).length === 0) return "every call";
  const parts: string[] = [];
  if (typeof when.callNumber === "number") parts.push(`call ${when.callNumber}`);
  if (Array.isArray(when.callNumbers)) parts.push(`calls ${when.callNumbers.join(", ")}`);
  if (typeof when.firstN === "number") parts.push(`first ${when.firstN} calls`);
  if (when.everyCall) parts.push("every call");
  if (typeof when.probability === "number") parts.push(`probability ${when.probability}`);
  if (when.argsMatch) parts.push(`args match ${formatValue(when.argsMatch, 60)}`);
  return parts.join(", ") || "every call";
}

/** Faults of a scenario; with `steps` (a run), also where each one was injected. */
export function FaultList({ faults, steps }: { faults: ScenarioFault[]; steps?: CaseStep[] }) {
  if (faults.length === 0) {
    return (
      <p className="px-4 py-3 text-sm text-slate-600">
        {steps
          ? "No faults were injected: the twin behaved normally."
          : "No faults: the twin behaves normally."}
      </p>
    );
  }
  return (
    <ul className="divide-y divide-slate-100" data-testid="faults">
      {faults.map((f, i) => {
        const { type, ...params } = f.behavior;
        const hits = (steps ?? [])
          .filter((s) => s.record.fault === type && s.tool === f.target)
          .map((s) => s.seq);
        return (
          <li key={`${f.target}-${type}-${i}`} className="flex flex-col gap-1 px-4 py-3" data-testid="fault">
            <div className="flex flex-wrap items-center gap-2">
              <Badge tone="warning">{type.replaceAll("_", " ")}</Badge>
              <span className="text-sm text-slate-700">
                on <code className="font-medium">{f.target}</code>, {whenText(f.when)}
              </span>
            </div>
            {Object.keys(params).length ? (
              <p className="font-mono text-xs text-slate-600">{formatParams(params)}</p>
            ) : null}
            {steps ? (
              <p className="text-xs text-slate-500">
                {hits.length ? (
                  <>
                    Injected at{" "}
                    {hits.map((n, j) => (
                      <span key={n}>
                        {j ? ", " : ""}
                        <a href={`#step-${n}`} className="text-indigo-700 hover:underline">
                          step {n}
                        </a>
                      </span>
                    ))}
                  </>
                ) : (
                  "Not triggered in this run (the agent never made a matching call)."
                )}
              </p>
            ) : null}
          </li>
        );
      })}
    </ul>
  );
}

const OP_TONE = { added: "success", removed: "danger", changed: "warning" } as const;

export function StateDiffTable({ changes, caption }: { changes: StateChange[]; caption: string }) {
  if (changes.length === 0) {
    return <p className="text-sm text-slate-600">No state changed.</p>;
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-xs">
        <caption className="sr-only">{caption}</caption>
        <thead className="text-slate-500">
          <tr>
            <th scope="col" className="py-1 pr-2 font-medium">
              Change
            </th>
            <th scope="col" className="py-1 pr-2 font-medium">
              Path
            </th>
            <th scope="col" className="py-1 pr-2 font-medium">
              Before
            </th>
            <th scope="col" className="py-1 font-medium">
              After
            </th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100 font-mono">
          {changes.map((c) => (
            <tr key={`${c.op}-${c.path}`} className="align-top" data-testid="state-change" data-path={c.path}>
              <td className="py-1 pr-2 font-sans">
                <Badge tone={OP_TONE[c.op] ?? "neutral"}>{c.op}</Badge>
              </td>
              <td className="break-all py-1 pr-2 text-slate-900">{c.path}</td>
              <td className="break-all py-1 pr-2 text-slate-600">
                {c.op === "added" ? "—" : formatValue(c.before)}
              </td>
              <td className="break-all py-1 text-slate-900">
                {c.op === "removed" ? "—" : formatValue(c.after)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** The whole run's state changes, grouped by collection. */
export function StateDiff({ changes }: { changes: StateChange[] }) {
  if (changes.length === 0) {
    return <p className="text-sm text-slate-600">The twin state is exactly as the scenario started it.</p>;
  }
  return (
    <div className="space-y-3" data-testid="state-diff">
      {groupStateChanges(changes).map(([group, items]) => (
        <section key={group}>
          <h4 className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">{group}</h4>
          <StateDiffTable changes={items} caption={`Changes to ${group}`} />
        </section>
      ))}
    </div>
  );
}
