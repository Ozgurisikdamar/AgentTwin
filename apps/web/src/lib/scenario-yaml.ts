/**
 * Scenario YAML in the browser. The YAML text is the source of truth of the
 * editor: the form reads parsed values and writes each change back into the
 * text with the `yaml` document API, so comments, key order and formatting
 * the author chose survive form edits. The server remains the authority on
 * validity (schema, twin cross-checks); this only parses.
 */
import { type Document, isCollection, isMap, isScalar, isSeq, parseDocument } from "yaml";
import type { ScenarioDocument } from "./types";

export type YamlPath = readonly (string | number)[];

export type ParsedYaml =
  { ok: true; value: Record<string, unknown> } | { ok: false; error: string; line: number | null };

const PARSE_OPTIONS = { prettyErrors: true, strict: true, uniqueKeys: true } as const;

/** Parses scenario text; a syntax error reports its first line. */
export function parseYaml(text: string): ParsedYaml {
  const doc = parseDocument(text, PARSE_OPTIONS);
  const err = doc.errors[0];
  if (err) {
    const line = err.linePos?.[0]?.line ?? null;
    return { ok: false, error: err.message.split("\n")[0] ?? "Invalid YAML.", line };
  }
  let value: unknown;
  try {
    value = doc.toJS({ maxAliasCount: 100 });
  } catch (e) {
    return { ok: false, error: e instanceof Error ? e.message : "Invalid YAML.", line: null };
  }
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return {
      ok: false,
      error: "A scenario is a YAML mapping (key: value pairs) at the top level.",
      line: null,
    };
  }
  return { ok: true, value: value as Record<string, unknown> };
}

/** Reads a value at a path of a parsed document. */
export function getIn(value: unknown, path: YamlPath): unknown {
  let cur: unknown = value;
  for (const key of path) {
    if (cur === null || typeof cur !== "object") return undefined;
    cur = (cur as Record<string | number, unknown>)[key];
  }
  return cur;
}

/** Values that remove a key instead of writing it (empty lists are written). */
function removes(value: unknown): boolean {
  return value === undefined || value === "";
}

/**
 * Returns `text` with the value at `path` replaced (undefined or "" removes
 * the key). Unrelated parts of the text are kept as written. Throws when the
 * text cannot be parsed - the form is only offered for parseable YAML.
 */
export function setIn(text: string, path: YamlPath, value: unknown): string {
  const doc = parseDocument(text, PARSE_OPTIONS);
  if (doc.errors.length) throw new Error("The YAML must be valid before it can be edited with the form.");
  if (removes(value)) {
    if (doc.hasIn(path)) doc.deleteIn(path);
  } else {
    replaceValue(doc, path, value);
  }
  return doc.toString({ lineWidth: 0 });
}

/** Writes a value where one exists, keeping the node's comments and its
 *  flow or block style; creates the key (and missing parents) otherwise. */
function replaceValue(doc: Document, path: YamlPath, value: unknown): void {
  const existing = doc.getIn(path, true);
  const scalar = value === null || typeof value !== "object";
  if (isScalar(existing) && scalar) {
    existing.value = value;
    return;
  }
  const node = doc.createNode(value);
  if (isCollection(existing) && isCollection(node)) {
    node.flow = existing.flow;
    node.comment = existing.comment;
    node.commentBefore = existing.commentBefore;
  }
  doc.setIn(path, node);
}

/** Appends an item to the list at `path` (created when missing). */
export function appendIn(text: string, path: YamlPath, item: unknown): string {
  const doc = parseDocument(text, PARSE_OPTIONS);
  if (doc.errors.length) throw new Error("The YAML must be valid before it can be edited with the form.");
  const list = doc.getIn(path, true);
  if (isSeq(list)) list.add(doc.createNode(item));
  else doc.setIn(path, doc.createNode([item]));
  return doc.toString({ lineWidth: 0 });
}

/** Removes the item at `index` of the list at `path`. */
export function removeAt(text: string, path: YamlPath, index: number): string {
  const doc = parseDocument(text, PARSE_OPTIONS);
  if (doc.errors.length) throw new Error("The YAML must be valid before it can be edited with the form.");
  const list = doc.getIn(path, true);
  if (isSeq(list) && index >= 0 && index < list.items.length) list.delete(index);
  return doc.toString({ lineWidth: 0 });
}

/** Whether the top level is a mapping (the form needs one). */
export function hasMappingRoot(text: string): boolean {
  const doc = parseDocument(text, PARSE_OPTIONS);
  return !doc.errors.length && isMap(doc.contents);
}

/** The scenario name written in the text, when there is one. */
export function scenarioName(value: Record<string, unknown> | null | undefined): string | null {
  const name = getIn(value, ["metadata", "name"]);
  return typeof name === "string" && name ? name : null;
}

export interface ScenarioTemplate {
  name: string;
  agent?: string;
  twin?: string;
  message?: string;
  context?: Record<string, string>;
}

/** A documented starting point for a new scenario. */
export function newScenarioYaml(t: ScenarioTemplate): string {
  const doc = parseDocument(
    [
      "# A scenario runs one agent conversation against a tool twin, with optional",
      "# injected faults, and checks the trajectory and the final twin state.",
      "apiVersion: agenttwin.dev/v1",
      "kind: Scenario",
      "metadata:",
      "  name: placeholder",
      "  severity: high",
      "  tags: []",
      "spec:",
      "  input:",
      "    # What the customer says to the agent.",
      "    message: placeholder",
      "  faults: []",
      "  expectations:",
      "    # The final state must back any success the agent reports.",
      "    - id: success-backed-by-state",
      "      type: outcomeVerified",
      "      critical: true",
      "",
    ].join("\n"),
  );
  doc.setIn(["metadata", "name"], t.name);
  const spec = doc.get("spec", true);
  if (isMap(spec)) {
    // agent and twin go first, in reading order.
    if (t.twin) spec.items.unshift(doc.createPair("twin", t.twin));
    if (t.agent) spec.items.unshift(doc.createPair("agent", t.agent));
  }
  // Left empty rather than filled with instructions that could be saved by mistake: validation
  // points at it until it is written.
  doc.setIn(["spec", "input", "message"], t.message ?? "");
  if (t.context && Object.keys(t.context).length) doc.setIn(["spec", "input", "context"], t.context);
  return doc.toString({ lineWidth: 0 });
}

/** The parsed value as a scenario document, when it has the basic shape. */
export function asScenario(value: Record<string, unknown>): ScenarioDocument | null {
  const meta = value.metadata;
  const spec = value.spec;
  if (!meta || typeof meta !== "object" || !spec || typeof spec !== "object") return null;
  return value as unknown as ScenarioDocument;
}
