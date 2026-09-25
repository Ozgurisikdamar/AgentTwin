/**
 * Pure helpers of the change pages: what a change set's items and impact
 * say, in words. Every function is total over what the contract allows
 * (paths are the graph service's objects, read defensively).
 */
import type {
  ChangeImpact,
  ChangeItem,
  ChangeSetSummary,
  GraphComponentRef,
  ImpactScenario,
} from "./api/control-plane";
import type { BadgeTone } from "@/components/ui/badge";
import { humanize } from "./format";

/** The local embedding model (ADR-0014): its similarity is lexical. */
export const HASHING_MODEL = "hashing-v1";

/**
 * How similarity is named (ADR-0014): the local hashing model compares words
 * and identifiers ("text similarity"); a hosted model compares meaning.
 */
export function similarityLabel(model: string | null | undefined): string {
  if (!model || model === HASHING_MODEL) return "text similarity";
  return "semantic similarity";
}

const KIND_LABEL: Record<string, string> = {
  AGENT: "Agent",
  AGENT_VERSION: "Agent version",
  PROMPT: "Prompt",
  MODEL: "Model",
  TOOL: "Tool",
  MCP_SERVER: "MCP server",
  HTTP_API: "HTTP API",
  SERVICE: "Service",
  DATABASE: "Database",
  QUEUE: "Queue",
  EXTERNAL_SYSTEM: "External system",
  RETRIEVAL_SOURCE: "Retrieval source",
  DATASET: "Dataset",
  POLICY: "Policy",
  SCENARIO: "Scenario",
  EVALUATOR: "Evaluator",
};

/** A graph component kind in words (unknown kinds humanized). */
export function kindLabel(kind: string | null | undefined): string {
  if (!kind) return "—";
  return KIND_LABEL[kind] ?? humanize(kind);
}

const ITEM_KIND_LABEL: Record<string, string> = {
  prompt: "Prompt",
  model: "Model",
  model_params: "Model parameters",
  limits: "Limits",
  tool: "Tool",
  retrieval_source: "Retrieval source",
  dependency: "Dependency",
  code: "Code",
  policy: "Policy",
  evaluator: "Evaluator",
  dataset: "Dataset",
};

export function itemKindLabel(kind: string): string {
  return ITEM_KIND_LABEL[kind] ?? humanize(kind);
}

const RELATION_PHRASE: Record<string, string> = {
  USES: "uses",
  CALLS: "calls",
  READS: "reads",
  WRITES: "writes",
  PUBLISHES: "publishes to",
  CONSUMES: "consumes",
  GUARDED_BY: "is guarded by",
  EVALUATED_BY: "is evaluated by",
  TESTED_BY: "is tested by",
  DEPENDS_ON: "depends on",
  RETRIEVES_FROM: "retrieves from",
  CAN_MUTATE: "can change",
  VERSION_OF: "is a version of",
};

/** "USES" → "uses": an edge type read from its source to its target. */
export function relationPhrase(edge: string): string {
  return RELATION_PHRASE[edge] ?? humanize(edge).toLowerCase();
}

/** One component of a path, with how it is related to the previous one. */
export interface ChainLink {
  kind: string;
  key: string;
  label: string;
  edge: string | null;
  /** `down`: what the previous component acts on; `up`: who depends on it. */
  direction: "down" | "up" | null;
  /** "support-refund-agent@1.3.0 uses refund_payment" (null for the first). */
  sentence: string | null;
}

function str(v: unknown): string {
  return typeof v === "string" ? v : "";
}

/** An object's fields; anything else reads as having none. */
function record(v: unknown): Record<string, unknown> {
  return v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : {};
}

/**
 * A graph service path (its `Step`s, from the changed component) as links
 * that each say how they relate to the previous component.
 */
export function pathChain(path: readonly unknown[] | null | undefined): ChainLink[] {
  const out: ChainLink[] = [];
  for (const raw of path ?? []) {
    if (!raw || typeof raw !== "object") continue;
    const step = record(raw);
    const component = record(step.component);
    const kind = str(component.kind);
    const key = str(component.key);
    const label = str(step.label) || key;
    const edge = str(step.edge) || null;
    const direction = step.direction === "down" || step.direction === "up" ? step.direction : null;
    const prev = out[out.length - 1];
    let sentence: string | null = null;
    if (prev && edge) {
      const phrase = relationPhrase(edge);
      sentence = direction === "up" ? `${label} ${phrase} ${prev.label}` : `${prev.label} ${phrase} ${label}`;
    }
    out.push({ kind, key, label, edge, direction, sentence });
  }
  return out;
}

/** Why a scenario was selected, as badges (strongest first). */
export interface ReasonTag {
  key: "linked" | "weak" | "similar" | "always" | "regression";
  label: string;
  tone: BadgeTone;
}

export function reasonTags(s: ImpactScenario, embeddingModel?: string): ReasonTag[] {
  const out: ReasonTag[] = [];
  const graph = s.reasons.graph;
  if (graph.some((r) => r.direct)) out.push({ key: "linked", label: "Linked to the change", tone: "danger" });
  else if (graph.length) out.push({ key: "weak", label: "Weakly linked", tone: "warning" });
  if (s.reasons.known_regression) out.push({ key: "regression", label: "Known regression", tone: "danger" });
  if (s.reasons.similar.length) {
    const best = Math.max(...s.reasons.similar.map((x) => x.similarity));
    out.push({
      key: "similar",
      label: `${similarityLabel(embeddingModel)} ${best.toFixed(2)}`,
      tone: "info",
    });
  }
  if (s.reasons.always_run_tags.length) {
    out.push({
      key: "always",
      label: `Always runs (${s.reasons.always_run_tags.join(", ")})`,
      tone: "brand",
    });
  }
  return out;
}

/** Whether the graph links a scenario to the change directly (a strong link). */
export function stronglyLinked(s: ImpactScenario): boolean {
  return s.reasons.graph.some((r) => r.direct) || s.reasons.known_regression;
}

/** The impact's verdict in one line. */
export function impactHeadline(impact: ChangeImpact): string {
  const n = impact.scenarios.length;
  const noun = n === 1 ? "scenario" : "scenarios";
  const strong = impact.scenarios.filter(stronglyLinked).length;
  const base = `${n} ${noun} required`;
  if (n === 0) return "No scenario is required by this change";
  return strong && strong < n ? `${base} · ${strong} linked to the change` : base;
}

/**
 * The new-simulation form, prefilled with the candidate version and the
 * scenarios an impact requires (those of the library only), linking back to
 * the change set.
 */
export function simulateHref(impact: ChangeImpact): string {
  const names = impact.scenarios.filter((s) => s.in_library).map((s) => s.name);
  const q = new URLSearchParams({
    project_id: impact.project_id,
    agent: impact.agent,
    version: impact.candidate_version,
    change_set: impact.change_set_id,
  });
  if (names.length) q.set("scenarios", names.join(","));
  return `/simulations/new?${q.toString()}`;
}

/** The graph explorer centred on a component (by kind and key). */
export function graphHref(projectId: string, component: GraphComponentRef): string {
  const q = new URLSearchParams({ project_id: projectId, kind: component.kind, key: component.key });
  return `/graph?${q.toString()}`;
}

/** "1 prompt change, 2 tool changes" from a change set's counts per kind. */
export function changeCountsLine(summary: ChangeSetSummary["summary"]): string {
  const kinds = Object.entries(summary.kinds ?? {})
    .filter(([, n]) => n > 0)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([kind, n]) => `${n} ${itemKindLabel(kind).toLowerCase()} ${n === 1 ? "change" : "changes"}`);
  if (kinds.length) return kinds.join(", ");
  return summary.items === 0
    ? "No changes"
    : `${summary.items} ${summary.items === 1 ? "change" : "changes"}`;
}

/** A prompt item's redacted line diff, if the caller may read it. */
export interface PromptDiff {
  lines: { op: " " | "-" | "+"; text: string }[];
  truncated: boolean;
  mentions: string[];
  /** Why there is no diff (hash only, or not allowed). */
  unavailable: string | null;
}

export function promptDiff(item: ChangeItem): PromptDiff | null {
  if (item.kind !== "prompt") return null;
  const d = record(item.detail);
  const lines = Array.isArray(d.diff)
    ? d.diff.flatMap((l) => {
        const line = record(l);
        const op = line.op === "-" || line.op === "+" ? line.op : " ";
        return typeof line.text === "string" ? [{ op: op as " " | "-" | "+", text: line.text }] : [];
      })
    : [];
  const mentions = Array.isArray(d.mentions)
    ? d.mentions.filter((m): m is string => typeof m === "string")
    : [];
  const unavailable = typeof d.diff_unavailable === "string" ? d.diff_unavailable : null;
  return { lines, truncated: d.diff_truncated === true, mentions, unavailable };
}

/** A tool item's schema changes, in words. */
export function schemaChanges(item: ChangeItem): { path: string; change: string; breaking: boolean }[] {
  if (item.kind !== "tool") return [];
  const d = record(item.detail);
  if (!Array.isArray(d.schema_changes)) return [];
  return d.schema_changes.flatMap((c) => {
    const x = record(c);
    return typeof x.path === "string"
      ? [{ path: x.path || "(root)", change: str(x.change), breaking: x.breaking === true }]
      : [];
  });
}

const CONFIDENCE_LABEL: Record<string, string> = {
  exact: "compared",
  hash_only: "hash only",
  filenames_only: "file names only",
  declared: "declared",
};

export function confidenceLabel(c: string): string {
  return CONFIDENCE_LABEL[c] ?? humanize(c);
}

/** What an item is about, short: a prompt by its hash, a commit shortened. */
export function itemSubject(item: ChangeItem): string {
  if (item.kind === "prompt") return `prompt ${item.subject.slice(0, 12)}`;
  if (item.kind === "code" && /^[0-9a-f]{40,64}$/i.test(item.subject)) return item.subject.slice(0, 12);
  return item.subject;
}

/** A detail value in words: lists joined, objects as `name: value` pairs. */
export function detailText(v: unknown): string {
  if (v === null || v === undefined) return "";
  if (typeof v === "string") return v;
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  if (Array.isArray(v)) return v.map(detailText).filter(Boolean).join(", ");
  if (typeof v === "object") {
    return Object.entries(v as Record<string, unknown>)
      .map(([k, x]) => `${k}: ${detailText(x) || "—"}`)
      .join("; ");
  }
  return "";
}

function short(v: unknown, n = 12): string {
  return typeof v === "string" ? v.slice(0, n) : "";
}

/** A change item's detail as labelled facts (the prompt diff and schema changes are shown apart). */
export function itemFacts(item: ChangeItem): { label: string; value: string }[] {
  const d = record(item.detail);
  const out: { label: string; value: string }[] = [];
  const add = (label: string, value: string) => {
    if (value) out.push({ label, value });
  };
  switch (item.kind) {
    case "prompt":
      if (d.base_sha256 || d.candidate_sha256) {
        add("Hashes", `${short(d.base_sha256) || "—"} → ${short(d.candidate_sha256) || "—"}`);
      }
      break;
    case "tool": {
      const risk = d.risk;
      if (risk && typeof risk === "object" && !Array.isArray(risk)) {
        const r = record(risk);
        add(
          "Risk",
          `${detailText(r.from)} → ${detailText(r.to)}${r.escalated === true ? " (escalated)" : ""}`,
        );
      } else {
        add("Risk", detailText(risk));
      }
      if (d.new_privilege === true) add("New privilege", "yes: the agent can do something it could not");
      add("Changed", detailText(d.aspects));
      add("Permissions", detailText(d.permissions));
      add("Approval", detailText(d.approval));
      add("Version", detailText(d.version));
      break;
    }
    case "code": {
      if (d.base_commit || d.candidate_commit) {
        add("Commits", `${short(d.base_commit) || "—"} → ${short(d.candidate_commit) || "—"}`);
      }
      const files = Array.isArray(d.changed_files)
        ? d.changed_files.filter((f) => typeof f === "string")
        : [];
      const count = typeof d.changed_file_count === "number" ? d.changed_file_count : files.length;
      if (count) {
        const shown = files.slice(0, 10).join(", ");
        const more = count - Math.min(files.length, 10);
        add("Changed files", `${count}${shown ? `: ${shown}` : ""}${more > 0 ? ` and ${more} more` : ""}`);
      }
      break;
    }
    case "model_params":
      add("Parameters", detailText(d.parameters));
      add("From", detailText(d.from));
      add("To", detailText(d.to));
      break;
    case "limits":
      add("Limits", detailText(d.limits));
      add("From", detailText(d.from));
      add("To", detailText(d.to));
      break;
    default:
      add("From", detailText(d.from));
      add("To", detailText(d.to));
  }
  return out;
}
