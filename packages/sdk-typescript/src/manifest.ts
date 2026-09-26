/**
 * Agent manifest helper.
 *
 * Reads an `agenttwin.dev/v1` agent manifest and exposes what
 * instrumentation needs: name, version, prompt hash and declared tool risks.
 * Validation and the canonical manifest identity are computed server-side
 * when the version is registered. JSON is read as is; for YAML pass a
 * parser (`loadManifest("agent.yaml", { parseYaml: YAML.parse })`) so the
 * SDK itself needs no dependency.
 */

import { readFile, stat } from "node:fs/promises";

import { sha256Hex } from "./hashing.js";

export const MANIFEST_API_VERSION = "agenttwin.dev/v1";
const MAX_BYTES = 1 << 20;

export interface AgentManifest {
  readonly name: string;
  readonly version: string;
  readonly instructions: string;
  /**
   * SHA-256 of the instructions, as the control plane computes it (or the
   * `promptRef` hash when the prompt lives elsewhere); pass it to model calls.
   */
  readonly promptHash: string | undefined;
  readonly modelProvider: string | undefined;
  readonly modelName: string | undefined;
  /** Declared risk level per tool (upper case). */
  readonly toolRisks: Readonly<Record<string, string>>;
  readonly raw: Readonly<Record<string, unknown>>;
  riskOf(tool: string): string | undefined;
}

function isObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/** Reads the parts of a parsed manifest document that instrumentation uses. */
export function parseManifest(doc: unknown): AgentManifest {
  if (!isObject(doc)) throw new TypeError("manifest must be an object");
  if (doc.apiVersion !== MANIFEST_API_VERSION || doc.kind !== "Agent") {
    throw new TypeError(`manifest must have apiVersion ${MANIFEST_API_VERSION} and kind Agent`);
  }
  const meta = isObject(doc.metadata) ? doc.metadata : {};
  const spec = isObject(doc.spec) ? doc.spec : {};
  const { name, version } = meta;
  if (typeof name !== "string" || typeof version !== "string" || !name || !version) {
    throw new TypeError("metadata.name and metadata.version are required");
  }
  const toolRisks: Record<string, string> = {};
  for (const tool of Array.isArray(spec.tools) ? spec.tools : []) {
    if (!isObject(tool) || typeof tool.name !== "string") continue;
    const level = isObject(tool.risk) ? tool.risk.level : tool.risk;
    if (typeof level === "string") toolRisks[tool.name] = level.toUpperCase();
  }
  const model = isObject(spec.model) ? spec.model : {};
  const instructions = typeof spec.instructions === "string" ? spec.instructions : "";
  const promptRef = isObject(spec.promptRef) ? spec.promptRef : undefined;
  const promptHash = instructions
    ? sha256Hex(instructions)
    : typeof promptRef?.sha256 === "string"
      ? promptRef.sha256
      : undefined;
  return Object.freeze({
    name,
    version,
    instructions,
    promptHash,
    modelProvider: typeof model.provider === "string" ? model.provider : undefined,
    modelName: typeof model.name === "string" ? model.name : undefined,
    toolRisks: Object.freeze(toolRisks),
    raw: doc,
    riskOf: (tool: string) => toolRisks[tool],
  });
}

export interface LoadManifestOptions {
  /** A YAML parser for `.yaml`/`.yml` files (e.g. `parse` of the `yaml` package). */
  readonly parseYaml?: (text: string) => unknown;
}

/** Loads a manifest file (`.json`; `.yaml`/`.yml` with `parseYaml`). */
export async function loadManifest(path: string, options: LoadManifestOptions = {}): Promise<AgentManifest> {
  if ((await stat(path)).size > MAX_BYTES) throw new RangeError("manifest larger than 1 MiB");
  const text = await readFile(path, "utf8");
  if (/\.ya?ml$/i.test(path)) {
    if (options.parseYaml === undefined) {
      throw new TypeError("reading a YAML manifest needs a parser: loadManifest(path, { parseYaml })");
    }
    return parseManifest(options.parseYaml(text));
  }
  return parseManifest(JSON.parse(text));
}
