import { AlertTriangle, Inbox } from "lucide-react";
import type { ReactNode } from "react";
import type { ApiError } from "@/lib/api";
import { cn } from "@/lib/utils";

export function EmptyState({
  title,
  children,
  className,
}: {
  title: string;
  children?: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("flex flex-col items-center gap-2 px-6 py-10 text-center", className)}>
      <Inbox className="h-8 w-8 text-slate-500" aria-hidden="true" />
      <p className="text-sm font-medium text-slate-800">{title}</p>
      {children ? <div className="max-w-md text-sm text-slate-600">{children}</div> : null}
    </div>
  );
}

export function ErrorState({ error, className }: { error: unknown; className?: string }) {
  const e = error as Partial<ApiError> | undefined;
  const message = e?.message ?? "Something went wrong.";
  return (
    <div
      role="alert"
      className={cn(
        "flex items-start gap-3 rounded-md border border-rose-200 bg-rose-50 p-4 text-sm",
        className,
      )}
    >
      <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-rose-700" aria-hidden="true" />
      <div>
        <p className="font-medium text-rose-900">{message}</p>
        {e?.code || e?.requestId ? (
          <p className="mt-1 font-mono text-xs text-rose-800">
            {e.code}
            {e.requestId ? ` · request ${e.requestId}` : ""}
          </p>
        ) : null}
      </div>
    </div>
  );
}
