import { describe, expect, it } from "vitest";
import {
  appendIn,
  asScenario,
  getIn,
  hasMappingRoot,
  newScenarioYaml,
  parseYaml,
  removeAt,
  scenarioName,
  setIn,
} from "@/lib/scenario-yaml";

const AUTHORED = `# Owned by the support platform team.
apiVersion: agenttwin.dev/v1
kind: Scenario
metadata:
  name: refund-happy-path
  severity: high # raised after the March incident
  tags: [refunds]
spec:
  agent: support-refund-agent
  input:
    message: "Refund $40 for ORD-1001"
  expectations:
    - id: refunded
      type: toolCalled
      tool: refund_payment
`;

describe("parseYaml", () => {
  it("parses a mapping", () => {
    const parsed = parseYaml(AUTHORED);
    expect(parsed.ok).toBe(true);
    if (!parsed.ok) return;
    expect(getIn(parsed.value, ["metadata", "severity"])).toBe("high");
    expect(getIn(parsed.value, ["spec", "expectations", 0, "tool"])).toBe("refund_payment");
    expect(getIn(parsed.value, ["spec", "missing", "deeper"])).toBeUndefined();
    expect(scenarioName(parsed.value)).toBe("refund-happy-path");
    expect(asScenario(parsed.value)?.spec.agent).toBe("support-refund-agent");
  });

  it("reports the line of a syntax error", () => {
    const parsed = parseYaml("metadata:\n  name: x\n  tags: [a,\n");
    expect(parsed.ok).toBe(false);
    if (parsed.ok) return;
    expect(parsed.line).toBeGreaterThanOrEqual(3);
    expect(parsed.error).not.toContain("\n");
  });

  it("rejects duplicate keys and non-mapping documents", () => {
    expect(parseYaml("a: 1\na: 2\n").ok).toBe(false);
    for (const text of ["- a\n- b\n", "just text", ""]) {
      const parsed = parseYaml(text);
      expect(parsed.ok).toBe(false);
    }
    expect(hasMappingRoot("- a\n")).toBe(false);
    expect(hasMappingRoot(AUTHORED)).toBe(true);
  });

  it("stops alias bombs", () => {
    const bomb = ["a: &a [x, x, x, x, x, x, x, x, x, x]"];
    for (let i = 1; i < 12; i++) {
      const prev = String.fromCharCode(96 + i);
      const next = String.fromCharCode(97 + i);
      bomb.push(`${next}: &${next} [${Array(10).fill(`*${prev}`).join(", ")}]`);
    }
    const parsed = parseYaml(bomb.join("\n"));
    expect(parsed.ok).toBe(false);
  });
});

describe("form edits keep the author's text", () => {
  it("changes one value and keeps comments and order", () => {
    const out = setIn(AUTHORED, ["metadata", "severity"], "critical");
    expect(out).toContain("# Owned by the support platform team.");
    expect(out).toContain("severity: critical # raised after the March incident");
    expect(out.indexOf("apiVersion")).toBeLessThan(out.indexOf("kind"));
    // A replaced list keeps its flow style.
    expect(setIn(AUTHORED, ["metadata", "tags"], ["refunds", "smoke"])).toContain("tags: [ refunds, smoke ]");
    const parsed = parseYaml(out);
    expect(parsed.ok && getIn(parsed.value, ["spec", "input", "message"])).toBe("Refund $40 for ORD-1001");
  });

  it("creates missing parents and removes emptied keys", () => {
    const withContext = setIn(AUTHORED, ["spec", "input", "context", "customer_id"], "CUS-100");
    const parsed = parseYaml(withContext);
    expect(parsed.ok && getIn(parsed.value, ["spec", "input", "context"])).toEqual({
      customer_id: "CUS-100",
    });
    const removed = setIn(withContext, ["metadata", "tags"], undefined);
    const again = parseYaml(removed);
    expect(again.ok && getIn(again.value, ["metadata", "tags"])).toBeUndefined();
    expect(setIn(removed, ["metadata", "owner"], "")).toBe(removed);
  });

  it("adds and removes list items", () => {
    const fault = { target: "refund_payment", behavior: { type: "timeout_after_mutation" } };
    const added = appendIn(AUTHORED, ["spec", "faults"], fault);
    const twice = appendIn(added, ["spec", "faults"], { ...fault, when: { callNumber: 2 } });
    const parsed = parseYaml(twice);
    expect(parsed.ok && getIn(parsed.value, ["spec", "faults"])).toHaveLength(2);
    const once = parseYaml(removeAt(twice, ["spec", "faults"], 0));
    expect(once.ok && getIn(once.value, ["spec", "faults"])).toEqual([{ ...fault, when: { callNumber: 2 } }]);
    // Out of range is a no-op.
    expect(parseYaml(removeAt(twice, ["spec", "faults"], 9))).toEqual(parsed);
  });

  it("refuses to edit text that does not parse", () => {
    expect(() => setIn("a: [", ["a"], 1)).toThrow(/must be valid/);
    expect(() => appendIn("a: [", ["a"], 1)).toThrow(/must be valid/);
  });
});

describe("newScenarioYaml", () => {
  it("is a valid starting point in reading order", () => {
    const text = newScenarioYaml({
      name: "late-delivery",
      agent: "support-refund-agent",
      twin: "demo-co-support",
      message: "My order ORD-1001 is late.",
      context: { tenant: "demo-co", customer_id: "CUS-100" },
    });
    const parsed = parseYaml(text);
    expect(parsed.ok).toBe(true);
    if (!parsed.ok) return;
    const doc = asScenario(parsed.value)!;
    expect(doc.apiVersion).toBe("agenttwin.dev/v1");
    expect(doc.metadata.name).toBe("late-delivery");
    expect(Object.keys(doc.spec).slice(0, 3)).toEqual(["agent", "twin", "input"]);
    expect(doc.spec.input?.context).toEqual({ tenant: "demo-co", customer_id: "CUS-100" });
    expect(doc.spec.expectations).toHaveLength(1);
    expect(text).toContain("# The final state must back any success the agent reports.");
  });
});

describe("newScenarioYaml without a message", () => {
  it("leaves the message empty for validation to point at, instead of saving instructions", () => {
    const text = newScenarioYaml({ name: "new-scenario" });
    const parsed = parseYaml(text);
    expect(parsed.ok).toBe(true);
    if (!parsed.ok) return;
    const doc = asScenario(parsed.value)!;
    expect(doc.spec.input?.message).toBe("");
    expect(doc.metadata.description).toBeUndefined();
    expect(text).toContain("# What the customer says to the agent.");
  });
});
