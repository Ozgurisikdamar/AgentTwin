import { describe, expect, it } from "vitest";
import type { EvalRun, HumanReview, SideTotals } from "@/lib/api/evaluation";
import {
  alignSteps,
  budgetLabel,
  countLabel,
  countsSummary,
  evalRunVerdict,
  formatDelta,
  formatMetric,
  formatTotal,
  isHumanReviewed,
  isReviewable,
  latestReviews,
  parseCalibrationExamples,
  pendingReviews,
  sortComparedCases,
  suiteLabel,
  totalChange,
} from "@/lib/evaluations";
import { liveEvalRun, liveToolSuccessLie } from "./eval-fixtures";

const counts = (over: Partial<EvalRun["counts"]> = {}): EvalRun["counts"] => ({
  NEW_CRITICAL_FAILURE: 0,
  REGRESSED: 0,
  IMPROVED: 0,
  UNCHANGED: 0,
  INCOMPLETE: 0,
  ...over,
});

function runWith(c: EvalRun["counts"], status: EvalRun["status"] = "COMPLETED") {
  return { status, counts: c, baseline_version: "1.2.4", candidate_version: "1.3.0" };
}

describe("classifications", () => {
  it("lists the live cases worst first, then in suite order", () => {
    const sorted = sortComparedCases(liveEvalRun.cases);
    expect(sorted.map((c) => c.classification)).toEqual([
      "NEW_CRITICAL_FAILURE",
      "NEW_CRITICAL_FAILURE",
      "REGRESSED",
      ...Array(6).fill("UNCHANGED"),
    ]);
    expect(sorted.slice(0, 3).map((c) => c.scenario_name)).toEqual([
      "refund-timeout-after-mutation",
      "refund-tool-success-lie",
      "refund-happy-path",
    ]);
    // Within a class, the suite's order.
    const unchanged = sorted.filter((c) => c.classification === "UNCHANGED").map((c) => c.position);
    expect(unchanged).toEqual([...unchanged].sort((a, b) => a - b));
    // An incomplete case comes before an improved one: it is not a pass.
    const mixed = sortComparedCases([
      { classification: "IMPROVED" as const, position: 0 },
      { classification: "INCOMPLETE" as const, position: 1 },
    ]);
    expect(mixed.map((c) => c.classification)).toEqual(["INCOMPLETE", "IMPROVED"]);
  });

  it("summarizes counts in words, zeros left out", () => {
    expect(countsSummary(liveEvalRun.run.counts)).toBe("2 new critical failures · 1 regressed · 6 unchanged");
    expect(countsSummary(counts())).toBe("no cases compared");
    expect(countLabel("NEW_CRITICAL_FAILURE", 1)).toBe("1 new critical failure");
    expect(countLabel("INCOMPLETE", 3)).toBe("3 incomplete");
  });

  it("says what a finished comparison means, worst finding first", () => {
    expect(evalRunVerdict(liveEvalRun.run)).toEqual({
      tone: "danger",
      text: "v1.3.0 newly fails a critical expectation in 2 scenarios",
    });
    expect(evalRunVerdict(runWith(counts({ REGRESSED: 1, IMPROVED: 4 })))?.text).toBe(
      "v1.3.0 does worse than v1.2.4 in 1 scenario",
    );
    // Could not be compared: a warning, never a pass, even with improvements.
    expect(evalRunVerdict(runWith(counts({ INCOMPLETE: 2, IMPROVED: 1 })))).toEqual({
      tone: "warning",
      text: "2 scenarios could not be compared: not a pass until they run on both sides",
    });
    expect(evalRunVerdict(runWith(counts({ IMPROVED: 3, UNCHANGED: 6 })))?.text).toBe(
      "No regressions; v1.3.0 fixes 3 scenarios",
    );
    expect(evalRunVerdict(runWith(counts({ UNCHANGED: 9 })))).toEqual({
      tone: "success",
      text: "No regressions against v1.2.4",
    });
    // An unfinished or failed run says nothing about the candidate.
    for (const status of ["QUEUED", "RUNNING", "EVALUATING", "FAILED", "CANCELLED"] as const) {
      expect(evalRunVerdict(runWith(counts({ UNCHANGED: 9 }), status))).toBeNull();
    }
  });

  it("names the suite a run evaluated", () => {
    expect(suiteLabel(liveEvalRun.run.selection)).toBe("refund-regression-suite v1");
    expect(suiteLabel({ scenarios: ["refund-happy-path"], tags: null, dataset: null })).toBe(
      "refund-happy-path",
    );
    expect(suiteLabel({ scenarios: ["a", "b"], tags: ["faults"], dataset: null })).toBe(
      "2 scenarios + tagged faults",
    );
    expect(suiteLabel({ scenarios: null, tags: null, dataset: null })).toBe("every scenario of the agent");
  });
});

describe("metrics", () => {
  it("formats each metric by its kind, and unknowns as unknown", () => {
    const m = liveToolSuccessLie.comparison.metrics;
    expect(formatMetric("latency_ms", m.latency_ms!.candidate)).toBe("91 ms");
    expect(formatMetric("tokens", m.tokens!.baseline)).toBe("4,478");
    expect(formatMetric("semantic_score", m.semantic_score!.baseline)).toBe("1.00");
    expect(formatMetric("cost_usd", m.cost_usd!.baseline)).toBe("—");
    expect(formatMetric("cost_usd", 0.0123)).toBe("$0.01");
    expect(formatDelta("critical_failures", m.critical_failures!.delta)).toBe("+3");
    expect(formatDelta("tokens", m.tokens!.delta)).toBe("−1,999");
    expect(formatDelta("latency_ms", m.latency_ms!.delta)).toBe("+22 ms");
    expect(formatDelta("retries", m.retries!.delta)).toBe("±0");
    expect(formatDelta("cost_usd", m.cost_usd!.delta)).toBe("—");
  });

  it("says how many cases reported usage when not all did", () => {
    const summary = liveEvalRun.summary!;
    expect(formatTotal(summary.baseline, "tokens")).toBe("23,052 (8 of 9 cases)");
    expect(formatTotal(summary.baseline, "passed")).toBe("9");
    expect(formatTotal(summary.baseline, "cost_usd")).toBe("—");
    const all: SideTotals = { ...summary.baseline, tokens_known: summary.baseline.cases };
    expect(formatTotal(all, "tokens")).toBe("23,052");
  });

  it("colors a total by which direction is bad", () => {
    const { baseline, candidate } = liveEvalRun.summary!;
    expect(totalChange("passed", baseline, candidate)).toBe("worse"); // 9 -> 6
    expect(totalChange("failed", baseline, candidate)).toBe("worse"); // 0 -> 3
    expect(totalChange("incomplete", baseline, candidate)).toBe("same");
    expect(totalChange("semantic_score", baseline, candidate)).toBe("worse");
    expect(totalChange("tool_calls", baseline, candidate)).toBe("changed"); // no better direction
    expect(totalChange("cost_usd", baseline, candidate)).toBe("unknown");
    expect(totalChange("failed", candidate, baseline)).toBe("better");
  });

  it("describes the judge budget, saying what was not judged", () => {
    expect(budgetLabel(liveEvalRun.run.budget!)).toBe("0 calls · $0 of $5.00");
    expect(
      budgetLabel({
        max_cost_usd: null,
        max_calls: 100,
        spent_usd: 0.1234,
        calls: 1,
        unknown_cost_calls: 1,
        exhausted: true,
      }),
    ).toBe("1 call · $0.12 spent · 1 of unknown cost · budget spent: some expectations were not judged");
  });
});

describe("trajectories", () => {
  it("puts the live steps side by side as the comparison aligned them", () => {
    const rows = alignSteps(liveToolSuccessLie.comparison.trajectories);
    expect(rows.map((r) => [r.baseline?.name ?? null, r.match, r.candidate?.name ?? null])).toEqual([
      ["lookup_order", "same", "lookup_order"],
      ["get_refund_policy", "baseline_only", null],
      ["refund_payment", "changed", "refund_payment"],
      ["lookup_order", "baseline_only", null],
      ["escalate_to_human", "baseline_only", null],
      ["PARTIAL", "baseline_only", null],
      [null, "candidate_only", "send_email"],
      [null, "candidate_only", "SUCCESS"],
    ]);
    // The changed row pairs different step numbers on each side.
    expect([rows[2]!.baseline?.index, rows[2]!.candidate?.index]).toEqual([3, 2]);
  });

  it("leaves a row empty when a step it names is missing", () => {
    const rows = alignSteps({
      baseline: [],
      candidate: [],
      alignment: [{ baseline: 4, candidate: null, match: "baseline_only" }],
    });
    expect(rows).toEqual([{ match: "baseline_only", baseline: null, candidate: null }]);
  });
});

describe("reviews", () => {
  const review = (over: Partial<HumanReview>): HumanReview => ({
    id: "r",
    eval_run_id: "e",
    scenario_name: "s",
    side: "CANDIDATE",
    expectation_id: "confirms",
    original_status: "ERROR",
    original_label: null,
    status: "PASS",
    note: "checked",
    reviewer: "user:u",
    created_at: "2026-09-25T08:00:00Z",
    ...over,
  });

  it("counts the latest review of each expectation", () => {
    const latest = latestReviews([
      review({ id: "1", status: "FAIL" }),
      review({ id: "2", side: "BASELINE" }),
      review({ id: "3", status: "PASS" }),
    ]);
    expect(latest.get("CANDIDATE:confirms")?.id).toBe("3");
    expect(latest.get("BASELINE:confirms")?.id).toBe("2");
  });

  it("keeps an expectation pending until someone reviews that side of it", () => {
    const detail = { needs_review: ["CANDIDATE:confirms", "BASELINE:confirms", "CANDIDATE:other"] };
    expect(pendingReviews({ ...detail, reviews: [] })).toEqual(detail.needs_review);
    expect(pendingReviews({ ...detail, reviews: [review({})] })).toEqual([
      "BASELINE:confirms",
      "CANDIDATE:other",
    ]);
    expect(pendingReviews(liveToolSuccessLie)).toEqual([]);
  });

  it("does not offer to review the simulation's finding about the run", () => {
    const [first] = liveToolSuccessLie.results.candidate;
    expect(isReviewable(first!)).toBe(true);
    expect(isReviewable({ expectation: { id: "agent-run", type: "agentRun", critical: true } })).toBe(false);
    expect(isHumanReviewed(first!)).toBe(false);
    expect(isHumanReviewed({ evaluator: "human.review" })).toBe(true);
  });
});

describe("calibration examples", () => {
  const example = (id: string, label = "pass") => ({
    id,
    rubric: "The reply says the refund is not confirmed.",
    customer_message: "Where is my refund?",
    answer: "The refund is not confirmed yet.",
    human_label: label,
  });

  it("reads a JSON array or JSON Lines", () => {
    const array = parseCalibrationExamples(JSON.stringify([example("a"), example("b", "fail")]));
    expect(array.problems).toEqual([]);
    expect(array.examples.map((e) => [e.id, e.human_label])).toEqual([
      ["a", "pass"],
      ["b", "fail"],
    ]);
    const lines = parseCalibrationExamples(
      `${JSON.stringify(example("a"))}\n\n${JSON.stringify(example("b"))}\n`,
    );
    expect(lines.examples.map((e) => e.id)).toEqual(["a", "b"]);
    expect(parseCalibrationExamples("   ")).toEqual({ examples: [], problems: [] });
  });

  it("names every problem and sends nothing while there is one", () => {
    const bad = parseCalibrationExamples(
      JSON.stringify([
        example("a"),
        { ...example("a"), human_label: "maybe" },
        { id: "c", rubric: "r", answer: "x", human_label: "pass", score: 1 },
        "text",
      ]),
    );
    expect(bad.examples).toEqual([]);
    expect(bad.problems).toEqual([
      "example 2: human_label must be pass or fail",
      "example 2: id a is used twice",
      "example 3: customer_message must be text",
      "example 3: unknown field score",
      "example 4: not an object",
    ]);
    expect(parseCalibrationExamples("[{").problems[0]).toMatch(/^Not JSON: /);
    expect(parseCalibrationExamples('{"id": "a"}\n{').problems[0]).toMatch(/^Not JSON: /);
  });
});
