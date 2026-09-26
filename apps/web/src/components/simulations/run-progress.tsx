import { countOf } from "@/lib/format";
import { runProgress, runVerdictSummary } from "@/lib/simulations";
import type { SimulationRun } from "@/lib/types";
import { cn } from "@/lib/utils";

const SEGMENT: Record<string, string> = {
  passed: "bg-emerald-500",
  failed: "bg-rose-500",
  errored: "bg-amber-400",
  cancelled: "bg-slate-400",
};

/** Finished cases by verdict as a bar, with the counts spelled out. */
export function RunProgress({ run, className }: { run: SimulationRun; className?: string }) {
  const p = runProgress(run);
  const summary = runVerdictSummary(run);
  return (
    <div className={cn("flex min-w-36 flex-col gap-1", className)}>
      <div
        role="img"
        aria-label={`${summary}, ${p.done} of ${countOf(p.total, "scenario")} finished`}
        className="flex h-2 w-full overflow-hidden rounded-full bg-slate-100"
      >
        {p.segments.map((s) => (
          <span key={s.key} className={SEGMENT[s.key]} style={{ width: `${s.pct}%` }} />
        ))}
      </div>
      <span className="text-xs text-slate-600" aria-hidden="true">
        {summary}
        <span className="text-slate-500"> · {p.total} total</span>
      </span>
    </div>
  );
}
