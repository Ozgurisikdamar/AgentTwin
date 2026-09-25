import { AlertTriangle, CheckCircle2, Loader2, XCircle } from "lucide-react";
import { ErrorState } from "@/components/ui/states";
import { shortId } from "@/lib/format";
import type { ScenarioValidation } from "@/lib/types";

export interface ValidationPanelProps {
  /** A local YAML syntax error (the server is not asked until it parses). */
  syntax: { error: string; line: number | null } | null;
  result: ScenarioValidation | undefined;
  checking: boolean;
  error: unknown;
}

/** What the server says about the current text: valid, or why not. */
export function ValidationPanel({ syntax, result, checking, error }: ValidationPanelProps) {
  return (
    <div className="space-y-2 text-sm" data-testid="validation" aria-live="polite">
      {syntax ? (
        <p className="flex items-start gap-2 text-rose-800" data-state="syntax-error">
          <XCircle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
          <span>
            YAML syntax error{syntax.line ? ` on line ${syntax.line}` : ""}: {syntax.error}
          </span>
        </p>
      ) : checking && !result ? (
        <p className="flex items-center gap-2 text-slate-600" data-state="checking">
          <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden="true" />
          Checking…
        </p>
      ) : error ? (
        <ErrorState error={error} />
      ) : result ? (
        result.valid ? (
          <div className="flex items-start gap-2 text-emerald-800" data-state="valid">
            <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
            <span>
              Valid
              {result.twin ? ` against twin ${result.twin.name} v${result.twin.version}` : ""}
              {result.spec_hash ? (
                <span className="ml-1 font-mono text-xs text-slate-500" title={result.spec_hash}>
                  {shortId(result.spec_hash, 10)}
                </span>
              ) : null}
              {checking ? <span className="ml-1 text-xs text-slate-500">(rechecking…)</span> : null}
            </span>
          </div>
        ) : (
          <div data-state="invalid">
            <p className="flex items-center gap-2 font-medium text-rose-800">
              <XCircle className="h-4 w-4 shrink-0" aria-hidden="true" />
              {result.problems.length} {result.problems.length === 1 ? "problem" : "problems"}
            </p>
            <ul className="mt-1 list-disc space-y-0.5 pl-6 text-xs text-rose-800" aria-label="Problems">
              {result.problems.map((p) => (
                <li key={p}>{p}</li>
              ))}
            </ul>
          </div>
        )
      ) : null}
      {!syntax && result?.warnings.length ? (
        <div data-state="warnings">
          <p className="flex items-center gap-2 font-medium text-amber-900">
            <AlertTriangle className="h-4 w-4 shrink-0" aria-hidden="true" />
            {result.warnings.length} {result.warnings.length === 1 ? "warning" : "warnings"}
          </p>
          <ul className="mt-1 list-disc space-y-0.5 pl-6 text-xs text-amber-900" aria-label="Warnings">
            {result.warnings.map((w) => (
              <li key={w}>{w}</li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}
