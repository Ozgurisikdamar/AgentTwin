import { describe, expect, it } from "vitest";
import type { GateDecision, ReleaseGate } from "@/lib/api/control-plane";
import {
  auditActionLabel,
  auditFacts,
  countLines,
  coverageRows,
  deltaTone,
  evalCaseHref,
  evidenceFileName,
  formatCostDelta,
  formatLatencyDelta,
  gateBadge,
  outcomeTone,
  overrideBody,
  overrideProblems,
  overrideState,
  releaseName,
  riskContributions,
  toLocalInput,
  traceHref,
  whyLines,
} from "@/lib/releases";
import {
  liveBlockedGate,
  liveOverriddenGate,
  livePassedGate,
  liveReleaseAudit,
  liveReleasePage,
  liveWarnedGate,
} from "./release-fixtures";

const release = liveReleasePage.items[0]!;

describe("gateBadge", () => {
  it("names every state, and never turns an override into a pass", () => {
    expect(gateBadge(null)).toEqual({ label: "Not evaluated", tone: "neutral", note: null });
    expect(gateBadge(release.gate)).toEqual({ label: "BLOCK", tone: "danger", note: null });
    const base = { outcome: null, overridden: false, incomplete: null } as const;
    expect(gateBadge({ ...base, effective_outcome: "PENDING" })).toMatchObject({ label: "Evaluating" });
    expect(
      gateBadge({ effective_outcome: "OVERRIDDEN", outcome: "BLOCK", overridden: true, incomplete: false }),
    ).toEqual({ label: "Overridden", tone: "brand", note: "originally BLOCK" });
    // An expired override: the decision applies again, and says so.
    expect(
      gateBadge({ effective_outcome: "BLOCK", outcome: "BLOCK", overridden: true, incomplete: true }),
    ).toEqual({ label: "BLOCK", tone: "danger", note: "evidence missing, override expired" });
    expect(
      gateBadge({ effective_outcome: "WARN", outcome: "WARN", overridden: false, incomplete: false }),
    ).toMatchObject({ tone: "warning", note: null });
    expect(
      gateBadge({ effective_outcome: "PASS", outcome: "PASS", overridden: false, incomplete: false }),
    ).toMatchObject({ tone: "success" });
  });

  it("tones unknown outcomes neutrally", () => {
    expect(outcomeTone("SOMETHING")).toBe("neutral");
    expect(outcomeTone(null)).toBe("neutral");
    expect(outcomeTone("OVERRIDDEN")).toBe("brand");
  });
});

describe("what a decision says", () => {
  it("lists every rule that fired with its scenarios, most severe first", () => {
    const why = whyLines(liveBlockedGate.decision);
    expect(why.map((w) => w.rule)).toEqual(liveBlockedGate.summary.rules);
    expect(why[0]).toEqual({
      rule: "duplicate_side_effect",
      outcome: "BLOCK",
      title: "Duplicate irreversible action",
      statement: "An irreversible action must not take effect more than once.",
      scenarios: ["refund-timeout-after-mutation"],
      preExisting: 0,
      evidence: 1,
    });
    // Evidence the baseline shares is counted.
    const shared = structuredClone(liveBlockedGate.decision) as GateDecision;
    shared.rules[0]!.evidence.push({ ...shared.rules[0]!.evidence[0]!, pre_existing: true });
    expect(whyLines(shared)[0]).toMatchObject({
      scenarios: ["refund-timeout-after-mutation"],
      preExisting: 1,
      evidence: 2,
    });
    // A scenario named by several pieces of evidence is listed once.
    for (const w of why) expect(new Set(w.scenarios).size).toBe(w.scenarios.length);
    expect(whyLines(livePassedGate.decision)).toEqual([]);
    expect(whyLines(null)).toEqual([]);
  });

  it("puts the counts in words, worst first", () => {
    expect(countLines(liveBlockedGate.decision.counts)).toEqual([
      "2 new critical failures",
      "1 regressed scenario",
      "3 of 9 required scenarios failed",
    ]);
    expect(countLines(livePassedGate.decision.counts)).toEqual([]);
    expect(
      countLines({
        required: 1,
        evaluated: 0,
        passed: 0,
        failed: 1,
        incomplete: 1,
        new_critical_failures: 1,
        regressed: 2,
        improved: 1,
        known_regressions: 3,
      }),
    ).toEqual([
      "1 new critical failure",
      "1 scenario without evidence",
      "2 regressed scenarios",
      "1 of 1 required scenario failed",
      "1 improved scenario",
      "3 known regressions replayed",
    ]);
  });

  it("keeps coverage that had something to cover, and the risk factors that added to it", () => {
    const rows = coverageRows(liveBlockedGate.decision.coverage);
    expect(rows.map((r) => r.name)).not.toContain("Known regressions replayed");
    expect(rows[0]).toMatchObject({
      name: "Required scenarios passed",
      covered: 6,
      total: 9,
      complete: false,
    });
    expect(rows.find((r) => r.name === "Irreversible actions in reach tested")?.complete).toBe(true);
    expect(coverageRows(null)).toEqual([]);

    const factors = riskContributions(liveBlockedGate.decision.risk_index);
    expect(factors.map((f) => [f.name, f.contribution])).toEqual([
      ["blocking failures", 60],
      ["non-critical regressions", 15],
      ["latency, cost or semantic regressions", 10],
      ["irreversible actions in reach", 5],
    ]);
    expect(factors.reduce((s, f) => s + f.contribution, 0)).toBe(90);
    expect(liveBlockedGate.decision.risk_index.value).toBe(90);
    expect(riskContributions(null)).toEqual([]);
  });

  it("signs the deltas; more cost or latency is worse", () => {
    expect(formatCostDelta(0.043)).toBe("+$0.04");
    expect(formatCostDelta(-1.2)).toBe("−$1.20");
    expect(formatCostDelta(0.004)).toBe("+$0.0040");
    expect(formatCostDelta(0)).toBe("$0");
    expect(formatCostDelta(null)).toBe("—");
    expect(formatLatencyDelta(-16)).toBe("−16 ms");
    expect(formatLatencyDelta(120.4)).toBe("+120 ms");
    expect(formatLatencyDelta(0.2)).toBe("0 ms");
    expect(formatLatencyDelta(2500)).toBe("+2.50 s");
    expect(formatLatencyDelta(-12_345)).toBe("−12.3 s");
    expect(formatLatencyDelta(Number.NaN)).toBe("—");
    expect(deltaTone(0.043)).toBe("worse");
    expect(deltaTone(-16)).toBe("better");
    expect(deltaTone(0)).toBe("same");
    expect(deltaTone(null)).toBe("same");
  });

  it("links to the evidence", () => {
    expect(releaseName(release)).toBe("support-refund-agent 1.2.4 → 1.3.0");
    expect(evalCaseHref("r-1", "refund timeout/2")).toBe("/evaluations/r-1/cases/refund%20timeout%2F2");
    expect(traceHref("abc", "p 1")).toBe("/traces/abc?project_id=p%201");
    expect(
      evidenceFileName(
        { agent: { id: "a", name: "support/agent" }, candidate: { id: "c", version: "1.3.0+b 1" } },
        2,
      ),
    ).toBe("agenttwin-gate-support_agent-1.3.0_b_1-r2.json");
  });
});

describe("overrides", () => {
  it("allow only the latest revision's WARN or BLOCK decision, once", () => {
    expect(overrideState(liveBlockedGate, 1)).toEqual({ possible: true, reason: null });
    expect(overrideState(liveWarnedGate, 2)).toEqual({ possible: true, reason: null }); // its second revision
    expect(overrideState(liveBlockedGate, 2)).toEqual({
      possible: false,
      reason: "Only the latest revision can be overridden.",
    });
    expect(overrideState(livePassedGate, 1).reason).toBe("The gate passed.");
    expect(overrideState(liveOverriddenGate, 1).reason).toMatch(/overridden before/);
    const evaluating: ReleaseGate = { ...liveBlockedGate, status: "EVALUATING", decision: null };
    expect(overrideState(evaluating, 1).reason).toMatch(/not decided/);
  });

  const now = new Date(2026, 8, 25, 12, 0);
  const ok = { reason: "Hotfix for the outage.", ticketUrl: "", expiresAt: "" };

  it("check the form as the API does", () => {
    expect(overrideProblems(ok, now)).toEqual({});
    expect(overrideProblems({ ...ok, reason: "   too short  " }, now).reason).toMatch(/at least 10/);
    expect(overrideProblems({ ...ok, reason: "é".repeat(10) }, now)).toEqual({}); // characters, not bytes
    expect(overrideProblems({ ...ok, reason: "é".repeat(5) }, now).reason).toMatch(/at least 10/);
    expect(overrideProblems({ ...ok, reason: "x".repeat(2001) }, now).reason).toMatch(/At most 2000/);
    expect(overrideProblems({ ...ok, reason: "Because\u0007 it is fine." }, now).reason).toMatch(/control/);
    expect(overrideProblems({ ...ok, reason: "Line one\n\tline two." }, now)).toEqual({});
    expect(overrideProblems({ ...ok, ticketUrl: "javascript:alert(1)" }, now).ticketUrl).toBeDefined();
    expect(overrideProblems({ ...ok, ticketUrl: "ftp://x.example.com" }, now).ticketUrl).toBeDefined();
    expect(
      overrideProblems({ ...ok, ticketUrl: `https://t.example.com/${"a".repeat(2048)}` }, now).ticketUrl,
    ).toBeDefined();
    expect(overrideProblems({ ...ok, ticketUrl: " https://tickets.example.com/OPS-12 " }, now)).toEqual({});
    // Surrounding whitespace is trimmed first, as the API does.
    expect(overrideProblems({ ...ok, ticketUrl: "\thttps://tickets.example.com/OPS-12\n" }, now)).toEqual({});
    expect(overrideProblems({ ...ok, expiresAt: "2026-09-25T11:59" }, now).expiresAt).toMatch(/future/);
    expect(overrideProblems({ ...ok, expiresAt: "2026-09-25T12:00" }, now).expiresAt).toMatch(/future/);
    expect(overrideProblems({ ...ok, expiresAt: "2026-12-24T12:00" }, now)).toEqual({}); // exactly 90 days
    expect(overrideProblems({ ...ok, expiresAt: "2026-12-24T12:01" }, now).expiresAt).toMatch(/90 days/);
    expect(overrideProblems({ ...ok, expiresAt: "2026-02-30T12:00" }, now).expiresAt).toMatch(/Not a date/);
    expect(overrideProblems({ ...ok, expiresAt: "tomorrow" }, now).expiresAt).toMatch(/Not a date/);
  });

  it("send what the form holds, trimmed, for its revision", () => {
    expect(overrideBody(ok, 3)).toEqual({ reason: "Hotfix for the outage.", revision: 3 });
    const at = new Date(2026, 8, 26, 9, 30);
    expect(
      overrideBody(
        {
          reason: "  Hotfix for the outage.  ",
          ticketUrl: " https://t.example.com/1 ",
          expiresAt: toLocalInput(at),
        },
        1,
      ),
    ).toEqual({
      reason: "Hotfix for the outage.",
      ticket_url: "https://t.example.com/1",
      expires_at: at.toISOString(),
      revision: 1,
    });
    expect(toLocalInput(new Date(2026, 0, 2, 3, 4))).toBe("2026-01-02T03:04");
  });
});

describe("the audit trail", () => {
  it("names the release's actions and their facts", () => {
    expect(liveReleaseAudit.items.map((e) => auditActionLabel(e.action))).toEqual([
      "Gate overridden",
      "Gate decided",
      "Evaluation requested",
      "Release created",
    ]);
    expect(auditActionLabel("release.something_new")).toBe("Release something new");
    const override = liveReleaseAudit.items[0]!;
    expect(auditFacts(override)).toEqual([
      `expires at ${override.metadata.expires_at}`,
      "original outcome BLOCK",
      "revision 1",
      "ticket url https://tickets.example.com/OPS-12",
    ]);
    expect(auditFacts({ metadata: null as never })).toEqual([]);
    // Nested values are left out; facts read in key order.
    expect(auditFacts({ metadata: { z_last: 1, nested: { x: 1 }, a_first: "yes", empty: "" } })).toEqual([
      "a first yes",
      "z last 1",
    ]);
  });
});
