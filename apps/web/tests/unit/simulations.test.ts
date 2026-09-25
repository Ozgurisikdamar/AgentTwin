import { describe, expect, it } from "vitest";
import {
  actorLabel,
  casesWithStatus,
  describeStep,
  formatParams,
  formatValue,
  groupStateChanges,
  isRunActive,
  isRunCancellable,
  passRate,
  resultCounts,
  runDurationMs,
  runProgress,
  runVerdictSummary,
  sortCases,
} from "@/lib/simulations";
import type { CaseStep, ExpectationResult, SimulationCase, SimulationRun } from "@/lib/types";

function run(over: Partial<SimulationRun> = {}): SimulationRun {
  return {
    id: "r1",
    organization_id: "o",
    project_id: "p",
    agent_name: "support-refund-agent",
    agent_version: "1.3.0",
    agent_version_id: null,
    side: "SINGLE",
    eval_run_id: null,
    release_id: null,
    status: "COMPLETED",
    requested_by: "user:u1",
    cancel_requested: false,
    attempts: 1,
    case_count: 9,
    passed: 6,
    failed: 3,
    errored: 0,
    cancelled: 0,
    critical_failures: 2,
    finished_cases: 9,
    error: null,
    created_at: "2026-09-24T10:00:00Z",
    started_at: "2026-09-24T10:00:01Z",
    finished_at: "2026-09-24T10:00:05.500Z",
    updated_at: "2026-09-24T10:00:05.500Z",
    ...over,
  };
}

function kase(name: string, status: SimulationCase["status"], position: number): SimulationCase {
  return {
    id: name,
    run_id: "r1",
    position,
    scenario_id: "s",
    scenario_version_id: "v",
    scenario_name: name,
    severity: "critical",
    twin_definition_id: "t",
    status,
    seed: 1,
    tenant: "demo-co",
    call_count: 2,
    trace_id: null,
    reason: null,
    error: null,
    latency_ms: 10,
    outcome_status: null,
    started_at: null,
    finished_at: null,
    labels: [],
    score: null,
  };
}

describe("run status", () => {
  it("knows which runs are still moving and which can be cancelled", () => {
    expect(isRunActive("RUNNING")).toBe(true);
    expect(isRunActive("EVALUATING")).toBe(true);
    expect(isRunActive("COMPLETED")).toBe(false);
    expect(isRunCancellable(run({ status: "QUEUED" }))).toBe(true);
    expect(isRunCancellable(run({ status: "RUNNING", cancel_requested: true }))).toBe(false);
    // Evaluation finishes on its own: cancelling it would lose the verdict.
    expect(isRunCancellable(run({ status: "EVALUATING" }))).toBe(false);
    expect(isRunCancellable(run({ status: "COMPLETED" }))).toBe(false);
  });

  it("summarizes verdicts, progress and pass rate", () => {
    expect(runVerdictSummary(run())).toBe("6 passed · 3 failed");
    expect(runVerdictSummary(run({ passed: 0, failed: 0 }))).toBe("0 passed");
    expect(runVerdictSummary(run({ passed: 1, failed: 0, errored: 1, cancelled: 2 }))).toBe(
      "1 passed · 1 errored · 2 cancelled",
    );
    const p = runProgress(run({ status: "RUNNING", passed: 3, failed: 1 }));
    expect(p).toEqual({
      total: 9,
      done: 4,
      segments: [
        { key: "passed", count: 3, pct: (3 / 9) * 100 },
        { key: "failed", count: 1, pct: (1 / 9) * 100 },
      ],
    });
    expect(runProgress(run({ case_count: 0, passed: 0, failed: 0 })).segments).toEqual([]);
    expect(passRate(run())).toBeCloseTo(6 / 9);
    expect(passRate(run({ passed: 0, failed: 0 }))).toBeNull();
  });

  it("measures duration, growing while active", () => {
    expect(runDurationMs(run())).toBe(4500);
    const now = new Date("2026-09-24T10:00:11Z");
    expect(runDurationMs(run({ finished_at: null }), now)).toBe(10_000);
    expect(runDurationMs(run({ started_at: null }))).toBeNull();
  });
});

describe("cases", () => {
  it("lists problems first and keeps the scenario order otherwise", () => {
    const cases = [
      kase("a", "PASSED", 0),
      kase("b", "FAILED", 1),
      kase("c", "ERRORED", 2),
      kase("d", "FAILED", 3),
    ];
    expect(sortCases(cases).map((c) => c.scenario_name)).toEqual(["b", "d", "c", "a"]);
    expect(casesWithStatus(cases, "FAILED")).toEqual(["b", "d"]);
  });
});

describe("actorLabel", () => {
  it("names who started a run without leaking full ids", () => {
    expect(actorLabel("user:u1", "u1")).toBe("You");
    expect(actorLabel("user:01a0d5d1-08dc-7a2b-9c3d-4e5f60718293", "u1")).toBe("User 60718293");
    expect(actorLabel("apikey:01a0d5d0-9568-74a7-8b1c-2d3e4f5a6b7c")).toBe("API key 4f5a6b7c");
    expect(actorLabel("service:simulation-service")).toBe("Service simulation-service");
    expect(actorLabel(null)).toBe("—");
  });
});

describe("describeStep", () => {
  const step = (record: CaseStep["record"], kind = "tool_call"): CaseStep => ({
    seq: 1,
    kind,
    tool: (record.tool as string) ?? null,
    latency_ms: 0,
    created_at: "2026-09-24T10:00:00Z",
    record,
  });

  it("shows the lie: success reported, no state change", () => {
    const v = describeStep(
      step({
        tool: "refund_payment",
        status: "ok",
        http_status: 200,
        fault: "success_without_mutation",
        mutated: false,
        expects_mutation: true,
      }),
    );
    expect(v.title).toBe("refund_payment");
    expect(v.facts.map((f) => f.label)).toEqual([
      "ok",
      "HTTP 200",
      "fault: success without mutation",
      "no state change",
    ]);
    expect(v.facts.find((f) => f.label === "no state change")?.tone).toBe("danger");
  });

  it("describes denials, replays and retrievals", () => {
    const denied = describeStep(
      step({
        tool: "lookup_order",
        status: "denied",
        http_status: 403,
        error_code: "ACCESS_DENIED",
        cross_tenant: "denied",
        policy_violation: "CROSS_TENANT",
      }),
    );
    expect(denied.facts.map((f) => f.label)).toEqual([
      "denied",
      "HTTP 403",
      "ACCESS_DENIED",
      "cross-tenant denied",
      "policy: CROSS_TENANT",
    ]);
    const replay = describeStep(
      step({ tool: "refund_payment", status: "ok", replayed: true, delay_ms: 1500 }),
    );
    expect(replay.facts.map((f) => f.label)).toEqual(["ok", "replayed (idempotent)", "+1.50 s delay"]);
    const kb = describeStep(
      step(
        {
          query: "refund policy",
          documents: [
            { id: "a", trusted: false },
            { id: "b", trusted: true },
          ],
        },
        "retrieval",
      ),
    );
    expect(kb.title).toBe("Knowledge search: refund policy");
    expect(kb.facts.map((f) => f.label)).toEqual(["2 documents", "1 untrusted"]);
  });
});

describe("evidence formatting", () => {
  it("counts results and groups state changes", () => {
    const r = (status: ExpectationResult["status"]) => ({ status }) as ExpectationResult;
    expect(resultCounts([r("PASS"), r("FAIL"), r("FAIL"), r("SKIPPED")])).toEqual({
      PASS: 1,
      FAIL: 2,
      SKIPPED: 1,
      ERROR: 0,
    });
    const groups = groupStateChanges([
      { op: "changed", path: "orders.ORD-1001.refund_count", before: 0, after: 2 },
      { op: "added", path: "refunds[0]", after: { amount: 40 } },
      { op: "added", path: "refunds[1]", after: { amount: 40 } },
    ]);
    expect(groups.map(([k, v]) => [k, v.length])).toEqual([
      ["orders", 1],
      ["refunds", 2],
    ]);
    expect(formatValue({ a: 1 })).toBe('{"a":1}');
    expect(formatValue("x")).toBe('"x"');
    expect(formatValue(undefined)).toBe("—");
    expect(formatValue("y".repeat(300), 10)).toHaveLength(10);
  });
});

describe("formatParams", () => {
  it("renders parameters as key: value, skipping the ones shown elsewhere", () => {
    expect(
      formatParams({ id: "x", type: "state", path: "orders.ORD-1001.refund_count", equals: 1 }, [
        "id",
        "type",
      ]),
    ).toBe('path: "orders.ORD-1001.refund_count" · equals: 1');
    expect(formatParams({})).toBe("");
    expect(formatParams({ big: "a".repeat(200) }, [], 12)).toBe('big: "aaaaaaaaaa…');
  });
});
