/** Pure helpers for evaluation runs, compared cases, datasets and reviews. */
import type {
  BodyOf,
  CaseComparison,
  ClassCounts,
  Classification,
  EvalCaseDetail,
  EvalCaseSummary,
  EvalExpectationResult,
  EvalRun,
  HumanReview,
  JudgeBudget,
  JudgeIdentity,
  Metrics,
  ReviewSide,
  SideTotals,
  TrajectoryStep,
} from "./api/evaluation";
import { formatCost, formatDuration, formatNumber } from "./format";

/** Classifications, worst first (the order cases and counts are shown in). */
export const CLASSIFICATIONS: readonly Classification[] = [
  "NEW_CRITICAL_FAILURE",
  "REGRESSED",
  "INCOMPLETE",
  "IMPROVED",
  "UNCHANGED",
];

export const CLASSIFICATION_LABEL: Record<Classification, string> = {
  NEW_CRITICAL_FAILURE: "New critical failure",
  REGRESSED: "Regressed",
  INCOMPLETE: "Incomplete",
  IMPROVED: "Improved",
  UNCHANGED: "Unchanged",
};

/** A classification read from untrusted text (a URL), or undefined when it is not one. */
export function asClassification(value: string): Classification | undefined {
  return CLASSIFICATIONS.find((c) => c === value);
}

/** Compared cases worth looking at first: worst classification, then suite position. */
export function sortComparedCases<T extends Pick<EvalCaseSummary, "classification" | "position">>(
  cases: readonly T[],
): T[] {
  const rank = (c: Classification) => {
    const i = CLASSIFICATIONS.indexOf(c);
    return i < 0 ? CLASSIFICATIONS.length : i;
  };
  return [...cases].sort(
    (a, b) => rank(a.classification) - rank(b.classification) || a.position - b.position,
  );
}

const COUNT_NOUNS: Record<Classification, [string, string]> = {
  NEW_CRITICAL_FAILURE: ["new critical failure", "new critical failures"],
  REGRESSED: ["regressed", "regressed"],
  INCOMPLETE: ["incomplete", "incomplete"],
  IMPROVED: ["improved", "improved"],
  UNCHANGED: ["unchanged", "unchanged"],
};

/** "2 new critical failures", "1 regressed". */
export function countLabel(classification: Classification, n: number): string {
  return `${formatNumber(n)} ${COUNT_NOUNS[classification][n === 1 ? 0 : 1]}`;
}

/** "2 new critical failures · 1 regressed · 6 unchanged" (zero counts left out). */
export function countsSummary(counts: ClassCounts): string {
  const parts = CLASSIFICATIONS.filter((c) => (counts[c] ?? 0) > 0).map((c) => countLabel(c, counts[c]));
  return parts.length ? parts.join(" · ") : "no cases compared";
}

export type Tone = "neutral" | "success" | "warning" | "danger" | "info";

/**
 * What a finished comparison says about the candidate, in one line. A case
 * that could not be compared is never read as a pass, and an unfinished run
 * says nothing yet.
 */
export function evalRunVerdict(
  run: Pick<EvalRun, "status" | "counts" | "candidate_version" | "baseline_version">,
): { tone: Tone; text: string } | null {
  if (run.status !== "COMPLETED") return null;
  const c = run.counts;
  const scenarios = (n: number) => `${formatNumber(n)} ${n === 1 ? "scenario" : "scenarios"}`;
  const base = `v${run.baseline_version}`;
  if (c.NEW_CRITICAL_FAILURE > 0) {
    return {
      tone: "danger",
      text: `v${run.candidate_version} newly fails a critical expectation in ${scenarios(c.NEW_CRITICAL_FAILURE)}`,
    };
  }
  if (c.REGRESSED > 0) {
    return {
      tone: "danger",
      text: `v${run.candidate_version} does worse than ${base} in ${scenarios(c.REGRESSED)}`,
    };
  }
  if (c.INCOMPLETE > 0) {
    return {
      tone: "warning",
      text: `${scenarios(c.INCOMPLETE)} could not be compared: not a pass until they run on both sides`,
    };
  }
  if (c.IMPROVED > 0) {
    return {
      tone: "success",
      text: `No regressions; v${run.candidate_version} fixes ${scenarios(c.IMPROVED)}`,
    };
  }
  return { tone: "success", text: `No regressions against ${base}` };
}

/** What a run evaluated: its dataset version, scenario names, tags, or every scenario. */
export function suiteLabel(selection: EvalRun["selection"]): string {
  if (selection.dataset) return `${selection.dataset.name} v${selection.dataset.version}`;
  const parts: string[] = [];
  if (selection.scenarios?.length) {
    parts.push(
      selection.scenarios.length === 1
        ? selection.scenarios[0]!
        : `${formatNumber(selection.scenarios.length)} scenarios`,
    );
  }
  if (selection.tags?.length) parts.push(`tagged ${selection.tags.join(", ")}`);
  return parts.length ? parts.join(" + ") : "every scenario of the agent";
}

// ------------------------------------------------------------------ metrics

export type MetricName = keyof Metrics;

type MetricKind = "count" | "ms" | "usd" | "score";

/** The metrics of a compared case, in the order they are shown. */
export const METRICS: readonly { name: MetricName; label: string; kind: MetricKind }[] = [
  { name: "critical_failures", label: "Critical failures", kind: "count" },
  { name: "failed_expectations", label: "Failed expectations", kind: "count" },
  { name: "policy_violations", label: "Policy violations", kind: "count" },
  { name: "duplicate_side_effects", label: "Duplicate side effects", kind: "count" },
  { name: "argument_failures", label: "Argument failures", kind: "count" },
  { name: "retries", label: "Retries", kind: "count" },
  { name: "escalations", label: "Escalations", kind: "count" },
  { name: "tool_calls", label: "Tool calls", kind: "count" },
  { name: "steps", label: "Steps", kind: "count" },
  { name: "latency_ms", label: "Latency", kind: "ms" },
  { name: "tokens", label: "Tokens", kind: "count" },
  { name: "cost_usd", label: "Cost", kind: "usd" },
  { name: "semantic_score", label: "Semantic score", kind: "score" },
];

const KIND: Record<string, MetricKind> = Object.fromEntries(METRICS.map((m) => [m.name, m.kind]));

/** A metric's value ("—" when not known: an unknown is not a zero). */
export function formatMetric(name: string, value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  switch (KIND[name]) {
    case "ms":
      return formatDuration(value);
    case "usd":
      return formatCost(value);
    case "score":
      return value.toFixed(2);
    default:
      return formatNumber(value);
  }
}

/** A signed difference ("+3", "−2", "+22.2 ms"); "—" when unknown, "±0" when equal. */
export function formatDelta(name: string, delta: number | null | undefined): string {
  if (delta == null || !Number.isFinite(delta)) return "—";
  if (delta === 0) return "±0";
  const sign = delta > 0 ? "+" : "−";
  return `${sign}${formatMetric(name, Math.abs(delta))}`;
}

// ------------------------------------------------------------------ judge and budget

/** "deterministic-fake keyword-overlap-v1 (judge-prompt/1)". */
export function judgeLabel(judge: Pick<JudgeIdentity, "provider" | "model" | "prompt_version">): string {
  return `${judge.provider} ${judge.model} (${judge.prompt_version})`;
}

/** "3 calls · $0.12 of $5.00", with calls of unknown cost and exhaustion said out loud. */
export function budgetLabel(budget: JudgeBudget): string {
  const spent = formatCost(budget.spent_usd);
  const parts = [
    `${formatNumber(budget.calls)} ${budget.calls === 1 ? "call" : "calls"}`,
    budget.max_cost_usd == null ? `${spent} spent` : `${spent} of ${formatCost(budget.max_cost_usd)}`,
  ];
  if (budget.unknown_cost_calls > 0) parts.push(`${formatNumber(budget.unknown_cost_calls)} of unknown cost`);
  if (budget.exhausted) parts.push("budget spent: some expectations were not judged");
  return parts.join(" · ");
}

// ------------------------------------------------------------------ trajectories

export interface AlignedStep {
  match: CaseComparison["trajectories"]["alignment"][number]["match"];
  baseline: TrajectoryStep | null;
  candidate: TrajectoryStep | null;
}

/** The two trajectories side by side, row by row as the comparison aligned them. */
export function alignSteps(trajectories: CaseComparison["trajectories"]): AlignedStep[] {
  const byIndex = (steps: readonly TrajectoryStep[]) => new Map(steps.map((s) => [s.index, s]));
  const b = byIndex(trajectories.baseline);
  const c = byIndex(trajectories.candidate);
  return trajectories.alignment.map((row) => ({
    match: row.match,
    baseline: row.baseline == null ? null : (b.get(row.baseline) ?? null),
    candidate: row.candidate == null ? null : (c.get(row.candidate) ?? null),
  }));
}

// ------------------------------------------------------------------ reviews

export const REVIEW_SIDES: readonly ReviewSide[] = ["BASELINE", "CANDIDATE"];

/** The key the API uses for one expectation of one side (`CANDIDATE:reply-confirms`). */
export function reviewKey(side: ReviewSide, expectationId: string): string {
  return `${side}:${expectationId}`;
}

/** The review that counts for each expectation: the latest one (reviews come oldest first). */
export function latestReviews(reviews: readonly HumanReview[]): Map<string, HumanReview> {
  const out = new Map<string, HumanReview>();
  for (const r of reviews) out.set(reviewKey(r.side, r.expectation_id), r);
  return out;
}

/** Expectations a person should still look at: flagged and not reviewed yet. */
export function pendingReviews(detail: Pick<EvalCaseDetail, "needs_review" | "reviews">): string[] {
  const done = latestReviews(detail.reviews);
  return detail.needs_review.filter((key) => !done.has(key));
}

/**
 * Whether a person may review a result: an expectation can be reviewed, the
 * simulation's finding about the run itself (the agent could not be run)
 * cannot (the API answers 409 EXPECTATION_NOT_REVIEWABLE).
 */
export function isReviewable(result: Pick<EvalExpectationResult, "expectation">): boolean {
  return result.expectation.type !== "agentRun";
}

/** A result a person's review replaced (its evaluator is the review). */
export function isHumanReviewed(result: Pick<EvalExpectationResult, "evaluator">): boolean {
  return result.evaluator === "human.review";
}

// ------------------------------------------------------------------ side totals

type TotalName = Exclude<keyof SideTotals, "tokens_known" | "cost_known">;

/** A run's totals per side, in the order they are shown; `worse` says which direction is bad. */
export const SIDE_TOTALS: readonly {
  name: TotalName;
  label: string;
  kind: MetricKind;
  worse: "higher" | "lower" | null;
}[] = [
  { name: "passed", label: "Passed", kind: "count", worse: "lower" },
  { name: "failed", label: "Failed", kind: "count", worse: "higher" },
  { name: "incomplete", label: "Incomplete", kind: "count", worse: "higher" },
  { name: "critical_failures", label: "Critical failures", kind: "count", worse: "higher" },
  { name: "policy_violations", label: "Policy violations", kind: "count", worse: "higher" },
  { name: "duplicate_side_effects", label: "Duplicate side effects", kind: "count", worse: "higher" },
  { name: "retries", label: "Retries", kind: "count", worse: null },
  { name: "escalations", label: "Escalations", kind: "count", worse: null },
  { name: "tool_calls", label: "Tool calls", kind: "count", worse: null },
  { name: "latency_ms_p50", label: "Latency p50", kind: "ms", worse: null },
  { name: "latency_ms_p95", label: "Latency p95", kind: "ms", worse: null },
  { name: "tokens", label: "Tokens", kind: "count", worse: null },
  { name: "cost_usd", label: "Cost", kind: "usd", worse: null },
  { name: "semantic_score", label: "Semantic score", kind: "score", worse: "lower" },
];

/** One side's total, with how many cases reported usage when not all did. */
export function formatTotal(totals: SideTotals, name: TotalName): string {
  const row = SIDE_TOTALS.find((t) => t.name === name);
  const value = totals[name];
  if (value == null || !Number.isFinite(value)) return "—";
  const text =
    row?.kind === "ms"
      ? formatDuration(value)
      : row?.kind === "usd"
        ? formatCost(value)
        : row?.kind === "score"
          ? value.toFixed(2)
          : formatNumber(value);
  const known = name === "tokens" ? totals.tokens_known : name === "cost_usd" ? totals.cost_known : null;
  return known !== null && known < totals.cases ? `${text} (${known} of ${totals.cases} cases)` : text;
}

/** Whether the candidate's total is worse, better or the same as the baseline's. */
export function totalChange(
  name: TotalName,
  baseline: SideTotals,
  candidate: SideTotals,
): "worse" | "better" | "changed" | "same" | "unknown" {
  const b = baseline[name];
  const c = candidate[name];
  if (b == null || c == null) return "unknown";
  if (b === c) return "same";
  const worse = SIDE_TOTALS.find((t) => t.name === name)?.worse;
  if (!worse) return "changed";
  return (worse === "higher") === c > b ? "worse" : "better";
}

// ------------------------------------------------------------------ datasets

/** A dataset or case tag (the evaluation API's `Tag`). */
export const TAG = /^[a-z0-9][a-z0-9_:.-]{0,62}$/;

/** Tags typed as text: split on commas and spaces, deduplicated in order; the ones the API would refuse. */
export function parseTags(text: string): { tags: string[]; invalid: string[] } {
  const tags = [...new Set(text.split(/[\s,]+/).filter(Boolean))];
  return { tags, invalid: tags.filter((t) => !TAG.test(t)) };
}

// ------------------------------------------------------------------ judge calibration

export type CalibrationExample = BodyOf<"startJudgeCalibration">["examples"][number];

/**
 * Human-labeled examples pasted as a JSON array or as JSON Lines (one object
 * per line). Each problem names the example it is about; nothing is sent
 * while there is one.
 */
export function parseCalibrationExamples(text: string): {
  examples: CalibrationExample[];
  problems: string[];
} {
  const trimmed = text.trim();
  if (!trimmed) return { examples: [], problems: [] };
  let raw: unknown[];
  try {
    raw = trimmed.startsWith("[")
      ? (JSON.parse(trimmed) as unknown[])
      : trimmed
          .split(/\r?\n/)
          .filter((l) => l.trim())
          .map((l) => JSON.parse(l) as unknown);
  } catch (err) {
    return { examples: [], problems: [`Not JSON: ${(err as Error).message}`] };
  }
  if (!Array.isArray(raw)) return { examples: [], problems: ["Expected a JSON array of examples."] };
  const problems: string[] = [];
  const examples: CalibrationExample[] = [];
  const seen = new Set<string>();
  raw.forEach((item, i) => {
    const at = `example ${i + 1}`;
    if (!item || typeof item !== "object" || Array.isArray(item)) {
      problems.push(`${at}: not an object`);
      return;
    }
    const e = item as Record<string, unknown>;
    const missing = ["id", "rubric", "customer_message", "answer"].filter((k) => typeof e[k] !== "string");
    if (missing.length) problems.push(`${at}: ${missing.join(", ")} must be text`);
    if (e.human_label !== "pass" && e.human_label !== "fail")
      problems.push(`${at}: human_label must be pass or fail`);
    if (typeof e.id === "string") {
      if (seen.has(e.id)) problems.push(`${at}: id ${e.id} is used twice`);
      seen.add(e.id);
    }
    const unknown = Object.keys(e).filter(
      (k) => !["id", "rubric", "customer_message", "answer", "tool_calls", "human_label"].includes(k),
    );
    if (unknown.length) problems.push(`${at}: unknown field ${unknown.join(", ")}`);
    examples.push(e as CalibrationExample);
  });
  return problems.length ? { examples: [], problems } : { examples, problems };
}
