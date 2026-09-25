import { AlertTriangle, CheckCircle2, Info } from "lucide-react";
import Link from "next/link";
import { SeverityBadge } from "@/components/simulations/badges";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/states";
import type { ChangeImpact, ImpactAffected, ImpactLinked, ImpactScenario } from "@/lib/api/control-plane";
import { HASHING_MODEL, graphHref, kindLabel, reasonTags, similarityLabel } from "@/lib/changes";
import { humanize } from "@/lib/format";
import { PathChain } from "./path-chain";

const PROBLEM_SERVICE: Record<string, string> = {
  "graph-service": "The dependency graph",
  "simulation-service": "The scenario library",
};

/** Whether the impact can be trusted as the whole answer, and why not. */
export function ImpactStatus({ impact }: { impact: ChangeImpact }) {
  const unresolved = impact.graph?.unresolved ?? [];
  const settled = impact.complete && unresolved.length === 0;
  return (
    <div className="space-y-2" data-testid="impact-status" data-complete={impact.complete}>
      {settled ? (
        <p className="flex items-center gap-1.5 text-sm text-emerald-800">
          <CheckCircle2 className="h-4 w-4" aria-hidden="true" />
          Complete: the dependency graph and the scenario library both answered.
        </p>
      ) : null}
      {!impact.complete ? (
        <div
          role="alert"
          className="rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900"
        >
          <p className="flex items-center gap-1.5 font-medium">
            <AlertTriangle className="h-4 w-4" aria-hidden="true" />
            Incomplete: this impact may miss scenarios. Treat the change as unverified.
          </p>
          <ul className="mt-1 list-disc pl-6 text-xs">
            {impact.problems.map((p) => (
              <li key={`${p.service}-${p.code}`}>
                {PROBLEM_SERVICE[p.service] ?? p.service} did not answer ({p.code}): {p.message}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {unresolved.length ? (
        <div className="rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
          <p className="font-medium">The dependency graph does not know every changed component yet:</p>
          <ul className="mt-1 list-disc pl-6 text-xs">
            {unresolved.map((u) => (
              <li key={`${u.component.kind}-${u.component.key}`}>
                {kindLabel(u.component.kind)} <span className="font-mono">{u.component.key}</span>
                {u.summary ? ` (${u.summary})` : ""}
              </li>
            ))}
          </ul>
          <p className="mt-1 text-xs">
            Registering the versions and importing the tool catalogs teaches it; recompute afterwards.
          </p>
        </div>
      ) : null}
      {impact.truncated || impact.graph?.truncated ? (
        <p className="flex items-center gap-1.5 text-xs text-amber-800">
          <AlertTriangle className="h-3.5 w-3.5" aria-hidden="true" />
          {impact.truncated
            ? "The scenario library selected more scenarios than it returned."
            : "The dependency graph stopped at its size limit."}
        </p>
      ) : null}
      {impact.notes.map((n) => (
        <p key={n} className="flex items-start gap-1.5 text-xs text-slate-600">
          <Info className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
          {n}
        </p>
      ))}
    </div>
  );
}

function ScenarioRow({ s, impact }: { s: ImpactScenario; impact: ChangeImpact }) {
  const tags = reasonTags(s, impact.embedding_model);
  return (
    <li className="space-y-2 px-4 py-3" data-testid="impact-scenario" data-scenario={s.name}>
      <div className="flex flex-wrap items-center gap-2">
        {s.id ? (
          <Link href={`/scenarios/${s.id}`} className="font-medium text-indigo-700 hover:underline">
            {s.name}
          </Link>
        ) : (
          <span className="font-medium text-slate-900">{s.name}</span>
        )}
        <SeverityBadge severity={s.severity} />
        {tags.map((t) => (
          <Badge key={t.key} tone={t.tone} data-reason={t.key}>
            {t.label}
          </Badge>
        ))}
        {!s.in_library ? (
          <Badge tone="warning" title="The scenario library did not confirm this scenario">
            not confirmed
          </Badge>
        ) : null}
      </div>
      {s.description ? <p className="text-xs text-slate-600">{s.description}</p> : null}
      <ul
        className="list-disc space-y-0.5 pl-5 text-sm text-slate-800"
        aria-label={`Why ${s.name} is required`}
      >
        {s.why.map((w) => (
          <li key={w}>{w}</li>
        ))}
      </ul>
      {s.reasons.graph.length ? (
        <details className="text-xs">
          <summary className="cursor-pointer text-slate-600 hover:text-slate-900">
            {s.reasons.graph.length === 1 ? "Show the path" : `Show ${s.reasons.graph.length} paths`}
          </summary>
          <ul className="mt-2 space-y-2">
            {s.reasons.graph.map((r, i) => (
              <li key={`${r.via.kind}-${r.via.key}-${i}`} className="space-y-1">
                <span className="text-slate-500">
                  through {kindLabel(r.via.kind).toLowerCase()}{" "}
                  <span className="font-mono">{r.via_label}</span>
                  {r.direct ? "" : " (weaker link)"}
                </span>
                <PathChain path={r.path} projectId={impact.project_id} />
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </li>
  );
}

/** The scenarios the change requires, strongest link first, each with why. */
export function RequiredScenarios({ impact }: { impact: ChangeImpact }) {
  const policyTags = impact.policy.always_run_tags;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Required scenarios</CardTitle>
        <span className="text-xs text-slate-500">
          {impact.counts.graph} linked by the graph · {impact.counts.similar} similar ·{" "}
          {impact.counts.always_run} always run
          {impact.counts.known_regression ? ` · ${impact.counts.known_regression} known regressions` : ""}
        </span>
      </CardHeader>
      {impact.scenarios.length === 0 ? (
        <EmptyState title="No scenario is required by this change">
          Nothing the change reaches is tested, no scenario is similar enough to it, and no scenario carries
          an always-run tag.
        </EmptyState>
      ) : (
        <ul className="divide-y divide-slate-100" aria-label="Required scenarios">
          {impact.scenarios.map((s) => (
            <ScenarioRow key={s.name} s={s} impact={impact} />
          ))}
        </ul>
      )}
      <div className="space-y-1 border-t border-slate-100 px-4 py-2 text-xs text-slate-500">
        {impact.embedding_model ? (
          <p data-testid="similarity-note">
            Similarity is {similarityLabel(impact.embedding_model)}
            {impact.embedding_model === HASHING_MODEL
              ? " (the local model compares words and identifiers, not meaning)"
              : ` (${impact.embedding_model})`}
            ; scenarios below {impact.min_similarity.toFixed(2)} are not selected by it.
          </p>
        ) : null}
        {policyTags.length ? (
          <p>
            The project&apos;s gate policy always runs scenarios tagged {policyTags.join(", ")}
            {impact.policy.include_known_regressions ? " and known regressions" : ""}; the graph is followed{" "}
            {impact.policy.max_depth} steps deep.
          </p>
        ) : null}
        {impact.unlinked_scenarios.length ? (
          <p>
            The graph also links {impact.unlinked_scenarios.length}{" "}
            {impact.unlinked_scenarios.length === 1 ? "scenario" : "scenarios"} that the library does not hold
            as active for this agent ({impact.unlinked_scenarios.join(", ")}).
          </p>
        ) : null}
      </div>
    </Card>
  );
}

function AffectedRow({ a, projectId }: { a: ImpactAffected; projectId: string }) {
  const risk = typeof a.attributes?.risk === "string" ? a.attributes.risk : "";
  return (
    <li
      className="space-y-1.5 px-4 py-3"
      data-testid="affected-component"
      data-kind={a.component.kind}
      data-key={a.component.key}
    >
      <div className="flex flex-wrap items-center gap-2">
        <Badge>{kindLabel(a.component.kind)}</Badge>
        <Link
          href={graphHref(projectId, a.component)}
          className="font-mono text-sm text-indigo-700 hover:underline"
          aria-label={`${kindLabel(a.component.kind)} ${a.label} in the dependency graph`}
        >
          {a.label}
        </Link>
        <SeverityBadge severity={a.severity} />
        {a.seed ? (
          <Badge tone="brand">changed</Badge>
        ) : (
          <span className="text-xs text-slate-500">
            {a.depth} {a.depth === 1 ? "step" : "steps"}
          </span>
        )}
        {!a.seed && !a.direct ? <Badge tone="warning">indirect</Badge> : null}
        {risk ? <Badge tone={risk === "READ" ? "neutral" : "danger"}>{humanize(risk)}</Badge> : null}
        {!a.certain ? <Badge tone="warning">inferred link</Badge> : null}
      </div>
      {a.seed ? null : <PathChain path={a.path} projectId={projectId} />}
      {a.factors.length ? <p className="text-xs text-slate-500">{a.factors.join(" · ")}</p> : null}
    </li>
  );
}

/** What the change reaches in the dependency graph (the blast radius). */
export function AffectedComponents({ impact }: { impact: ChangeImpact }) {
  const graph = impact.graph;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Blast radius</CardTitle>
        {graph ? (
          <span className="text-xs text-slate-500">
            {graph.affected.length < graph.affected_count
              ? `${graph.affected.length} most affected of ${graph.affected_count}`
              : `${graph.affected_count} ${graph.affected_count === 1 ? "component" : "components"}`}{" "}
            within {graph.max_depth} steps
          </span>
        ) : null}
      </CardHeader>
      {!graph ? (
        <EmptyState title="The dependency graph did not answer">
          The components this change reaches are unknown; recompute when the graph service is back.
        </EmptyState>
      ) : graph.affected.length === 0 ? (
        <EmptyState title="The change reaches nothing the graph knows" />
      ) : (
        <ul className="divide-y divide-slate-100" aria-label="Affected components, most affected first">
          {graph.affected.map((a) => (
            <AffectedRow key={`${a.component.kind}-${a.component.key}`} a={a} projectId={impact.project_id} />
          ))}
        </ul>
      )}
    </Card>
  );
}

/** Irreversible actions within reach and privileges the candidate gains. */
export function RiskyChanges({ impact }: { impact: ChangeImpact }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Irreversible actions and new privileges</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {impact.irreversible_actions.length === 0 && impact.new_privileges.length === 0 ? (
          <p className="text-sm text-slate-600">
            The change reaches no irreversible or administrative action.
          </p>
        ) : null}
        {impact.irreversible_actions.length ? (
          <ul className="space-y-2" aria-label="Irreversible actions within reach">
            {impact.irreversible_actions.map((a) => {
              const risk = typeof a.attributes?.risk === "string" ? a.attributes.risk : "";
              return (
                <li
                  key={`${a.component.kind}-${a.component.key}`}
                  className="space-y-1"
                  data-testid="irreversible-action"
                  data-key={a.component.key}
                >
                  <div className="flex flex-wrap items-center gap-2 text-sm">
                    <span className="font-mono text-slate-900">{a.label}</span>
                    {risk ? <Badge tone="danger">{humanize(risk)}</Badge> : null}
                    {a.direct ? null : <Badge tone="warning">indirect</Badge>}
                  </div>
                  <PathChain path={a.path} projectId={impact.project_id} />
                </li>
              );
            })}
          </ul>
        ) : null}
        {impact.new_privileges.length ? (
          <ul className="space-y-1 text-sm" aria-label="New privileges" data-testid="new-privileges">
            {impact.new_privileges.map((p) => (
              <li key={p.tool} className="flex flex-wrap items-center gap-2">
                <Badge tone="danger">{p.change === "added" ? "new tool" : "escalated"}</Badge>
                <span className="font-mono">{p.tool}</span>
                <span className="text-slate-600">
                  {p.change === "escalated" && p.from ? `${humanize(p.from)} → ` : ""}
                  {humanize(p.risk)}
                </span>
              </li>
            ))}
          </ul>
        ) : null}
      </CardContent>
    </Card>
  );
}

function LinkedList({
  title,
  items,
  projectId,
}: {
  title: string;
  items: ImpactLinked[];
  projectId: string;
}) {
  if (!items.length) return null;
  return (
    <div className="space-y-2">
      <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-500">{title}</h3>
      <ul className="space-y-2">
        {items.map((l) => (
          <li key={`${l.component.kind}-${l.component.key}`} className="space-y-1">
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <Link
                href={graphHref(projectId, l.component)}
                className="font-mono text-indigo-700 hover:underline"
              >
                {l.label}
              </Link>
              {l.severity ? <SeverityBadge severity={l.severity} /> : null}
            </div>
            {l.reasons[0] ? <PathChain path={l.reasons[0].path} projectId={projectId} /> : null}
          </li>
        ))}
      </ul>
    </div>
  );
}

/** Policies and evaluators that guard or judge what the change reaches. */
export function LinkedControls({ impact }: { impact: ChangeImpact }) {
  const policies = impact.graph?.policies ?? [];
  const evaluators = impact.graph?.evaluators ?? [];
  if (!policies.length && !evaluators.length) return null;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Policies and evaluators</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <LinkedList title="Policies" items={policies} projectId={impact.project_id} />
        <LinkedList title="Evaluators" items={evaluators} projectId={impact.project_id} />
      </CardContent>
    </Card>
  );
}
