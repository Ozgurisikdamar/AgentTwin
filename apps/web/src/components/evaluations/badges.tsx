import {
  AlertTriangle,
  ArrowDownRight,
  ArrowUpRight,
  CheckCircle2,
  CircleSlash,
  Equal,
  OctagonAlert,
  UserCheck,
} from "lucide-react";
import type { ReactNode } from "react";
import { CaseStatusBadge } from "@/components/simulations/badges";
import { Badge, type BadgeTone } from "@/components/ui/badge";
import type { Classification, ExpectationChange, MetricDelta, SideStatus } from "@/lib/api/evaluation";
import { CLASSIFICATION_LABEL } from "@/lib/evaluations";

const icon = "h-3 w-3";

const CLASS: Record<Classification, { tone: BadgeTone; icon: ReactNode }> = {
  NEW_CRITICAL_FAILURE: { tone: "danger", icon: <OctagonAlert className={icon} aria-hidden="true" /> },
  REGRESSED: { tone: "danger", icon: <ArrowDownRight className={icon} aria-hidden="true" /> },
  INCOMPLETE: { tone: "warning", icon: <AlertTriangle className={icon} aria-hidden="true" /> },
  IMPROVED: { tone: "success", icon: <ArrowUpRight className={icon} aria-hidden="true" /> },
  UNCHANGED: { tone: "neutral", icon: <Equal className={icon} aria-hidden="true" /> },
};

export function ClassificationBadge({ classification }: { classification: Classification }) {
  const c = CLASS[classification] ?? CLASS.INCOMPLETE;
  return (
    <Badge tone={c.tone} data-classification={classification}>
      {c.icon}
      {CLASSIFICATION_LABEL[classification] ?? classification}
    </Badge>
  );
}

/** One side's case status; `MISSING` is a case that side never ran. */
export function SideStatusBadge({ status }: { status: SideStatus }) {
  if (status === "MISSING") {
    return (
      <Badge tone="warning" data-status={status}>
        <CircleSlash className={icon} aria-hidden="true" />
        Not run
      </Badge>
    );
  }
  return <CaseStatusBadge status={status} />;
}

const CHANGE: Record<ExpectationChange["change"], { tone: BadgeTone; label: string }> = {
  broken: { tone: "danger", label: "newly fails" },
  fixed: { tone: "success", label: "fixed" },
  still_failing: { tone: "warning", label: "still fails" },
  same: { tone: "neutral", label: "same" },
  not_comparable: { tone: "warning", label: "not comparable" },
};

export function ExpectationChangeBadge({ change }: { change: ExpectationChange["change"] }) {
  const c = CHANGE[change] ?? CHANGE.not_comparable;
  return (
    <Badge tone={c.tone} data-change={change}>
      {c.label}
    </Badge>
  );
}

const METRIC: Record<MetricDelta["change"], string> = {
  better: "text-emerald-700",
  worse: "text-rose-700",
  changed: "text-sky-800",
  same: "text-slate-500",
  unknown: "text-slate-400",
};

/** The colour of a metric difference: better, worse, merely different, or unknown. */
export function metricTone(change: MetricDelta["change"]): string {
  return METRIC[change] ?? METRIC.unknown;
}

export function ReviewedBadge({ title }: { title?: string }) {
  return (
    <Badge tone="info" title={title}>
      <UserCheck className={icon} aria-hidden="true" />
      reviewed
    </Badge>
  );
}

export function NeedsReviewBadge() {
  return (
    <Badge tone="warning">
      <CircleSlash className={icon} aria-hidden="true" />
      needs review
    </Badge>
  );
}

export function CalibratedBadge({ calibrated }: { calibrated: boolean }) {
  return calibrated ? (
    <Badge tone="success">
      <CheckCircle2 className={icon} aria-hidden="true" />
      calibrated
    </Badge>
  ) : (
    <Badge
      tone="warning"
      title="The judge has not passed a calibration against human labels for this criterion."
    >
      <AlertTriangle className={icon} aria-hidden="true" />
      not calibrated
    </Badge>
  );
}
