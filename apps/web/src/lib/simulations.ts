/** Pure helpers for simulation runs, cases and their evidence. */
import { formatDuration, shortId } from "./format";
import type {
  CaseStatus,
  CaseStep,
  ExpectationResult,
  RunStatus,
  Severity,
  SimulationCase,
  SimulationRun,
  StateChange,
} from "./types";

export const FINAL_RUN_STATUSES: ReadonlySet<RunStatus> = new Set(["COMPLETED", "FAILED", "CANCELLED"]);
export const FINAL_CASE_STATUSES: ReadonlySet<CaseStatus> = new Set([
  "PASSED",
  "FAILED",
  "ERRORED",
  "CANCELLED",
]);
export const RUN_STATUSES: readonly RunStatus[] = [
  "QUEUED",
  "PREPARING",
  "RUNNING",
  "EVALUATING",
  "COMPLETED",
  "FAILED",
  "CANCELLED",
];
export const SEVERITIES: readonly Severity[] = ["critical", "high", "medium", "low"];

/** A run status read from untrusted text (a URL), or undefined when it is not one. */
export function asRunStatus(value: string): RunStatus | undefined {
  return RUN_STATUSES.find((s) => s === value);
}

/** A severity read from untrusted text (a URL), or undefined when it is not one. */
export function asSeverity(value: string): Severity | undefined {
  return SEVERITIES.find((s) => s === value);
}

/** A run that has not reached a final status (the page keeps polling it). */
export function isRunActive(status: RunStatus): boolean {
  return !FINAL_RUN_STATUSES.has(status);
}

/** A run can still be cancelled until it starts evaluating. */
export function isRunCancellable(run: Pick<SimulationRun, "status" | "cancel_requested">): boolean {
  return !run.cancel_requested && ["QUEUED", "PREPARING", "RUNNING"].includes(run.status);
}

export interface ProgressSegment {
  key: "passed" | "failed" | "errored" | "cancelled";
  count: number;
  /** Width in percent of all cases. */
  pct: number;
}

/** Finished cases by verdict, as progress-bar segments. */
export function runProgress(run: SimulationRun): {
  total: number;
  done: number;
  segments: ProgressSegment[];
} {
  const total = Math.max(0, run.case_count);
  const keys = ["passed", "failed", "errored", "cancelled"] as const;
  const segments = keys
    .map((key) => ({ key, count: Math.max(0, run[key]) }))
    .filter((s) => s.count > 0)
    .map((s) => ({ ...s, pct: total ? (s.count / total) * 100 : 0 }));
  const done = Math.min(
    total,
    segments.reduce((n, s) => n + s.count, 0),
  );
  return { total, done, segments };
}

/** "6 passed · 3 failed" (zero counts are omitted, except "0 passed" for an empty run). */
export function runVerdictSummary(
  run: Pick<SimulationRun, "passed" | "failed" | "errored" | "cancelled">,
): string {
  const parts = (
    [
      ["passed", run.passed],
      ["failed", run.failed],
      ["errored", run.errored],
      ["cancelled", run.cancelled],
    ] as const
  )
    .filter(([, n]) => n > 0)
    .map(([label, n]) => `${n} ${label}`);
  return parts.length ? parts.join(" · ") : "0 passed";
}

/** Share of evaluated cases that passed; null until a case was evaluated. */
export function passRate(run: Pick<SimulationRun, "passed" | "failed" | "errored">): number | null {
  const evaluated = run.passed + run.failed + run.errored;
  return evaluated ? run.passed / evaluated : null;
}

/** Wall time of a run (still growing while it is active). */
export function runDurationMs(
  run: Pick<SimulationRun, "started_at" | "finished_at">,
  now: Date = new Date(),
): number | null {
  if (!run.started_at) return null;
  const start = Date.parse(run.started_at);
  const end = run.finished_at ? Date.parse(run.finished_at) : now.getTime();
  if (Number.isNaN(start) || Number.isNaN(end)) return null;
  return Math.max(0, end - start);
}

/** Cases worth looking at first: failed and errored before passed, then by position. */
export function sortCases(cases: readonly SimulationCase[]): SimulationCase[] {
  const rank: Record<string, number> = { FAILED: 0, ERRORED: 1, RUNNING: 2, PENDING: 3, CANCELLED: 4 };
  return [...cases].sort((a, b) => (rank[a.status] ?? 5) - (rank[b.status] ?? 5) || a.position - b.position);
}

/** Who started a run: "you", or the kind of principal and a short id. */
export function actorLabel(actor: string | null | undefined, meUserId?: string): string {
  if (!actor) return "—";
  const [kind, id = ""] = actor.split(/:(.*)/s, 2) as [string, string?];
  if (kind === "user") return meUserId && id === meUserId ? "You" : `User ${shortId(id)}`;
  if (kind === "apikey") return `API key ${shortId(id)}`;
  if (kind === "service") return `Service ${id}`;
  return actor;
}

export interface StepView {
  title: string;
  /** Short facts: status, HTTP code, injected fault, retries, mutation. */
  facts: { label: string; tone: "neutral" | "success" | "warning" | "danger" | "info" }[];
}

/** What a trajectory step did, in words (used for rows and accessible names). */
export function describeStep(step: CaseStep): StepView {
  const r = step.record;
  if (step.kind === "retrieval") {
    const docs = r.documents ?? [];
    const untrusted = docs.filter((d) => !d.trusted).length;
    return {
      title: `Knowledge search: ${r.query ?? ""}`.trim(),
      facts: [
        { label: `${docs.length} document${docs.length === 1 ? "" : "s"}`, tone: "neutral" },
        ...(untrusted ? [{ label: `${untrusted} untrusted`, tone: "warning" as const }] : []),
      ],
    };
  }
  const facts: StepView["facts"] = [];
  const status = r.status ?? "unknown";
  facts.push({ label: status.replaceAll("_", " "), tone: status === "ok" ? "success" : "danger" });
  if (r.http_status) facts.push({ label: `HTTP ${r.http_status}`, tone: "neutral" });
  if (r.fault) facts.push({ label: `fault: ${r.fault.replaceAll("_", " ")}`, tone: "warning" });
  if (r.error_code) facts.push({ label: r.error_code, tone: "neutral" });
  if (r.cross_tenant) facts.push({ label: `cross-tenant ${r.cross_tenant}`, tone: "danger" });
  if (r.policy_violation) facts.push({ label: `policy: ${r.policy_violation}`, tone: "danger" });
  if (r.replayed) facts.push({ label: "replayed (idempotent)", tone: "info" });
  if (r.mutated) facts.push({ label: "changed state", tone: "info" });
  else if (r.expects_mutation && status === "ok") facts.push({ label: "no state change", tone: "danger" });
  if (r.delay_ms) facts.push({ label: `+${formatDuration(r.delay_ms)} delay`, tone: "neutral" });
  return { title: step.tool ?? r.tool ?? "tool call", facts };
}

/** Expectation results in the order the scenario declares them, failures counted. */
export function resultCounts(
  results: readonly ExpectationResult[],
): Record<ExpectationResult["status"], number> {
  const out = { PASS: 0, FAIL: 0, SKIPPED: 0, ERROR: 0 };
  for (const r of results) out[r.status] = (out[r.status] ?? 0) + 1;
  return out;
}

/** Compact text for a state value (state diffs, arguments). */
export function formatValue(value: unknown, max = 160): string {
  if (value === undefined) return "—";
  const text = JSON.stringify(value) ?? String(value);
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

/** Parameters as `key: value · key: value`, skipping the keys in `omit`. */
export function formatParams(
  params: Record<string, unknown>,
  omit: readonly string[] = [],
  max = 80,
): string {
  return Object.entries(params)
    .filter(([k]) => !omit.includes(k))
    .map(([k, v]) => `${k}: ${formatValue(v, max)}`)
    .join(" · ");
}

/** State changes grouped by top-level collection ("orders", "refunds", ...). */
export function groupStateChanges(changes: readonly StateChange[]): [string, StateChange[]][] {
  const groups = new Map<string, StateChange[]>();
  for (const c of changes) {
    const top = c.path.split(/[.[]/, 1)[0] || c.path;
    const list = groups.get(top) ?? [];
    list.push(c);
    groups.set(top, list);
  }
  return [...groups.entries()];
}

/** Scenario names of the cases with a given status, sorted. */
export function casesWithStatus(cases: readonly SimulationCase[], status: CaseStatus): string[] {
  return cases
    .filter((c) => c.status === status)
    .map((c) => c.scenario_name)
    .sort();
}
