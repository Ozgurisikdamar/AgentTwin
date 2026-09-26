import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

export interface KeyValueItem {
  label: string;
  value: ReactNode;
}

/** A semantic definition list; empty values are rendered as a dash. */
export function KeyValue({ items, className }: { items: KeyValueItem[]; className?: string }) {
  return (
    <dl
      className={cn(
        "grid grid-cols-[minmax(7rem,max-content)_minmax(0,1fr)] gap-x-4 gap-y-1.5 text-sm",
        className,
      )}
    >
      {items.map((it) => (
        <div key={it.label} className="contents">
          <dt className="text-slate-500">{it.label}</dt>
          <dd className="min-w-0 wrap-anywhere text-slate-900">
            {it.value === null || it.value === undefined || it.value === "" ? "—" : it.value}
          </dd>
        </div>
      ))}
    </dl>
  );
}
