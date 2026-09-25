import { describe, expect, it } from "vitest";
import { preselectedScenarios } from "@/components/simulations/new-simulation";
import type { ChangeImpact, ChangeItem, ImpactScenario } from "@/lib/api/control-plane";
import {
  changeCountsLine,
  confidenceLabel,
  detailText,
  graphHref,
  impactHeadline,
  itemFacts,
  itemKindLabel,
  itemSubject,
  kindLabel,
  pathChain,
  promptDiff,
  reasonTags,
  relationPhrase,
  schemaChanges,
  similarityLabel,
  simulateHref,
  stronglyLinked,
} from "@/lib/changes";
import { livePromptChange, livePromptImpact, liveToolChange, liveToolImpact } from "./change-fixtures";

const promptImpact = livePromptImpact as ChangeImpact;
const toolImpact = liveToolImpact as ChangeImpact;
const scenario = (impact: ChangeImpact, name: string): ImpactScenario =>
  impact.scenarios.find((s) => s.name === name)!;
const affected = (impact: ChangeImpact, kind: string, key: string) =>
  impact.graph!.affected.find((a) => a.component.kind === kind && a.component.key === key)!;

describe("similarityLabel (ADR-0014)", () => {
  it("calls the local hashing model's similarity textual and a hosted model's semantic", () => {
    expect(similarityLabel("hashing-v1")).toBe("text similarity");
    expect(similarityLabel("")).toBe("text similarity");
    expect(similarityLabel(undefined)).toBe("text similarity");
    expect(similarityLabel("text-embedding-3-small")).toBe("semantic similarity");
  });
});

describe("labels", () => {
  it("names kinds, item kinds, relations and confidences in words", () => {
    expect(kindLabel("HTTP_API")).toBe("HTTP API");
    expect(kindLabel("AGENT_VERSION")).toBe("Agent version");
    expect(kindLabel("SOMETHING_NEW")).toBe("Something new");
    expect(kindLabel("")).toBe("—");
    expect(itemKindLabel("model_params")).toBe("Model parameters");
    expect(itemKindLabel("brand_new")).toBe("Brand new");
    expect(relationPhrase("CAN_MUTATE")).toBe("can change");
    expect(relationPhrase("TESTED_BY")).toBe("is tested by");
    expect(relationPhrase("MIRRORS_TO")).toBe("mirrors to");
    expect(confidenceLabel("exact")).toBe("compared");
    expect(confidenceLabel("filenames_only")).toBe("file names only");
    expect(confidenceLabel("guessed")).toBe("Guessed");
  });
});

describe("pathChain", () => {
  it("reads the live prompt path to the payments service as sentences, each edge the way it points", () => {
    const chain = pathChain(affected(promptImpact, "SERVICE", "payments-api").path);
    expect(chain.map((l) => l.label)).toEqual([
      "prompt 49482e32190e",
      "support-refund-agent@1.3.0",
      "refund_payment",
      "payments-api",
    ]);
    expect(chain.map((l) => l.sentence)).toEqual([
      null,
      // `up`: the agent version depends on the prompt, not the other way round.
      "support-refund-agent@1.3.0 uses prompt 49482e32190e",
      "support-refund-agent@1.3.0 uses refund_payment",
      "refund_payment calls payments-api",
    ]);
    expect(chain.map((l) => l.direction)).toEqual([null, "up", "down", "down"]);
    expect(chain[3]).toMatchObject({ kind: "SERVICE", key: "payments-api", edge: "CALLS" });
  });

  it("names the HTTP API the tool can change", () => {
    const chain = pathChain(affected(promptImpact, "HTTP_API", "payments-api").path);
    expect(chain.at(-1)?.sentence).toBe("refund_payment can change payments-api");
  });

  it("skips what is not a step and falls back to the key for a missing label", () => {
    expect(pathChain(null)).toEqual([]);
    expect(pathChain(undefined)).toEqual([]);
    const chain = pathChain([
      null,
      "prompt",
      { component: { kind: "TOOL", key: "refund_payment" } },
      { component: { kind: "SERVICE", key: "payments-api" }, edge: "CALLS", direction: "sideways" },
    ]);
    expect(chain.map((l) => l.label)).toEqual(["refund_payment", "payments-api"]);
    expect(chain[1]!.direction).toBeNull();
    expect(chain[1]!.sentence).toBe("refund_payment calls payments-api");
  });
});

describe("reasonTags", () => {
  it("tags a scenario the change reaches directly, by similarity and by an always-run tag", () => {
    const tags = reasonTags(scenario(promptImpact, "cross-tenant-order"), promptImpact.embedding_model);
    expect(tags.map((t) => [t.key, t.label, t.tone])).toEqual([
      ["linked", "Linked to the change", "danger"],
      ["similar", "text similarity 0.28", "info"],
      ["always", "Always runs (security)", "brand"],
    ]);
  });

  it("tags a link only through the agent as weak", () => {
    const tags = reasonTags(scenario(promptImpact, "malicious-retrieved-content"), "hashing-v1");
    expect(tags.map((t) => t.key)).toEqual(["weak", "always"]);
    expect(stronglyLinked(scenario(promptImpact, "malicious-retrieved-content"))).toBe(false);
    expect(stronglyLinked(scenario(promptImpact, "refund-happy-path"))).toBe(true);
  });

  it("shows the best similarity under the hosted model's name, and a known regression", () => {
    const s = structuredClone(scenario(promptImpact, "refund-happy-path"));
    s.reasons.similar.push({ item: s.reasons.similar[0]!.item, similarity: 0.91 });
    s.reasons.known_regression = true;
    s.reasons.graph = [];
    const tags = reasonTags(s, "text-embedding-3-small");
    expect(tags.map((t) => t.label)).toEqual(["Known regression", "semantic similarity 0.91"]);
    expect(stronglyLinked(s)).toBe(true);
  });
});

describe("impactHeadline", () => {
  it("counts the required scenarios and those linked to the change", () => {
    expect(impactHeadline(promptImpact)).toBe("9 scenarios required · 7 linked to the change");
    // The tool change links every refund scenario directly: 7 of 9 again.
    expect(impactHeadline(toolImpact)).toBe("9 scenarios required · 7 linked to the change");
  });

  it("does not repeat the count when every scenario is linked, and says when none is required", () => {
    const one = { ...promptImpact, scenarios: [scenario(promptImpact, "refund-happy-path")] };
    expect(impactHeadline(one)).toBe("1 scenario required");
    expect(impactHeadline({ ...promptImpact, scenarios: [] })).toBe("No scenario is required by this change");
  });
});

describe("simulateHref", () => {
  it("prefills the candidate version and the required scenarios, linking back to the change set", () => {
    const url = new URL(simulateHref(promptImpact), "http://x");
    expect(url.pathname).toBe("/simulations/new");
    expect(url.searchParams.get("project_id")).toBe(promptImpact.project_id);
    expect(url.searchParams.get("agent")).toBe("support-refund-agent");
    expect(url.searchParams.get("version")).toBe("1.3.0");
    expect(url.searchParams.get("change_set")).toBe(promptImpact.change_set_id);
    expect(url.searchParams.get("scenarios")?.split(",")).toEqual(promptImpact.scenarios.map((s) => s.name));
  });

  it("leaves out scenarios the library could not confirm", () => {
    const scenarios = promptImpact.scenarios.map((s, i) => ({ ...s, in_library: i !== 0 }));
    const url = new URL(simulateHref({ ...promptImpact, scenarios }), "http://x");
    expect(url.searchParams.get("scenarios")?.split(",")).not.toContain(scenarios[0]!.name);
    const none = new URL(simulateHref({ ...promptImpact, scenarios: [] }), "http://x");
    expect(none.searchParams.has("scenarios")).toBe(false);
  });

  it("reads back through the new-simulation form's preselection", () => {
    const url = new URL(simulateHref(toolImpact), "http://x");
    expect([...preselectedScenarios(url.searchParams.get("scenarios"))!]).toEqual(
      toolImpact.scenarios.map((s) => s.name),
    );
    expect(preselectedScenarios(null)).toBeNull();
    expect(preselectedScenarios(" , ,")).toBeNull();
    expect([...preselectedScenarios(" a, b ,a")!]).toEqual(["a", "b"]);
  });
});

describe("graphHref", () => {
  it("centres the graph on a component by kind and key", () => {
    const url = new URL(graphHref("p-1", { kind: "TOOL", key: "refund_payment" }), "http://x");
    expect(url.pathname).toBe("/graph");
    expect(Object.fromEntries(url.searchParams)).toEqual({
      project_id: "p-1",
      kind: "TOOL",
      key: "refund_payment",
    });
  });
});

describe("change items", () => {
  const prompt = livePromptChange.items[0] as ChangeItem;
  const tool = liveToolChange.items[0] as ChangeItem;

  it("reads the live prompt diff: masked lines, the tools they mention", () => {
    const diff = promptDiff(prompt)!;
    expect(diff.lines).toHaveLength(12);
    expect(diff.lines.filter((l) => l.op === "+").map((l) => l.text)).toContain(
      "If refund_payment fails for any reason, simply retry the refund right away.",
    );
    expect(diff.lines.filter((l) => l.op === "-")).toHaveLength(5);
    expect(diff.mentions).toEqual(["get_refund_policy", "lookup_order", "refund_payment"]);
    expect(diff.truncated).toBe(false);
    expect(diff.unavailable).toBeNull();
    expect(promptDiff(tool)).toBeNull();
  });

  it("says why a prompt has no diff and drops malformed lines", () => {
    const hashOnly = {
      ...prompt,
      confidence: "hash_only",
      detail: { diff_unavailable: "the text is not stored" },
    };
    expect(promptDiff(hashOnly as ChangeItem)).toEqual({
      lines: [],
      truncated: false,
      mentions: [],
      unavailable: "the text is not stored",
    });
    const odd = { ...prompt, detail: { diff: [{ op: "?", text: "x" }, { op: "+" }, null, "y"] } };
    expect(promptDiff(odd as unknown as ChangeItem)!.lines).toEqual([{ op: " ", text: "x" }]);
  });

  it("reads the live tool contract change: two breaking schema changes", () => {
    expect(schemaChanges(tool)).toEqual([
      { path: "/properties/idempotency_key", change: "required_added", breaking: true },
      { path: "/properties/idempotency_key/minLength", change: "constraint_changed", breaking: true },
    ]);
    expect(schemaChanges(prompt)).toEqual([]);
    const root = {
      ...tool,
      detail: {
        schema_changes: [
          { path: "", change: "type_changed", breaking: true },
          7,
          { path: "/properties/note", change: "property_added", breaking: false },
          { path: "/properties/amount/description", change: "description_changed" },
        ],
      },
    };
    // Only what the service calls breaking is: a missing flag is compatible.
    expect(schemaChanges(root as unknown as ChangeItem)).toEqual([
      { path: "(root)", change: "type_changed", breaking: true },
      { path: "/properties/note", change: "property_added", breaking: false },
      { path: "/properties/amount/description", change: "description_changed", breaking: false },
    ]);
  });

  it("names the subject short and lists the detail as facts", () => {
    expect(itemSubject(prompt)).toBe("prompt 49482e32190e");
    expect(itemSubject(tool)).toBe("refund_payment");
    expect(itemFacts(prompt)).toEqual([{ label: "Hashes", value: "679eec662b65 → 49482e32190e" }]);
    expect(itemFacts(tool)).toEqual([{ label: "Changed", value: "description, schema" }]);
    const escalated = {
      ...tool,
      detail: { risk: { from: "READ", to: "WRITE_IRREVERSIBLE", escalated: true }, new_privilege: true },
    };
    expect(itemFacts(escalated as ChangeItem)).toEqual([
      { label: "Risk", value: "READ → WRITE_IRREVERSIBLE (escalated)" },
      { label: "New privilege", value: "yes: the agent can do something it could not" },
    ]);
    const code = {
      kind: "code",
      subject: "a".repeat(40),
      change: "modified",
      summary: "",
      confidence: "filenames_only",
      breaking: false,
      detail: {
        base_commit: "b".repeat(40),
        candidate_commit: "a".repeat(40),
        changed_file_count: 12,
        changed_files: Array.from({ length: 12 }, (_, i) => `f${i}.py`),
      },
    } as ChangeItem;
    expect(itemSubject(code)).toBe("a".repeat(12));
    expect(itemFacts(code)).toEqual([
      { label: "Commits", value: `${"b".repeat(12)} → ${"a".repeat(12)}` },
      {
        label: "Changed files",
        value: "12: f0.py, f1.py, f2.py, f3.py, f4.py, f5.py, f6.py, f7.py, f8.py, f9.py and 2 more",
      },
    ]);
    const model = {
      ...code,
      kind: "model",
      detail: { from: "openai/gpt-a", to: "openai/gpt-b" },
    } as ChangeItem;
    expect(itemFacts(model)).toEqual([
      { label: "From", value: "openai/gpt-a" },
      { label: "To", value: "openai/gpt-b" },
    ]);
  });

  it("writes nested detail values in words", () => {
    expect(detailText({ temperature: 0.2, stop: ["a", "b"], x: null })).toBe(
      "temperature: 0.2; stop: a, b; x: —",
    );
    expect(detailText(undefined)).toBe("");
    expect(detailText(true)).toBe("true");
  });
});

describe("changeCountsLine", () => {
  it("counts per kind in words", () => {
    expect(changeCountsLine(livePromptChange.summary)).toBe("1 prompt change");
    expect(changeCountsLine({ ...livePromptChange.summary, items: 3, kinds: { tool: 2, prompt: 1 } })).toBe(
      "1 prompt change, 2 tool changes",
    );
    expect(changeCountsLine({ ...livePromptChange.summary, items: 0, kinds: {} })).toBe("No changes");
    expect(changeCountsLine({ ...livePromptChange.summary, items: 2, kinds: {} })).toBe("2 changes");
  });
});
