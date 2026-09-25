import { Badge } from "@/components/ui/badge";
import type { RegressionStatus } from "@/lib/api/evaluation";
import { statusBadge, statusMeaning } from "@/lib/regressions";

export function RegressionStatusBadge({
  status,
  className,
}: {
  status: RegressionStatus;
  className?: string;
}) {
  const b = statusBadge(status);
  return (
    <Badge tone={b.tone} title={statusMeaning(status)} data-status={status} className={className}>
      {b.label}
    </Badge>
  );
}
