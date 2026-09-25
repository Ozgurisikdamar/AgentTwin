// @vitest-environment node
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { parse } from "yaml";
import type { Attempt } from "@/lib/api/runtime";
import {
  APPROVAL_STATUSES,
  EFFECTS,
  OUTCOMES,
  approvalStatusLabel,
  approvalStatusMeaning,
  argumentRows,
  attemptLabel,
  attemptMeaning,
  canDecide,
  changeText,
  effectLabel,
  expiry,
  failModeMeaning,
  outcomeLabel,
  outcomeMeaning,
  policyShape,
  probeText,
  problemsOf,
  reportVerdict,
  spanText,
  thresholdText,
  valueText,
} from "@/lib/runtime";
import {
  liveDeniedApproval,
  liveFailingTestReport,
  liveNotActivatable,
  livePendingApproval,
  livePolicyVersion,
  liveTestReport,
  liveUsedApproval,
} from "./runtime-fixtures";

const ROOT = resolve(__dirname, "../../../..");

/** An enum of the runtime gateway's contract. */
function contractEnum(schema: string, property?: string): string[] {
  const doc = parse(
    readFileSync(resolve(ROOT, "packages/contracts/openapi/runtime-gateway.openapi.yaml"), "utf8"),
  ) as {
    components: {
      schemas: Record<string, { enum?: string[]; properties?: Record<string, { enum?: string[] }> }>;
    };
  };
  const s = doc.components.schemas[schema]!;
  const values = property ? s.properties?.[property]?.enum : s.enum;
  expect(values, `${schema}${property ? `.${property}` : ""} has no enum`).toBeDefined();
  return [...values!].sort();
}

describe("the words for what the gateway decides", () => {
  it("names every effect, outcome, status and attempt the contract has", () => {
    expect([...EFFECTS].sort()).toEqual(contractEnum("Effect"));
    expect([...OUTCOMES].sort()).toEqual(contractEnum("Outcome"));
    expect([...APPROVAL_STATUSES].sort()).toEqual(contractEnum("ApprovalStatus"));
    for (const e of EFFECTS) expect(effectLabel(e)).not.toBe("");
    for (const o of OUTCOMES) expect(outcomeMeaning(o)).toMatch(/\.$/);
    for (const s of APPROVAL_STATUSES) expect(approvalStatusMeaning(s)).toMatch(/\.$/);
    for (const r of contractEnum("Attempt", "result") as Attempt["result"][]) {
      expect(attemptLabel(r)).not.toBe("");
      expect(attemptMeaning(r)).toMatch(/\.$/);
    }
  });

  it("says a held call waits for a person and a refused token did not run", () => {
    expect(outcomeLabel("approval_required")).toBe("Held for approval");
    expect(outcomeMeaning("approval_refused")).toContain("did not let it run");
    expect(approvalStatusLabel("USED")).toBe("Used");
    expect(attemptLabel("mismatch")).toBe("Different action");
  });
});

describe("expiry", () => {
  const at = "2026-09-25T12:00:00Z";
  it.each([
    ["2026-09-25T11:59:30Z", false, "in 30 s"],
    ["2026-09-25T11:46:00Z", false, "in 14 min"],
    ["2026-09-25T10:30:00Z", false, "in 1 h 30 min"],
    ["2026-09-25T09:00:00Z", false, "in 3 h"],
    ["2026-09-23T12:00:00Z", false, "in 2 d"],
    ["2026-09-25T12:00:00Z", true, "expired 0 s ago"],
    ["2026-09-25T12:03:00Z", true, "expired 3 min ago"],
  ])("at %s: expired %s, %s", (now, expired, text) => {
    expect(expiry(at, new Date(now))).toEqual({ expired, text });
  });

  it("does not guess on a bad timestamp", () => {
    expect(expiry("not a time", new Date())).toEqual({ expired: false, text: "—" });
  });
});

describe("who can still decide", () => {
  const due = livePendingApproval.expires_at;
  const before = new Date(new Date(due).getTime() - 60_000);
  const after = new Date(new Date(due).getTime() + 1_000);

  it("a pending request until it expires", () => {
    expect(canDecide(livePendingApproval, before)).toBe(true);
    // Its status may still say PENDING: the service closes it on the next read.
    expect(canDecide(livePendingApproval, after)).toBe(false);
  });

  it("never a request someone already decided", () => {
    expect(canDecide(liveUsedApproval, before)).toBe(false);
    expect(canDecide(liveDeniedApproval, before)).toBe(false);
    expect(canDecide({ status: "APPROVED", expires_at: due }, before)).toBe(false);
  });
});

describe("the exact action", () => {
  it("shows the approved refund's arguments, sorted", () => {
    expect(argumentRows(livePendingApproval.arguments)).toEqual([
      { path: "amount", value: "150" },
      { path: "idempotency_key", value: `"${livePendingApproval.arguments.idempotency_key}"` },
      { path: "order_id", value: `"${livePendingApproval.arguments.order_id}"` },
    ]);
  });

  it("walks nested objects and lists, and shows empty ones", () => {
    expect(
      argumentRows({
        z: null,
        items: [{ sku: "A", qty: 2 }, []],
        meta: {},
        flags: { gift: true },
      }),
    ).toEqual([
      { path: "flags.gift", value: "true" },
      { path: "items[0].qty", value: "2" },
      { path: "items[0].sku", value: '"A"' },
      { path: "items[1]", value: "[]" },
      { path: "meta", value: "{}" },
      { path: "z", value: "null" },
    ]);
    expect(argumentRows({})).toEqual([]);
  });

  it("writes a value on one line", () => {
    expect(valueText(undefined)).toBe("—");
    expect(valueText("a\nb")).toBe('"a\\nb"');
    expect(valueText({ b: 1 })).toBe('{"b":1}');
  });
});

describe("how a use differed from the approved action", () => {
  it("says what the changed refund changed", () => {
    const [mismatch, executed] = liveUsedApproval.attempts;
    expect(mismatch!.result).toBe("mismatch");
    const approved = liveUsedApproval.arguments.idempotency_key;
    const tried = mismatch!.arguments.idempotency_key;
    expect(mismatch!.changes.map(changeText)).toEqual([
      "amount: 150 → 200",
      `idempotency_key: "${approved}" → "${tried}"`,
    ]);
    expect(executed!.result).toBe("executed");
    expect(executed!.changes).toEqual([]);
  });

  it("names added and removed arguments", () => {
    expect(changeText({ path: "note", added: true, after: "x" })).toBe('note added: "x"');
    expect(changeText({ path: "reason", removed: true, before: 3 })).toBe("reason removed (was 3)");
  });
});

describe("what a policy version decides", () => {
  it("reads the seeded refund limits", () => {
    const shape = policyShape(livePolicyVersion.spec);
    expect(shape.tool).toBe("refund_payment");
    expect(shape.defaultEffect).toBe("allow");
    expect(shape.failMode).toBe("fail_closed");
    expect(shape.approvalExpiresInSeconds).toBe(3600);
    expect(shape.description).toContain("only irreversible action");
    expect(shape.rules.map((r) => [r.name, r.when, r.effect])).toEqual([
      ["over-automatic-limit", "args.amount > 100", "require_approval"],
      ["over-finance-limit", "args.amount > 1000", "deny"],
      ["one-refund-per-conversation", "trace.calls >= 1", "deny"],
    ]);
    expect(shape.tests).toHaveLength(6);
    const second = shape.tests.find((t) => t.name === "a second refund in the conversation")!;
    expect(second).toMatchObject({ expect: "deny", rule: "one-refund-per-conversation" });
    expect(second.context).toEqual({ traceCalls: 1 });
    expect(shape.tests[0]!.rule).toBeUndefined();
  });

  it("fills the service's defaults and never trusts a malformed document", () => {
    expect(policyShape({})).toEqual({
      tool: "",
      description: "",
      defaultEffect: "allow",
      failMode: "fail_closed",
      approvalExpiresInSeconds: null,
      rules: [],
      tests: [],
    });
    const odd = policyShape({
      spec: { default: "maybe", rules: [{ name: 1, effect: "explode" }, "x"], tests: [{ expect: 2 }] },
    });
    expect(odd.defaultEffect).toBe("allow");
    expect(odd.rules).toEqual([
      { name: "", when: "", effect: "deny", message: "" },
      { name: "", when: "", effect: "deny", message: "" },
    ]);
    expect(odd.tests[0]).toMatchObject({ name: "", expect: "deny", args: {}, context: {} });
  });

  it("explains fail modes and thresholds", () => {
    expect(failModeMeaning("fail_closed")).toContain("denies");
    expect(failModeMeaning("fail_open")).toContain("read-only");
    expect(failModeMeaning("require_approval")).toContain("asks a person");
    expect(failModeMeaning("other")).toBe("other");
    expect(thresholdText(livePolicyVersion.thresholds[0]!)).toBe("args.amount > 100");
  });
});

describe("what a person reads about testing and activating", () => {
  it("lists the problems an error names, and only those with a message", () => {
    const err = {
      status: 422,
      code: "POLICY_NOT_ACTIVATABLE",
      details: liveNotActivatable.body.error.details,
    };
    expect(problemsOf(err)).toEqual([
      {
        field: "spec.tests[1]",
        message:
          '"someone else" fails: expected allow, the policy decided deny (The agent may email only customers at example.com.)',
      },
    ]);
    expect(problemsOf({ details: { problems: [{ field: "x" }, { message: "m" }, "junk"] } })).toEqual([
      { field: "", message: "m" },
    ]);
    expect(problemsOf({ details: { problems: "no" } })).toEqual([]);
    expect(problemsOf(null)).toEqual([]);
  });

  it("says whether a version can be activated", () => {
    expect(reportVerdict(liveTestReport)).toBe("All 6 tests pass: this version can be activated.");
    expect(reportVerdict(liveFailingTestReport)).toBe("1 of 6 tests fail: this version cannot be activated.");
    expect(reportVerdict({ ...liveTestReport, results: [] })).toMatch(/^No tests/);
    expect(reportVerdict({ ...liveTestReport, activatable: false })).toMatch(
      /cannot be activated \(see the problems\)/,
    );
  });

  it("writes probes and spans", () => {
    expect(probeText({ value: 100.01, effect: "require_approval", rule: "over-automatic-limit" })).toBe(
      "100.01 → Needs approval (over-automatic-limit)",
    );
    expect(probeText({ value: 99, effect: "allow" })).toBe("99 → Allow");
    expect(spanText(3600)).toBe("1 h");
    expect(spanText(-5)).toBe("0 s");
  });
});
