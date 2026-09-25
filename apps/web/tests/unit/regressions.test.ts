// @vitest-environment node
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { parse } from "yaml";
import type { RegressionEvent, RegressionStatus } from "@/lib/api/evaluation";
import {
  FAILURE_LABELS,
  SEVERITIES,
  STATUS_VIEWS,
  assigneeProblem,
  availableActions,
  canTriage,
  eventSummary,
  needsReason,
  parseTags,
  regressionActor,
  statusBadge,
  statusMeaning,
  statusView,
  taxonomyLabel,
} from "@/lib/regressions";
import { liveCandidate, liveFixed, livePromoted, liveRegressionInbox } from "./regression-fixtures";

const ROOT = resolve(__dirname, "../../../..");
const STATUSES: RegressionStatus[] = ["CANDIDATE", "CONFIRMED", "PROMOTED", "FIXED", "DISMISSED", "REOPENED"];

/** The lifecycle the evaluation service enforces (`mining._ALLOWED`). */
function serviceTransitions(): Record<string, string[]> {
  const src = readFileSync(
    resolve(ROOT, "services/evaluation-service/src/agenttwin_evaluation/mining.py"),
    "utf8",
  );
  const block = /_ALLOWED: dict\[Action, frozenset\[str\]\] = \{([\s\S]*?)\n\}/.exec(src);
  expect(block, "mining._ALLOWED not found").not.toBeNull();
  const out: Record<string, string[]> = {};
  for (const m of block![1]!.matchAll(/"(\w+)": frozenset\(\{([^}]*)\}\)/g)) {
    out[m[1]!] = [...m[2]!.matchAll(/"(\w+)"/g)].map((x) => x[1]!).sort();
  }
  return out;
}

describe("what a regression allows", () => {
  it("offers a status change exactly where the service allows it", () => {
    const allowed = serviceTransitions();
    expect(Object.keys(allowed).sort()).toEqual(["confirm", "dismiss", "fixed", "promote", "reopen"]);
    for (const action of ["confirm", "promote", "dismiss", "reopen"] as const) {
      const offered = STATUSES.filter((status) =>
        availableActions({ status, merged_into: null, scenario_name: null }).includes(action),
      ).sort();
      expect(offered, action).toEqual(allowed[action]);
    }
  });

  it("never promotes a group with a test again, nor merges it away", () => {
    const withTest = { merged_into: null, scenario_name: "regression-x" };
    expect(availableActions({ status: "PROMOTED", ...withTest })).toEqual([]);
    expect(availableActions({ status: "REOPENED", ...withTest })).toEqual(["confirm", "dismiss"]);
    expect(availableActions({ status: "FIXED", ...withTest })).toEqual(["reopen"]);
    const fixed = liveFixed.regression;
    expect(availableActions(fixed)).toEqual(["reopen"]);
    expect(availableActions(livePromoted.regression)).toEqual([]);
  });

  it("offers nothing on a group merged into another, and merges any other group", () => {
    const merged = { status: "CANDIDATE" as const, merged_into: "x", scenario_name: null };
    expect(availableActions(merged)).toEqual([]);
    expect(canTriage(merged)).toBe(false);
    expect(availableActions(liveCandidate.regression)).toEqual(["confirm", "promote", "dismiss", "merge"]);
    expect(availableActions({ status: "DISMISSED", merged_into: null, scenario_name: null })).toEqual([
      "reopen",
      "merge",
    ]);
    expect(canTriage(liveCandidate.regression)).toBe(true);
  });

  it("asks a reason where the service requires one", () => {
    expect(
      ["confirm", "promote", "dismiss", "reopen", "merge"].filter((a) => needsReason(a as never)),
    ).toEqual(["dismiss", "reopen"]);
  });
});

describe("words", () => {
  it("names every status and failure label of the contract", () => {
    const contract = parse(
      readFileSync(resolve(ROOT, "packages/contracts/openapi/evaluation-service.openapi.yaml"), "utf8"),
    ) as { components: { schemas: Record<string, { enum?: string[] }> } };
    const schemas = contract.components.schemas;
    expect([...FAILURE_LABELS]).toEqual(schemas.FailureLabel!.enum);
    expect([...SEVERITIES]).toEqual(schemas.Severity!.enum);
    expect(STATUSES).toEqual(schemas.RegressionStatus!.enum);
    for (const s of STATUSES) {
      expect(statusBadge(s).label).not.toBe("");
      expect(statusMeaning(s)).not.toBe("");
    }
    expect(statusBadge("PROMOTED")).toEqual({ label: "Has a test", tone: "brand" });
    expect(taxonomyLabel("DUPLICATE_SIDE_EFFECT")).toBe("Duplicate side effect");
  });

  it("opens the inbox on what needs a decision; every status is in exactly one view", () => {
    expect(statusView(null).value).toBe("open");
    expect(statusView("nonsense").value).toBe("open");
    expect(statusView("all").statuses).toEqual([]);
    const listed = STATUS_VIEWS.flatMap((v) => v.statuses as readonly string[]).sort();
    expect(listed).toEqual([...STATUSES].sort());
  });

  it("tells the history of the live regression", () => {
    const me = "01a0d9b2-0000-7000-8000-000000000001";
    expect(liveFixed.events.map((e) => eventSummary(e))).toEqual([
      "Found by the regression miner",
      "Promoted to the regression test regression-refund-payment-took-effect-twice-edc1a0 in production-regressions",
      "Fixed in 1.3.1",
    ]);
    expect(liveFixed.events.map((e) => regressionActor(e.actor))).toEqual([
      "the regression miner",
      "User reviewer",
      "an evaluation",
    ]);
    expect(regressionActor(`user:${me}`, me)).toBe("You");
  });

  it("tells triage, merges and unknown actions", () => {
    const event = (action: RegressionEvent["action"], detail: Record<string, unknown>): RegressionEvent => ({
      seq: 2,
      action,
      from_status: null,
      to_status: null,
      actor: "user:alex",
      reason: null,
      detail,
      at: "2026-09-25T17:52:03Z",
    });
    expect(
      eventSummary(event("triage", { severity: ["critical", "high"], tags: [[], ["payments", "refunds"]] })),
    ).toBe("Changed severity critical → high; tags none → payments, refunds");
    expect(eventSummary(event("assign", { assignee: ["user:alex", null] }))).toBe(
      "Changed assignee user:alex → none",
    );
    expect(
      eventSummary(event("merge", { into: "01a0d9b2-177f-7086-8b59-7f8801edc1a0", occurrences: 1 })),
    ).toBe("Merged into 01edc1a0");
    expect(
      eventSummary(event("merged", { from: "01a0d9b2-177f-7086-8b59-7f8801edc1a0", occurrences: 2 })),
    ).toBe("Took in 01edc1a0 (2 failures)");
    expect(eventSummary(event("triage", {}))).toBe("Triaged");
  });
});

describe("the triage form's rules", () => {
  it("reads tags as the service accepts them", () => {
    expect(parseTags(" payments, refunds payments ,, ")).toEqual({
      tags: ["payments", "refunds"],
      invalid: [],
    });
    expect(parseTags("Payments refund_v2 a:b x.y -bad")).toEqual({
      tags: ["refund_v2", "a:b", "x.y"],
      invalid: ["Payments", "-bad"],
    });
    expect(parseTags("a".repeat(63)).tags).toHaveLength(1);
    expect(parseTags("a".repeat(64)).invalid).toHaveLength(1);
  });

  it("accepts a principal as the assignee, or nobody", () => {
    expect(assigneeProblem("")).toBeNull();
    expect(assigneeProblem("user:alex")).toBeNull();
    expect(assigneeProblem("apikey:ci.bot@x")).toBeNull();
    expect(assigneeProblem("alex")).not.toBeNull();
    expect(assigneeProblem("user:al ex")).not.toBeNull();
  });

  it("the live inbox is what the service sent", () => {
    expect(liveRegressionInbox.items.map((r) => r.status)).toEqual(["DISMISSED", "CANDIDATE", "CANDIDATE"]);
  });
});
