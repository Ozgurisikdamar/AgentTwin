/**
 * Pure helpers of the runtime containment pages (ADR-0033): what a gateway
 * decision, an approval request and a policy say, in words; whether a person
 * can still decide a request; the exact action as rows; how an attempted
 * action differs from the approved one. The gateway decides everything:
 * these only decide what the pages show and offer.
 */
import type { BadgeTone } from "@/components/ui/badge";
import type { Approval, ApprovalStatus, Attempt, Change, Effect, Outcome, Risk } from "./api/runtime";

export const EFFECTS: readonly Effect[] = ["allow", "allow_with_limits", "require_approval", "deny"];

const EFFECT: Record<Effect, { label: string; tone: BadgeTone }> = {
  allow: { label: "Allow", tone: "success" },
  allow_with_limits: { label: "Allow with limits", tone: "info" },
  require_approval: { label: "Needs approval", tone: "warning" },
  deny: { label: "Deny", tone: "danger" },
};

export function effectLabel(effect: Effect): string {
  return EFFECT[effect].label;
}

export function effectTone(effect: Effect): BadgeTone {
  return EFFECT[effect].tone;
}

export const OUTCOMES: readonly Outcome[] = [
  "executed",
  "forwarded",
  "failed",
  "replayed",
  "denied",
  "approval_required",
  "approval_refused",
];

const OUTCOME: Record<Outcome, { label: string; tone: BadgeTone; says: string }> = {
  forwarded: { label: "Forwarded", tone: "info", says: "Sent to the tool; no answer yet." },
  executed: { label: "Executed", tone: "success", says: "The tool ran it and answered." },
  failed: { label: "Failed", tone: "danger", says: "The tool failed, timed out or answered 5xx." },
  replayed: {
    label: "Replayed",
    tone: "neutral",
    says: "The same idempotency key: the stored answer was returned; the tool did not run again.",
  },
  denied: { label: "Denied", tone: "danger", says: "A policy refused it; the tool was not called." },
  approval_required: {
    label: "Held for approval",
    tone: "warning",
    says: "A person must approve this exact action before it runs.",
  },
  approval_refused: {
    label: "Approval refused",
    tone: "danger",
    says: "The approval token did not let it run (another action, used, expired or not approved).",
  },
};

export function outcomeLabel(outcome: Outcome): string {
  return OUTCOME[outcome].label;
}

export function outcomeTone(outcome: Outcome): BadgeTone {
  return OUTCOME[outcome].tone;
}

export function outcomeMeaning(outcome: Outcome): string {
  return OUTCOME[outcome].says;
}

export const APPROVAL_STATUSES: readonly ApprovalStatus[] = [
  "PENDING",
  "APPROVED",
  "DENIED",
  "EXPIRED",
  "USED",
];

const STATUS: Record<ApprovalStatus, { label: string; tone: BadgeTone; says: string }> = {
  PENDING: {
    label: "Pending",
    tone: "warning",
    says: "Waiting for a person. Approving lets this exact action run once.",
  },
  APPROVED: {
    label: "Approved",
    tone: "info",
    says: "Approved; the agent has not run the action yet. It can run it once, unchanged.",
  },
  DENIED: { label: "Denied", tone: "danger", says: "A person declined it; the action will not run." },
  EXPIRED: {
    label: "Expired",
    tone: "neutral",
    says: "Nobody decided in time (or the approval was not used in time); the action will not run.",
  },
  USED: {
    label: "Used",
    tone: "success",
    says: "Approved and run, once. The approval cannot be used again.",
  },
};

export function approvalStatusLabel(status: ApprovalStatus): string {
  return STATUS[status].label;
}

export function approvalStatusTone(status: ApprovalStatus): BadgeTone {
  return STATUS[status].tone;
}

export function approvalStatusMeaning(status: ApprovalStatus): string {
  return STATUS[status].says;
}

const RISK: Record<Risk, BadgeTone> = {
  READ: "neutral",
  WRITE_REVERSIBLE: "info",
  WRITE_IRREVERSIBLE: "danger",
  EXECUTE: "warning",
  ADMIN: "danger",
};

export function riskTone(risk: Risk): BadgeTone {
  return RISK[risk];
}

/**
 * When the request expires, in words ("in 14 min", "expired 3 min ago"), and
 * whether it has. The service closes an expired request as EXPIRED when it is
 * next read or decided; until then its status may still say PENDING.
 */
export function expiry(expiresAt: string, now: Date = new Date()): { expired: boolean; text: string } {
  const at = new Date(expiresAt).getTime();
  if (Number.isNaN(at)) return { expired: false, text: "—" };
  const seconds = Math.round((at - now.getTime()) / 1000);
  const span = spanOf(Math.abs(seconds));
  return seconds > 0
    ? { expired: false, text: `in ${span}` }
    : { expired: true, text: `expired ${span} ago` };
}

function spanOf(s: number): string {
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.round(s / 60)} min`;
  if (s < 86400) {
    const h = Math.floor(s / 3600);
    const m = Math.round((s % 3600) / 60);
    return m ? `${h} h ${m} min` : `${h} h`;
  }
  return `${Math.round(s / 86400)} d`;
}

/** Whether a person can still approve or deny the request. */
export function canDecide(
  approval: Pick<Approval, "status" | "expires_at">,
  now: Date = new Date(),
): boolean {
  return approval.status === "PENDING" && !expiry(approval.expires_at, now).expired;
}

/** A JSON value in one line, as the action and its changes show it. */
export function valueText(value: unknown): string {
  if (value === undefined) return "—";
  if (typeof value === "string") return JSON.stringify(value);
  return JSON.stringify(value) ?? String(value);
}

/**
 * The action's arguments as rows (`amount`, `items[1].sku`), in a stable
 * order: what a person approves is exactly these values.
 */
export function argumentRows(args: Record<string, unknown>): { path: string; value: string }[] {
  const rows: { path: string; value: string }[] = [];
  const walk = (value: unknown, path: string) => {
    if (Array.isArray(value)) {
      if (value.length === 0) rows.push({ path, value: "[]" });
      value.forEach((v, i) => walk(v, `${path}[${i}]`));
      return;
    }
    if (value !== null && typeof value === "object") {
      const keys = Object.keys(value).sort();
      if (keys.length === 0 && path) rows.push({ path, value: "{}" });
      for (const k of keys) walk((value as Record<string, unknown>)[k], path ? `${path}.${k}` : k);
      return;
    }
    rows.push({ path, value: valueText(value) });
  };
  walk(args, "");
  return rows;
}

/** One difference between the approved and the attempted action, in words. */
export function changeText(change: Change): string {
  if (change.added) return `${change.path} added: ${valueText(change.after)}`;
  if (change.removed) return `${change.path} removed (was ${valueText(change.before)})`;
  return `${change.path}: ${valueText(change.before)} → ${valueText(change.after)}`;
}

const ATTEMPT: Record<Attempt["result"], { label: string; tone: BadgeTone; says: string }> = {
  executed: { label: "Executed", tone: "success", says: "The approved action ran." },
  mismatch: {
    label: "Different action",
    tone: "danger",
    says: "The token was presented for another action; it did not run.",
  },
  used: { label: "Already used", tone: "danger", says: "The approval had been used; it did not run again." },
  expired: { label: "Expired", tone: "neutral", says: "The approval had expired; it did not run." },
  not_approved: { label: "Not approved", tone: "danger", says: "Nobody had approved it; it did not run." },
};

export function attemptLabel(result: Attempt["result"]): string {
  return ATTEMPT[result].label;
}

export function attemptTone(result: Attempt["result"]): BadgeTone {
  return ATTEMPT[result].tone;
}

export function attemptMeaning(result: Attempt["result"]): string {
  return ATTEMPT[result].says;
}

/** A rule of a policy version, as its document states it. */
export interface PolicyRule {
  name: string;
  when: string;
  effect: Effect;
  message: string;
}

/** A test a policy version carries. */
export interface PolicyTest {
  name: string;
  expect: Effect;
  rule?: string;
  args: Record<string, unknown>;
  context: Record<string, unknown>;
}

/** What a policy version decides, read from its document (defaults filled). */
export interface PolicyShape {
  tool: string;
  description: string;
  defaultEffect: Effect;
  failMode: string;
  approvalExpiresInSeconds: number | null;
  rules: PolicyRule[];
  tests: PolicyTest[];
}

function isEffect(v: unknown): v is Effect {
  return typeof v === "string" && (EFFECTS as readonly string[]).includes(v);
}

function record(v: unknown): Record<string, unknown> {
  return v !== null && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : {};
}

function str(v: unknown): string {
  return typeof v === "string" ? v : "";
}

/** The policy's rules, tests and settings from a version's `spec` (the document as read). */
export function policyShape(spec: Record<string, unknown>): PolicyShape {
  const body = record(spec.spec);
  const approval = record(body.approval);
  const expires = approval.expiresInSeconds;
  const rules = Array.isArray(body.rules) ? body.rules : [];
  const tests = Array.isArray(body.tests) ? body.tests : [];
  return {
    tool: str(body.tool),
    description: str(record(spec.metadata).description),
    defaultEffect: isEffect(body.default) ? body.default : "allow",
    failMode: str(body.failMode) || "fail_closed",
    approvalExpiresInSeconds: typeof expires === "number" ? expires : null,
    rules: rules.map(record).map((r) => ({
      name: str(r.name),
      when: str(r.when),
      effect: isEffect(r.effect) ? r.effect : "deny",
      message: str(r.message),
    })),
    tests: tests.map(record).map((t) => ({
      name: str(t.name),
      expect: isEffect(t.expect) ? t.expect : "deny",
      rule: str(t.rule) || undefined,
      args: record(t.args),
      context: record(t.context),
    })),
  };
}

const FAIL_MODE: Record<string, string> = {
  fail_closed: "A rule that cannot be evaluated denies the call.",
  fail_open: "A rule that cannot be evaluated lets the call through (read-only tools only).",
  require_approval: "A rule that cannot be evaluated asks a person.",
};

export function failModeMeaning(mode: string): string {
  return FAIL_MODE[mode] ?? mode;
}

/** A threshold as a sentence: `args.amount > 100`. */
export function thresholdText(t: { path: string; op: string; value: number }): string {
  return `${t.path} ${t.op} ${t.value}`;
}
