/**
 * Pure helpers of the release pages: what a gate says, in words, and the
 * override form's rules. Every function is total over what the contract
 * allows. The gate decides; nothing here re-decides it (spec §28): the UI
 * shows the stored decision, never an outcome of its own.
 */
import type { BadgeTone } from "@/components/ui/badge";
import type {
  AuditEntry,
  BodyOf,
  GateCounts,
  GateDecision,
  GateRatio,
  GateRiskIndex,
  GateSummary,
  Release,
  ReleaseGate,
} from "./api/control-plane";
import { formatCost, humanize } from "./format";

type Outcome = "PENDING" | "PASS" | "WARN" | "BLOCK" | "OVERRIDDEN";

const OUTCOME_TONE: Record<Outcome, BadgeTone> = {
  PENDING: "neutral",
  PASS: "success",
  WARN: "warning",
  BLOCK: "danger",
  OVERRIDDEN: "brand",
};

/** The badge tone of an outcome (unknown ones are neutral). */
export function outcomeTone(outcome: string | null | undefined): BadgeTone {
  return (outcome && OUTCOME_TONE[outcome as Outcome]) || "neutral";
}

/** What a gate is, as lists show it. */
export interface GateBadge {
  label: string;
  tone: BadgeTone;
  /** Said next to the label: the decision an override did not change. */
  note: string | null;
}

/**
 * A gate as a badge. An override never turns a decision into a PASS
 * (spec §92): an overridden gate reads "Overridden" with its original
 * outcome, and one whose override expired reads its outcome again.
 */
export function gateBadge(
  gate: Pick<GateSummary, "effective_outcome" | "outcome" | "overridden" | "incomplete"> | null | undefined,
): GateBadge {
  if (!gate) return { label: "Not evaluated", tone: "neutral", note: null };
  switch (gate.effective_outcome) {
    case "PENDING":
      return { label: "Evaluating", tone: "neutral", note: null };
    case "OVERRIDDEN":
      return {
        label: "Overridden",
        tone: "brand",
        note: gate.outcome ? `originally ${gate.outcome}` : null,
      };
    default: {
      const notes = [];
      if (gate.incomplete) notes.push("evidence missing");
      if (gate.overridden) notes.push("override expired");
      return {
        label: gate.effective_outcome,
        tone: outcomeTone(gate.effective_outcome),
        note: notes.length ? notes.join(", ") : null,
      };
    }
  }
}

/** "support-refund-agent 1.2.4 → 1.3.0". */
export function releaseName(r: Pick<Release, "agent" | "baseline" | "candidate">): string {
  return `${r.agent.name} ${r.baseline.version} → ${r.candidate.version}`;
}

/** A signed cost: "+$0.04", "−$1.20", "$0" (the dash when unknown). */
export function formatCostDelta(usd: number | null | undefined): string {
  if (usd == null || !Number.isFinite(usd)) return "—";
  if (usd === 0) return "$0";
  return `${usd > 0 ? "+" : "−"}${formatCost(Math.abs(usd))}`;
}

/** A signed latency: "+120 ms", "−16 ms", "0 ms" (the dash when unknown). */
export function formatLatencyDelta(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms)) return "—";
  const r = Math.round(ms);
  if (r === 0) return "0 ms";
  const abs = Math.abs(r);
  const text = abs >= 1000 ? `${(abs / 1000).toFixed(abs >= 10_000 ? 1 : 2)} s` : `${abs} ms`;
  return `${r > 0 ? "+" : "−"}${text}`;
}

/** A delta's tone: an increase of cost or latency is worse. */
export function deltaTone(delta: number | null | undefined): "worse" | "better" | "same" {
  if (delta == null || !Number.isFinite(delta) || delta === 0) return "same";
  return delta > 0 ? "worse" : "better";
}

/** One rule that fired, as "why" shows it. */
export interface WhyLine {
  rule: string;
  outcome: "WARN" | "BLOCK";
  title: string;
  statement: string;
  /** The scenarios of its evidence, in order, each once. */
  scenarios: string[];
  /** Evidence the baseline fails the same way. */
  preExisting: number;
  evidence: number;
}

/** Why the gate decided: every rule that fired, most severe first (as stored). */
export function whyLines(decision: GateDecision | null | undefined): WhyLine[] {
  return (decision?.rules ?? []).map((r) => {
    const scenarios: string[] = [];
    for (const ev of r.evidence) {
      if (ev.scenario && !scenarios.includes(ev.scenario)) scenarios.push(ev.scenario);
    }
    return {
      rule: r.rule,
      outcome: r.outcome,
      title: r.title,
      statement: r.statement,
      scenarios,
      preExisting: r.evidence.filter((e) => e.pre_existing).length,
      evidence: r.evidence.length,
    };
  });
}

function count(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`;
}

/**
 * The decision's counts in words, worst first ("2 new critical failures",
 * "3 of 9 required scenarios failed"); nothing to say reads as none.
 */
export function countLines(c: GateCounts): string[] {
  const out: string[] = [];
  if (c.new_critical_failures) {
    out.push(count(c.new_critical_failures, "new critical failure", "new critical failures"));
  }
  if (c.incomplete) out.push(`${count(c.incomplete, "scenario", "scenarios")} without evidence`);
  if (c.regressed) out.push(count(c.regressed, "regressed scenario", "regressed scenarios"));
  if (c.failed) {
    out.push(`${c.failed} of ${c.required} required ${c.required === 1 ? "scenario" : "scenarios"} failed`);
  }
  if (c.improved) out.push(count(c.improved, "improved scenario", "improved scenarios"));
  if (c.known_regressions) {
    out.push(`${count(c.known_regressions, "known regression", "known regressions")} replayed`);
  }
  return out;
}

/** The coverage the decision measured (ratios with nothing to cover are left out). */
export function coverageRows(coverage: readonly GateRatio[] | null | undefined): (GateRatio & {
  complete: boolean;
})[] {
  return (coverage ?? []).filter((r) => r.total > 0).map((r) => ({ ...r, complete: r.covered >= r.total }));
}

/** The factors that added to the risk index, largest first. */
export function riskContributions(risk: GateRiskIndex | null | undefined): GateRiskIndex["factors"] {
  return [...(risk?.factors ?? [])]
    .filter((f) => f.contribution > 0)
    .sort((a, b) => b.contribution - a.contribution || a.name.localeCompare(b.name));
}

/** The comparison of one scenario in an eval run. */
export function evalCaseHref(evalRunId: string, scenario: string): string {
  return `/evaluations/${evalRunId}/cases/${encodeURIComponent(scenario)}`;
}

/** A trace of the project. */
export function traceHref(traceId: string, projectId: string): string {
  return `/traces/${traceId}?project_id=${encodeURIComponent(projectId)}`;
}

/** Whether the latest revision's decision may be overridden, and if not why. */
export function overrideState(
  gate: Pick<ReleaseGate, "revision" | "decision" | "override">,
  latestRevision: number,
): { possible: boolean; reason: string | null } {
  if (gate.revision !== latestRevision) {
    return { possible: false, reason: "Only the latest revision can be overridden." };
  }
  // Only a decided evaluation has a decision.
  if (!gate.decision) {
    return { possible: false, reason: "The evaluation has not decided yet." };
  }
  if (gate.override) {
    return {
      possible: false,
      reason: "This decision was overridden before; a new evaluation is gated anew.",
    };
  }
  if (gate.decision.outcome === "PASS") return { possible: false, reason: "The gate passed." };
  return { possible: true, reason: null };
}

/** What the override form holds (`expiresAt` is a local `datetime-local` value). */
export interface OverrideDraft {
  reason: string;
  ticketUrl: string;
  expiresAt: string;
}

export const OVERRIDE_REASON_MIN = 10;
export const OVERRIDE_REASON_MAX = 2000;
export const OVERRIDE_MAX_DAYS = 90;
const URL_MAX = 2048;

// Control characters (Go's unicode.IsControl), which the API refuses.
const CONTROL = /[\u0000-\u001f\u007f-\u009f]/;
const CONTROL_BUT_LINES = /[\u0000-\u0008\u000b-\u001f\u007f-\u009f]/;

function httpURL(raw: string): boolean {
  if (raw.length > URL_MAX || CONTROL.test(raw)) return false;
  try {
    const u = new URL(raw);
    return (u.protocol === "http:" || u.protocol === "https:") && u.host !== "";
  } catch {
    return false;
  }
}

/** The local date-time of a `datetime-local` value, or null. */
function localDate(value: string): Date | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/.exec(value);
  if (!m) return null;
  const [y, mo, d, h, mi] = m.slice(1, 6).map(Number) as [number, number, number, number, number];
  const date = new Date(y, mo - 1, d, h, mi, m[6] ? Number(m[6]) : 0);
  return Number.isNaN(date.getTime()) || date.getMonth() !== mo - 1 ? null : date;
}

/** The form's problems by field, as the API would refuse them (none: the form is valid). */
export function overrideProblems(d: OverrideDraft, now: Date): Partial<Record<keyof OverrideDraft, string>> {
  const out: Partial<Record<keyof OverrideDraft, string>> = {};
  const reason = d.reason.trim();
  const n = [...reason].length;
  if (n < OVERRIDE_REASON_MIN) {
    out.reason = `Say why the release may go out, in at least ${OVERRIDE_REASON_MIN} characters.`;
  } else if (n > OVERRIDE_REASON_MAX) {
    out.reason = `At most ${OVERRIDE_REASON_MAX} characters.`;
  } else if (CONTROL_BUT_LINES.test(reason)) {
    out.reason = "Remove the control characters.";
  }
  const ticket = d.ticketUrl.trim();
  if (ticket && !httpURL(ticket)) out.ticketUrl = "An http(s) URL of at most 2048 characters.";
  if (d.expiresAt) {
    const at = localDate(d.expiresAt);
    if (!at) out.expiresAt = "Not a date and time.";
    else if (at.getTime() <= now.getTime()) out.expiresAt = "Must be in the future.";
    else if (at.getTime() > now.getTime() + OVERRIDE_MAX_DAYS * 86_400_000) {
      out.expiresAt = `At most ${OVERRIDE_MAX_DAYS} days away.`;
    }
  }
  return out;
}

/** The request of a valid form, for the revision it overrides. */
export function overrideBody(d: OverrideDraft, revision: number): BodyOf<"overrideReleaseGate"> {
  const body: BodyOf<"overrideReleaseGate"> = { reason: d.reason.trim(), revision };
  const ticket = d.ticketUrl.trim();
  if (ticket) body.ticket_url = ticket;
  const at = d.expiresAt ? localDate(d.expiresAt) : null;
  if (at) body.expires_at = at.toISOString();
  return body;
}

/** A `datetime-local` value for a date (local time, to the minute). */
export function toLocalInput(date: Date): string {
  const p = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${p(date.getMonth() + 1)}-${p(date.getDate())}T${p(date.getHours())}:${p(
    date.getMinutes(),
  )}`;
}

const AUDIT_ACTION: Record<string, string> = {
  "release.created": "Release created",
  "release.evaluate": "Evaluation requested",
  "release.gate_decided": "Gate decided",
  "release.override": "Gate overridden",
};

/** An audit action in words. */
export function auditActionLabel(action: string): string {
  return AUDIT_ACTION[action] ?? humanize(action);
}

/** An audit entry's metadata as short facts (`revision 1`, `outcome BLOCK`). */
export function auditFacts(entry: Pick<AuditEntry, "metadata">): string[] {
  const meta = entry.metadata;
  if (!meta || typeof meta !== "object" || Array.isArray(meta)) return [];
  return Object.entries(meta as Record<string, unknown>)
    .filter(([, v]) => v !== null && v !== undefined && v !== "" && typeof v !== "object")
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([k, v]) => `${humanize(k).toLowerCase()} ${String(v)}`);
}

/** The file name of a gate's downloaded evidence. */
export function evidenceFileName(release: Pick<Release, "agent" | "candidate">, revision: number): string {
  const safe = (s: string) => s.replace(/[^A-Za-z0-9._-]+/g, "_");
  return `agenttwin-gate-${safe(release.agent.name)}-${safe(release.candidate.version)}-r${revision}.json`;
}
