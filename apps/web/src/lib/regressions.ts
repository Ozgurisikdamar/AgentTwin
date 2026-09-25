/**
 * Pure helpers of the regression pages (spec §18, §41.3): the actions a
 * regression's status allows — the lifecycle of the evaluation service's
 * `mining.transition` — and what a regression and its history say, in words.
 * The service decides every action; these only decide what the page offers.
 */
import type { BadgeTone } from "@/components/ui/badge";
import type { FailureLabel, Regression, RegressionEvent, RegressionStatus, Severity } from "./api/evaluation";
import { humanize, shortId } from "./format";
import { actorLabel } from "./simulations";

export const SEVERITIES: readonly Severity[] = ["critical", "high", "medium", "low"];

/** The failure taxonomy of spec §18.3, in the contract's order. */
export const FAILURE_LABELS: readonly FailureLabel[] = [
  "WRONG_TOOL",
  "WRONG_ARGUMENT",
  "MISSING_TOOL",
  "TOOL_ERROR_HANDLING",
  "DUPLICATE_SIDE_EFFECT",
  "RETRY_SAFETY",
  "LOOP",
  "TIMEOUT",
  "COST_BUDGET",
  "LATENCY",
  "POLICY_VIOLATION",
  "AUTHORIZATION",
  "PROMPT_INJECTION",
  "DATA_LEAKAGE",
  "STALE_CONTEXT",
  "RETRIEVAL_FAILURE",
  "HALLUCINATED_SUCCESS",
  "INCORRECT_ESCALATION",
  "MISSING_APPROVAL",
  "STATE_MISMATCH",
  "UNKNOWN",
];

/** The inbox's views: which statuses each lists. */
export const STATUS_VIEWS = [
  { value: "open", label: "Needs a decision", statuses: ["CANDIDATE", "CONFIRMED", "REOPENED"] },
  { value: "promoted", label: "Has a test", statuses: ["PROMOTED"] },
  { value: "fixed", label: "Fixed", statuses: ["FIXED"] },
  { value: "dismissed", label: "Dismissed", statuses: ["DISMISSED"] },
  { value: "all", label: "All", statuses: [] },
] as const satisfies readonly { value: string; label: string; statuses: readonly RegressionStatus[] }[];

export type StatusView = (typeof STATUS_VIEWS)[number];

/** The view a URL names; the inbox opens on what still needs a decision. */
export function statusView(value: string | null | undefined): StatusView {
  return STATUS_VIEWS.find((v) => v.value === value) ?? STATUS_VIEWS[0];
}

const STATUS: Record<RegressionStatus, { label: string; tone: BadgeTone; says: string }> = {
  CANDIDATE: {
    label: "Candidate",
    tone: "warning",
    says: "Found by the miner; nobody has looked at it yet.",
  },
  CONFIRMED: { label: "Confirmed", tone: "danger", says: "A person agrees it is a failure worth a test." },
  PROMOTED: {
    label: "Has a test",
    tone: "brand",
    says: "It is a regression test every evaluation of this agent runs; it is fixed when a version passes it.",
  },
  FIXED: { label: "Fixed", tone: "success", says: "A later version passed its regression test." },
  DISMISSED: { label: "Dismissed", tone: "neutral", says: "Not a failure worth a test." },
  REOPENED: {
    label: "Reopened",
    tone: "danger",
    says: "It was dismissed or fixed, and it is back.",
  },
};

export function statusBadge(status: RegressionStatus): { label: string; tone: BadgeTone } {
  const s = STATUS[status];
  return s ? { label: s.label, tone: s.tone } : { label: humanize(status), tone: "neutral" };
}

/** What a status means, in a sentence. */
export function statusMeaning(status: RegressionStatus): string {
  return STATUS[status]?.says ?? "";
}

/** A failure label as people read it: `DUPLICATE_SIDE_EFFECT` → "Duplicate side effect". */
export function taxonomyLabel(label: string | null | undefined): string {
  return humanize(label);
}

export type RegressionAction = "confirm" | "promote" | "dismiss" | "reopen" | "merge";

/** Where each status change is possible from (`mining.transition`). */
const FROM: Record<Exclude<RegressionAction, "merge">, readonly RegressionStatus[]> = {
  confirm: ["CANDIDATE", "REOPENED"],
  promote: ["CANDIDATE", "CONFIRMED", "REOPENED"],
  dismiss: ["CANDIDATE", "CONFIRMED", "REOPENED"],
  reopen: ["DISMISSED", "FIXED"],
};

/**
 * The actions a regression allows now. A merged group allows none (its
 * failures live in the other one); a group with a test is never promoted
 * again (a reopened one waits for a fix) nor merged away (merge the others
 * into it).
 */
export function availableActions(
  r: Pick<Regression, "status" | "merged_into" | "scenario_name">,
): RegressionAction[] {
  if (r.merged_into) return [];
  const out: RegressionAction[] = (["confirm", "promote", "dismiss", "reopen"] as const).filter(
    (a) => FROM[a].includes(r.status) && (a !== "promote" || r.scenario_name === null),
  );
  if (r.scenario_name === null) out.push("merge");
  return out;
}

/** Label, severity, tags and assignee can be set on any group not merged away. */
export function canTriage(r: Pick<Regression, "merged_into">): boolean {
  return !r.merged_into;
}

/** Actions a person must give a reason for (the service refuses them without one). */
export function needsReason(action: RegressionAction): boolean {
  return action === "dismiss" || action === "reopen";
}

/** Who acted, as the history says it. */
export function regressionActor(actor: string | null | undefined, meUserId?: string): string {
  if (actor === "system:regression-miner") return "the regression miner";
  if (actor === "system:evaluation") return "an evaluation";
  return actorLabel(actor, meUserId);
}

/** The contract's `Tag` and `Actor` patterns. */
const TAG = /^[a-z0-9][a-z0-9_:.-]{0,62}$/;
const ACTOR = /^(user|apikey|service):[A-Za-z0-9._@:-]{1,200}$/;
export const MAX_TAGS = 20;

/**
 * Tags as a person typed them (comma or space separated), deduplicated in
 * order; `invalid` names those the service would refuse (lowercase letters,
 * digits and `_:.-`, at most 63 characters).
 */
export function parseTags(text: string): { tags: string[]; invalid: string[] } {
  const tags: string[] = [];
  const invalid: string[] = [];
  for (const raw of text.split(/[\s,]+/)) {
    const t = raw.trim();
    if (!t) continue;
    if (!TAG.test(t)) {
      if (!invalid.includes(t)) invalid.push(t);
    } else if (!tags.includes(t)) tags.push(t);
  }
  return { tags, invalid };
}

/** An actor the service accepts as an assignee (`user:…`), or an empty field (unassigned). */
export function assigneeProblem(text: string): string | null {
  const t = text.trim();
  if (!t) return null;
  return ACTOR.test(t) ? null : "An assignee is a principal such as user:alex.";
}

function pair(v: unknown): [unknown, unknown] | null {
  return Array.isArray(v) && v.length === 2 ? [v[0], v[1]] : null;
}

function shown(v: unknown): string {
  if (v === null || v === undefined || v === "") return "none";
  if (Array.isArray(v)) return v.length ? v.join(", ") : "none";
  return String(v);
}

/** What an event of the history did, in a sentence (without who and when). */
export function eventSummary(e: RegressionEvent): string {
  const d = e.detail ?? {};
  switch (e.action) {
    case "created":
      return "Found by the regression miner";
    case "confirm":
      return "Confirmed";
    case "dismiss":
      return "Dismissed";
    case "reopen":
      return "Reopened";
    case "promote": {
      const scenario = typeof d.scenario === "string" ? d.scenario : null;
      const dataset = typeof d.dataset === "string" ? d.dataset : null;
      return scenario
        ? `Promoted to the regression test ${scenario}${dataset ? ` in ${dataset}` : ""}`
        : "Promoted to a regression test";
    }
    case "fixed":
      return typeof d.agent_version === "string" ? `Fixed in ${d.agent_version}` : "Fixed";
    case "merge":
      return `Merged into ${typeof d.into === "string" ? shortId(d.into) : "another regression"}`;
    case "merged":
      return `Took in ${typeof d.from === "string" ? shortId(d.from) : "another regression"}${
        typeof d.occurrences === "number"
          ? ` (${d.occurrences} ${d.occurrences === 1 ? "failure" : "failures"})`
          : ""
      }`;
    case "triage":
    case "assign": {
      const parts = Object.entries(d)
        .map(([field, v]) => {
          const p = pair(v);
          return p ? `${field} ${shown(p[0])} → ${shown(p[1])}` : null;
        })
        .filter((x): x is string => x !== null);
      return parts.length ? `Changed ${parts.join("; ")}` : e.action === "assign" ? "Assigned" : "Triaged";
    }
    default:
      return humanize(e.action);
  }
}
