import { Badge } from "@/components/ui/badge";
import type { GateSummary } from "@/lib/api/control-plane";
import { deltaTone, gateBadge } from "@/lib/releases";
import { cn } from "@/lib/utils";

/**
 * A gate's outcome. An overridden gate reads "Overridden · originally BLOCK":
 * the decision it did not change stays in sight (spec §92).
 */
export function GateOutcomeBadge({
  gate,
  className,
}: {
  gate: Pick<GateSummary, "effective_outcome" | "outcome" | "overridden" | "incomplete"> | null | undefined;
  className?: string;
}) {
  const b = gateBadge(gate);
  return (
    <span className={cn("inline-flex flex-wrap items-center gap-1.5", className)}>
      <Badge tone={b.tone} data-testid="gate-outcome">
        {b.label}
      </Badge>
      {b.note ? <span className="text-xs text-slate-600">{b.note}</span> : null}
    </span>
  );
}

/** A cost or latency delta, coloured when it got worse or better. */
export function Delta({ value, text }: { value: number | null | undefined; text: string }) {
  const tone = deltaTone(value);
  return (
    <span
      className={cn(
        "tabular-nums",
        tone === "worse" && "text-rose-700",
        tone === "better" && "text-emerald-700",
        tone === "same" && "text-slate-700",
      )}
    >
      {text}
      {tone !== "same" ? (
        <span className="sr-only">{tone === "worse" ? " (worse)" : " (better)"}</span>
      ) : null}
    </span>
  );
}
