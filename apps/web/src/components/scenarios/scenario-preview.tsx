import { FaultList, JsonBlock } from "@/components/simulations/case-parts";
import { SeverityBadge } from "@/components/simulations/badges";
import { Badge } from "@/components/ui/badge";
import { KeyValue } from "@/components/ui/key-value";
import { formatParams, formatValue } from "@/lib/simulations";
import type { ScenarioDocument } from "@/lib/types";

/** The keys an expectation row already shows; everything else is a parameter. */
const SHOWN = ["id", "type", "critical"] as const;

/** A read-only rendering of a scenario document. */
export function ScenarioPreview({ doc }: { doc: ScenarioDocument }) {
  const meta = doc.metadata ?? { name: "", severity: "high" };
  const spec = doc.spec ?? {};
  const input = spec.input ?? {};
  const expectations = spec.expectations ?? [];
  return (
    <div className="space-y-5" data-testid="scenario-preview">
      <section className="space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="text-base font-semibold text-slate-900">{meta.name || "Unnamed scenario"}</h3>
          {meta.severity ? <SeverityBadge severity={meta.severity} /> : null}
          {(meta.tags ?? []).map((t) => (
            <Badge key={t}>{t}</Badge>
          ))}
        </div>
        {meta.description ? (
          <p className="whitespace-pre-wrap text-sm text-slate-700">{meta.description}</p>
        ) : null}
        <KeyValue
          items={[
            { label: "Agent", value: spec.agent },
            { label: "Tool twin", value: spec.twin },
            { label: "Owner", value: meta.owner },
            {
              label: "Seed",
              value: spec.seed ?? <span className="text-slate-500">derived from each run&apos;s seed</span>,
            },
            { label: "Covers", value: spec.covers?.length ? spec.covers.join(", ") : null },
          ]}
        />
      </section>

      <section>
        <h4 className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">Input</h4>
        <p className="whitespace-pre-wrap rounded-md bg-slate-50 p-3 text-sm text-slate-900">
          {input.message ?? "—"}
        </p>
        {input.context ? (
          <div className="mt-2 flex flex-wrap gap-1">
            {Object.entries(input.context).map(([k, v]) => (
              <Badge key={k}>
                {k}: {typeof v === "string" ? v : formatValue(v, 40)}
              </Badge>
            ))}
          </div>
        ) : null}
        {input.documents?.length ? (
          <p className="mt-2 text-xs text-slate-600">
            {input.documents.length} retrieved {input.documents.length === 1 ? "document" : "documents"}:{" "}
            {input.documents.map((d) => `${String(d.id)}${d.trusted ? "" : " (untrusted)"}`).join(", ")}
          </p>
        ) : null}
      </section>

      {spec.state ? (
        <section>
          <h4 className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">
            Starting state (merged into the twin&apos;s)
          </h4>
          <JsonBlock value={spec.state} label="Starting state overrides" />
        </section>
      ) : null}

      <section>
        <h4 className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">Faults</h4>
        <div className="rounded-md border border-slate-100">
          <FaultList faults={spec.faults ?? []} />
        </div>
      </section>

      <section>
        <h4 className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">
          Expectations ({expectations.length})
        </h4>
        <ul className="divide-y divide-slate-100 rounded-md border border-slate-100">
          {expectations.map((e, i) => {
            const params = formatParams(e, SHOWN);
            return (
              <li key={`${e.id ?? i}`} className="px-3 py-2 text-sm">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium text-slate-900">{e.id ?? `#${i + 1}`}</span>
                  <code className="text-xs text-slate-500">{e.type}</code>
                  {e.critical ? <Badge tone="danger">critical</Badge> : null}
                </div>
                {params ? <p className="mt-0.5 font-mono text-xs text-slate-600">{params}</p> : null}
              </li>
            );
          })}
        </ul>
      </section>
    </div>
  );
}
