import {
  AlertTriangle,
  BookOpen,
  Bot,
  CheckCircle2,
  Circle,
  CircleHelp,
  Flag,
  Globe,
  MessageSquareQuote,
  Plug,
  Shield,
  ShieldCheck,
  Sparkles,
  Wrench,
  XCircle,
} from "lucide-react";
import { Badge, type BadgeTone } from "@/components/ui/badge";
import { humanize } from "@/lib/format";
import type { OutcomeStatus, SpanKind, TraceStatus } from "@/lib/types";

export function StatusBadge({ status }: { status: TraceStatus | string }) {
  if (status === "ERROR")
    return (
      <Badge tone="danger">
        <XCircle className="h-3 w-3" aria-hidden="true" />
        Error
      </Badge>
    );
  if (status === "OK")
    return (
      <Badge tone="success">
        <CheckCircle2 className="h-3 w-3" aria-hidden="true" />
        OK
      </Badge>
    );
  return (
    <Badge tone="neutral">
      <Circle className="h-3 w-3" aria-hidden="true" />
      Unset
    </Badge>
  );
}

const OUTCOME_TONE: Record<OutcomeStatus, BadgeTone> = {
  SUCCESS: "success",
  PARTIAL: "warning",
  FAILURE: "danger",
  UNKNOWN: "neutral",
};

/**
 * Outcome with its evidence level. A verified outcome is backed by evidence
 * (state assertion, callback, human review); a claimed one is only the
 * agent's self-report (spec §15) and is labeled as such.
 */
export function OutcomeBadge({
  status,
  verified,
}: {
  status: OutcomeStatus | null | undefined;
  verified: boolean | null | undefined;
}) {
  if (!status) return <span className="text-xs text-slate-400">No outcome</span>;
  const tone = OUTCOME_TONE[status] ?? "neutral";
  return (
    <span className="inline-flex items-center gap-1">
      <Badge tone={tone}>{humanize(status)}</Badge>
      {verified ? (
        <span
          className="inline-flex items-center gap-0.5 text-xs text-emerald-800"
          title="Verified by evidence"
        >
          <ShieldCheck className="h-3.5 w-3.5" aria-hidden="true" />
          verified
        </span>
      ) : (
        <span
          className="inline-flex items-center gap-0.5 text-xs text-slate-500"
          title="Self-reported by the agent"
        >
          <MessageSquareQuote className="h-3.5 w-3.5" aria-hidden="true" />
          claimed
        </span>
      )}
    </span>
  );
}

const RISK_TONE: Record<string, BadgeTone> = {
  READ: "neutral",
  WRITE_REVERSIBLE: "warning",
  WRITE_IRREVERSIBLE: "danger",
  EXECUTE: "danger",
  ADMIN: "danger",
};

export function RiskBadge({ risk }: { risk: string | null | undefined }) {
  if (!risk) return null;
  return <Badge tone={RISK_TONE[risk] ?? "neutral"}>{risk.replaceAll("_", " ")}</Badge>;
}

/** Short risk labels for dense rows; the full tier stays in the title and the details panel. */
export const RISK_SHORT: Record<string, string> = {
  READ: "read",
  WRITE_REVERSIBLE: "write",
  WRITE_IRREVERSIBLE: "irreversible",
  EXECUTE: "execute",
  ADMIN: "admin",
};

const RISK_MARKER_CLASS: Record<string, string> = {
  WRITE_REVERSIBLE: "bg-amber-100 text-amber-900",
  WRITE_IRREVERSIBLE: "bg-rose-100 text-rose-900",
  EXECUTE: "bg-rose-100 text-rose-900",
  ADMIN: "bg-rose-100 text-rose-900",
};

/** Compact risk tag for waterfall rows. Returns nothing for read-only tools. */
export function RiskMarker({ risk }: { risk: string | null | undefined }) {
  if (!risk || risk === "READ") return null;
  return (
    <span
      title={`Risk: ${risk.replaceAll("_", " ").toLowerCase()}`}
      className={`shrink-0 rounded px-1 text-[10px] font-medium ${RISK_MARKER_CLASS[risk] ?? "bg-slate-100 text-slate-700"}`}
    >
      {RISK_SHORT[risk] ?? risk.toLowerCase()}
    </span>
  );
}

const SEVERE_SIGNALS = new Set([
  "contradiction",
  "duplicate_side_effect",
  "timeout_after_mutation",
  "policy_violation",
  "policy_denied",
  "outcome_failure",
  "cross_tenant_access",
  "secret_exposure",
]);

export function signalTone(signal: string): BadgeTone {
  if (SEVERE_SIGNALS.has(signal)) return "danger";
  if (signal === "retry" || signal.endsWith("_error") || signal === "error" || signal === "incomplete")
    return "warning";
  return "info";
}

export function SignalBadge({ signal }: { signal: string }) {
  const severe = signalTone(signal) === "danger";
  return (
    <Badge tone={signalTone(signal)}>
      {severe ? <AlertTriangle className="h-3 w-3" aria-hidden="true" /> : null}
      {signal.replaceAll("_", " ")}
    </Badge>
  );
}

const KIND_ICON: Record<SpanKind, typeof Bot> = {
  agent: Bot,
  model: Sparkles,
  tool: Wrench,
  retrieval: BookOpen,
  policy: Shield,
  outcome: Flag,
  http: Globe,
  mcp: Plug,
  other: CircleHelp,
};

export const KIND_LABEL: Record<SpanKind, string> = {
  agent: "Agent run",
  model: "Model call",
  tool: "Tool call",
  retrieval: "Retrieval",
  policy: "Policy decision",
  outcome: "Outcome",
  http: "HTTP call",
  mcp: "MCP call",
  other: "Span",
};

export function KindIcon({ kind, className }: { kind: SpanKind; className?: string }) {
  const Icon = KIND_ICON[kind] ?? CircleHelp;
  return <Icon className={className ?? "h-3.5 w-3.5"} aria-label={KIND_LABEL[kind] ?? "Span"} role="img" />;
}
