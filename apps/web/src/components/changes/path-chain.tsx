import { ArrowLeft, ArrowRight } from "lucide-react";
import Link from "next/link";
import { Fragment } from "react";
import { graphHref, kindLabel, pathChain, relationPhrase } from "@/lib/changes";
import { cn } from "@/lib/utils";

/**
 * A graph path from the changed component, one chip per component. The
 * connector between two chips names the relation and points the way the edge
 * does (a prompt is used by the agent version: the arrow points back at it).
 * Screen readers get the same path as sentences.
 */
export function PathChain({
  path,
  projectId,
  className,
}: {
  path: readonly unknown[] | null | undefined;
  projectId?: string;
  className?: string;
}) {
  const links = pathChain(path);
  if (links.length === 0) return null;
  const sentences = links.flatMap((l) => (l.sentence ? [l.sentence] : []));
  return (
    <div className={cn("min-w-0", className)} data-testid="path-chain">
      <ol className="flex flex-wrap items-center gap-1 text-xs" aria-hidden={sentences.length > 0}>
        {links.map((l, i) => {
          const chip = (
            <span className="inline-flex max-w-[22rem] items-center gap-1 rounded border border-slate-200 bg-slate-50 px-1.5 py-0.5">
              <span className="text-[10px] uppercase tracking-wide text-slate-500">{kindLabel(l.kind)}</span>
              <span className="truncate font-mono text-slate-900" title={l.key}>
                {l.label}
              </span>
            </span>
          );
          return (
            <Fragment key={`${i}-${l.kind}-${l.key}`}>
              {i > 0 && l.edge ? (
                <li className="inline-flex items-center gap-0.5 text-slate-500" data-edge={l.edge}>
                  {l.direction === "up" ? <ArrowLeft className="h-3 w-3" aria-hidden="true" /> : null}
                  <span>{relationPhrase(l.edge)}</span>
                  {l.direction === "up" ? null : <ArrowRight className="h-3 w-3" aria-hidden="true" />}
                </li>
              ) : null}
              <li data-kind={l.kind} data-key={l.key}>
                {projectId && l.kind && l.key ? (
                  <Link
                    href={graphHref(projectId, { kind: l.kind, key: l.key })}
                    className="rounded hover:ring-1 hover:ring-indigo-300"
                    tabIndex={sentences.length > 0 ? -1 : undefined}
                  >
                    {chip}
                  </Link>
                ) : (
                  chip
                )}
              </li>
            </Fragment>
          );
        })}
      </ol>
      {sentences.length > 0 ? (
        <ol className="sr-only">
          {sentences.map((s, i) => (
            <li key={i}>{s}</li>
          ))}
        </ol>
      ) : null}
    </div>
  );
}
