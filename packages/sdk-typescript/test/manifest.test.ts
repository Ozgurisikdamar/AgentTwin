import { mkdtempSync, writeFileSync } from "node:fs";
import { readFileSync, readdirSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { parse as parseYaml } from "yaml";

import { sha256Hex } from "../src/hashing.js";
import { loadManifest, parseManifest } from "../src/manifest.js";
import { REPO } from "./helpers.js";

const DEMO = path.join(REPO, "demo", "support-refund-agent", "manifests");

describe("agent manifest helper", () => {
  it("reads every demo manifest: name, version, prompt hash, tool risks", async () => {
    const files = readdirSync(DEMO).filter((f) => f.endsWith(".yaml"));
    expect(files.length).toBeGreaterThanOrEqual(4);
    for (const file of files) {
      const text = readFileSync(path.join(DEMO, file), "utf8");
      const m = await loadManifest(path.join(DEMO, file), { parseYaml });
      const doc = parseYaml(text) as { metadata: { version: string }; spec: { instructions: string } };
      expect(m.name).toBe("support-refund-agent");
      expect(m.version).toBe(doc.metadata.version);
      expect(m.promptHash).toBe(sha256Hex(doc.spec.instructions));
      expect(m.riskOf("lookup_order")).toBe("READ");
      expect(m.riskOf("refund_payment")).toBe("WRITE_IRREVERSIBLE"); // risk: {level: ...}
      expect(m.riskOf("unknown_tool")).toBeUndefined();
      expect(m.modelProvider).toBe("scripted");
    }
  });

  it("the prompt of a promptRef manifest is the reference's hash, as the control plane records it", () => {
    const m = parseManifest({
      apiVersion: "agenttwin.dev/v1",
      kind: "Agent",
      metadata: { name: "a", version: "1" },
      spec: {
        promptRef: { name: "p", sha256: "ab".repeat(32) },
        tools: [{ name: "t", risk: "read" }, "junk", { risk: "READ" }],
      },
    });
    expect(m.promptHash).toBe("ab".repeat(32));
    expect(m.instructions).toBe("");
    expect(m.toolRisks).toEqual({ t: "READ" });
    expect(
      parseManifest({ apiVersion: "agenttwin.dev/v1", kind: "Agent", metadata: { name: "a", version: "1" } })
        .promptHash,
    ).toBeUndefined();
  });

  it("reads JSON; YAML needs a parser; rejects what is not an agent manifest", async () => {
    const dir = mkdtempSync(path.join(tmpdir(), "manifest-"));
    const json = path.join(dir, "agent.json");
    writeFileSync(
      json,
      JSON.stringify({
        apiVersion: "agenttwin.dev/v1",
        kind: "Agent",
        metadata: { name: "j", version: "2" },
        spec: { instructions: "hi" },
      }),
    );
    expect((await loadManifest(json)).promptHash).toBe(sha256Hex("hi"));
    await expect(loadManifest(path.join(DEMO, "1.3.1.yaml"))).rejects.toThrow(/parser/);
    const big = path.join(dir, "big.json");
    writeFileSync(big, " ".repeat((1 << 20) + 1));
    await expect(loadManifest(big)).rejects.toThrow(RangeError);
    for (const bad of [
      null,
      [],
      { apiVersion: "v0", kind: "Agent" },
      { apiVersion: "agenttwin.dev/v1", kind: "Scenario" },
      { apiVersion: "agenttwin.dev/v1", kind: "Agent", metadata: { name: "x" } },
    ]) {
      expect(() => parseManifest(bad)).toThrow(TypeError);
    }
  });
});
