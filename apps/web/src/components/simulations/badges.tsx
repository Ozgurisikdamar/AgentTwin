import {
  AlertTriangle,
  Ban,
  CheckCircle2,
  CircleDashed,
  Clock,
  Loader2,
  MinusCircle,
  XCircle,
} from "lucide-react";
import type { ReactNode } from "react";
import { Badge, type BadgeTone } from "@/components/ui/badge";
import type { CaseStatus, ResultStatus, RunStatus, Severity } from "@/lib/types";

const spin = "h-3 w-3 animate-spin motion-reduce:animate-none";
const icon = "h-3 w-3";

const RUN: Record<RunStatus, { tone: BadgeTone; label: string; icon: ReactNode }> = {
  QUEUED: { tone: "neutral", label: "Queued", icon: <Clock className={icon} aria-hidden="true" /> },
  PREPARING: { tone: "info", label: "Preparing", icon: <Loader2 className={spin} aria-hidden="true" /> },
  RUNNING: { tone: "info", label: "Running", icon: <Loader2 className={spin} aria-hidden="true" /> },
  EVALUATING: { tone: "info", label: "Evaluating", icon: <Loader2 className={spin} aria-hidden="true" /> },
  // A completed run can hold failed cases: the verdict is shown next to it,
  // so the lifecycle badge stays neutral.
  COMPLETED: {
    tone: "neutral",
    label: "Completed",
    icon: <CheckCircle2 className={icon} aria-hidden="true" />,
  },
  FAILED: { tone: "danger", label: "Run failed", icon: <XCircle className={icon} aria-hidden="true" /> },
  CANCELLED: { tone: "neutral", label: "Cancelled", icon: <Ban className={icon} aria-hidden="true" /> },
};

export function RunStatusBadge({ status }: { status: RunStatus }) {
  const s = RUN[status] ?? RUN.QUEUED;
  return (
    <Badge tone={s.tone} data-status={status}>
      {s.icon}
      {s.label}
    </Badge>
  );
}

const CASE: Record<CaseStatus, { tone: BadgeTone; label: string; icon: ReactNode }> = {
  PENDING: { tone: "neutral", label: "Pending", icon: <CircleDashed className={icon} aria-hidden="true" /> },
  RUNNING: { tone: "info", label: "Running", icon: <Loader2 className={spin} aria-hidden="true" /> },
  PASSED: { tone: "success", label: "Passed", icon: <CheckCircle2 className={icon} aria-hidden="true" /> },
  FAILED: { tone: "danger", label: "Failed", icon: <XCircle className={icon} aria-hidden="true" /> },
  ERRORED: { tone: "warning", label: "Errored", icon: <AlertTriangle className={icon} aria-hidden="true" /> },
  CANCELLED: { tone: "neutral", label: "Cancelled", icon: <Ban className={icon} aria-hidden="true" /> },
};

export function CaseStatusBadge({ status }: { status: CaseStatus }) {
  const s = CASE[status] ?? CASE.PENDING;
  return (
    <Badge tone={s.tone} data-status={status}>
      {s.icon}
      {s.label}
    </Badge>
  );
}

const SEVERITY: Record<Severity, BadgeTone> = {
  critical: "danger",
  high: "warning",
  medium: "info",
  low: "neutral",
};

export function SeverityBadge({ severity }: { severity: Severity | string }) {
  return <Badge tone={SEVERITY[severity as Severity] ?? "neutral"}>{severity}</Badge>;
}

const RESULT: Record<ResultStatus, { tone: BadgeTone; label: string; icon: ReactNode }> = {
  PASS: { tone: "success", label: "Pass", icon: <CheckCircle2 className={icon} aria-hidden="true" /> },
  FAIL: { tone: "danger", label: "Fail", icon: <XCircle className={icon} aria-hidden="true" /> },
  SKIPPED: { tone: "neutral", label: "Skipped", icon: <MinusCircle className={icon} aria-hidden="true" /> },
  ERROR: { tone: "warning", label: "Error", icon: <AlertTriangle className={icon} aria-hidden="true" /> },
};

export function ResultBadge({ status }: { status: ResultStatus }) {
  const s = RESULT[status] ?? RESULT.ERROR;
  return (
    <Badge tone={s.tone}>
      {s.icon}
      {s.label}
    </Badge>
  );
}

/** A failure taxonomy label (e.g. HALLUCINATED_SUCCESS). */
export function FailureLabel({ label }: { label: string }) {
  return (
    <Badge tone="danger" title={label}>
      {label.replaceAll("_", " ").toLowerCase()}
    </Badge>
  );
}
