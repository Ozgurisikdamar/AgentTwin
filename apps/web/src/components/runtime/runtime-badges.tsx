import { Badge } from "@/components/ui/badge";
import type { ApprovalStatus, Attempt, Effect, Outcome, Risk } from "@/lib/api/runtime";
import {
  approvalStatusLabel,
  approvalStatusMeaning,
  approvalStatusTone,
  attemptLabel,
  attemptMeaning,
  attemptTone,
  effectLabel,
  effectTone,
  outcomeLabel,
  outcomeMeaning,
  outcomeTone,
  riskTone,
} from "@/lib/runtime";
import { humanize } from "@/lib/format";

export function EffectBadge({ effect }: { effect: Effect }) {
  return (
    <Badge tone={effectTone(effect)} data-effect={effect}>
      {effectLabel(effect)}
    </Badge>
  );
}

export function OutcomeBadge({ outcome }: { outcome: Outcome }) {
  return (
    <Badge tone={outcomeTone(outcome)} title={outcomeMeaning(outcome)} data-outcome={outcome}>
      {outcomeLabel(outcome)}
    </Badge>
  );
}

export function ApprovalStatusBadge({ status }: { status: ApprovalStatus }) {
  return (
    <Badge tone={approvalStatusTone(status)} title={approvalStatusMeaning(status)} data-status={status}>
      {approvalStatusLabel(status)}
    </Badge>
  );
}

export function RiskBadge({ risk }: { risk: Risk }) {
  return <Badge tone={riskTone(risk)}>{humanize(risk)}</Badge>;
}

export function AttemptBadge({ result }: { result: Attempt["result"] }) {
  return (
    <Badge tone={attemptTone(result)} title={attemptMeaning(result)}>
      {attemptLabel(result)}
    </Badge>
  );
}
