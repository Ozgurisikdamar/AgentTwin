"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Download, ShieldAlert, ShieldCheck } from "lucide-react";
import Link from "next/link";
import { type FormEvent, useState } from "react";
import { useCan, useMe } from "@/components/shell/me-context";
import { SeverityBadge } from "@/components/simulations/badges";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { CopyButton } from "@/components/ui/copy-button";
import { Input, Label } from "@/components/ui/input";
import { KeyValue } from "@/components/ui/key-value";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { api, withQuery } from "@/lib/api";
import {
  type AuditPage,
  type GateEvidence,
  type Release,
  type ReleaseGate,
  queryOf,
} from "@/lib/api/control-plane";
import type { EvalRunDetail } from "@/lib/api/evaluation";
import { formatCost, formatDateTime, formatPercent, shortId } from "@/lib/format";
import {
  OVERRIDE_REASON_MAX,
  type OverrideDraft,
  auditActionLabel,
  auditFacts,
  countLines,
  coverageRows,
  evalCaseHref,
  evidenceFileName,
  overrideBody,
  overrideProblems,
  riskContributions,
  toLocalInput,
  traceHref,
  whyLines,
} from "@/lib/releases";
import { actorLabel } from "@/lib/simulations";
import { useActionKey } from "@/lib/use-action-key";
import { cn } from "@/lib/utils";

const textareaClass =
  "w-full rounded-md border border-slate-300 bg-white px-2.5 py-2 text-sm text-slate-900 focus-visible:border-indigo-500 focus-visible:outline-2 focus-visible:outline-indigo-200";

/** An http(s) link, or the text when the value is anything else. */
function SafeLink({ href, children }: { href: string; children: React.ReactNode }) {
  if (!/^https?:\/\//i.test(href)) return <span className="break-all">{children}</span>;
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="break-all text-indigo-700 hover:underline"
    >
      {children}
    </a>
  );
}

const WHY_TITLE: Record<string, string> = {
  BLOCK: "Why it is blocked",
  WARN: "Why it warns",
  PASS: "Why it passes",
};

/**
 * Why the gate decided (spec §28, §78): the decision's own summary, its
 * counts in words and every rule that fired with the scenarios behind it.
 */
export function WhySection({ gate, onShowEvidence }: { gate: ReleaseGate; onShowEvidence: () => void }) {
  const d = gate.decision;
  if (!d) return null;
  const lines = countLines(d.counts);
  const why = whyLines(d);
  return (
    <Card data-testid="why">
      <CardHeader>
        <CardTitle>{WHY_TITLE[d.outcome] ?? "The decision"}</CardTitle>
        {why.length ? (
          <Button size="sm" variant="ghost" onClick={onShowEvidence}>
            See the evidence
          </Button>
        ) : null}
      </CardHeader>
      <CardContent className="space-y-3">
        <p className="text-sm text-slate-800">{d.summary}</p>
        {d.incomplete ? (
          <p className="flex items-center gap-2 text-sm font-medium text-rose-800" role="note">
            <AlertTriangle className="h-4 w-4" aria-hidden="true" />
            Evidence is missing: a gate without evidence cannot pass.
          </p>
        ) : null}
        {lines.length ? (
          <ul className="flex flex-wrap gap-2" aria-label="What the evaluation found">
            {lines.map((l) => (
              <li key={l} className="rounded-md bg-slate-100 px-2 py-1 text-xs font-medium text-slate-800">
                {l}
              </li>
            ))}
          </ul>
        ) : null}
        {why.length ? (
          <ol
            className="divide-y divide-slate-100 rounded-md border border-slate-200"
            aria-label="Rules that fired"
          >
            {why.map((w) => (
              <li key={w.rule} className="flex flex-wrap items-start gap-2 px-3 py-2" data-testid="why-rule">
                <Badge tone={w.outcome === "BLOCK" ? "danger" : "warning"}>{w.outcome}</Badge>
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-medium text-slate-900">{w.title}</p>
                  <p className="text-xs text-slate-600">{w.statement}</p>
                  {w.scenarios.length ? (
                    <p className="mt-1 text-xs text-slate-600">
                      {w.scenarios.length === 1 ? "Scenario" : "Scenarios"}:{" "}
                      <span className="font-mono">{w.scenarios.join(", ")}</span>
                      {w.preExisting ? ` · ${w.preExisting} also failing on the baseline` : ""}
                    </p>
                  ) : null}
                </div>
                <code className="text-[11px] text-slate-500">{w.rule}</code>
              </li>
            ))}
          </ol>
        ) : null}
      </CardContent>
    </Card>
  );
}

/**
 * The override, stated next to the decision it did not change (spec §92):
 * "Originally BLOCKED, overridden by …, reason …".
 */
export function OverrideBanner({ gate, meUserId }: { gate: ReleaseGate; meUserId?: string }) {
  const o = gate.override;
  if (!o) return null;
  const until = o.expires_at ? formatDateTime(o.expires_at) : null;
  return (
    <div
      role="note"
      data-testid="override-banner"
      className={cn(
        "rounded-md border p-4 text-sm",
        o.active
          ? "border-indigo-200 bg-indigo-50 text-indigo-950"
          : "border-slate-200 bg-slate-50 text-slate-800",
      )}
    >
      <p className="font-semibold">
        Originally {o.original_outcome === "BLOCK" ? "BLOCKED" : "WARNED"} ·{" "}
        {o.active ? "overridden" : "override expired"}
      </p>
      <p className="mt-1">
        Overridden by {actorLabel(o.actor, meUserId)} on {formatDateTime(o.created_at)}
        {until
          ? o.active
            ? `, until ${until}`
            : `; it expired on ${until} and the decision applies again`
          : ""}
        .
      </p>
      <p className="mt-1 whitespace-pre-line">
        <span className="font-medium">Reason:</span> {o.reason}
      </p>
      {o.ticket_url ? (
        <p className="mt-1">
          <span className="font-medium">Ticket:</span> <SafeLink href={o.ticket_url}>{o.ticket_url}</SafeLink>
        </p>
      ) : null}
    </div>
  );
}

/** The override form: reason, ticket and expiry; the gate as it now stands on success. */
export function OverrideForm({
  release,
  gate,
  onCancel,
  onDone,
}: {
  release: Release;
  gate: ReleaseGate;
  onCancel: () => void;
  onDone: (g: ReleaseGate) => void;
}) {
  const [draft, setDraft] = useState<OverrideDraft>({ reason: "", ticketUrl: "", expiresAt: "" });
  const [touched, setTouched] = useState(false);
  // When the form opened: what "in the future" means while it is filled in
  // (checked again against the clock on submit).
  const [openedAt] = useState(() => new Date());
  const key = useActionKey("override");
  const qc = useQueryClient();
  const problems = overrideProblems(draft, openedAt);
  const override = useMutation({
    mutationFn: () =>
      api<ReleaseGate>(`/releases/${release.id}/override`, {
        method: "POST",
        idempotencyKey: key.key,
        body: overrideBody(draft, gate.revision),
      }),
    onSuccess: (g) => {
      qc.setQueryData(["release-gate", release.id, g.revision], g);
      void qc.invalidateQueries({ queryKey: ["release", release.id] });
      void qc.invalidateQueries({ queryKey: ["releases"] });
      onDone(g);
    },
    onSettled: (_d, error) => key.settle(error),
  });
  const show = (field: keyof OverrideDraft) => (touched ? problems[field] : undefined);

  function submit(e: FormEvent) {
    e.preventDefault();
    setTouched(true);
    const valid = Object.keys(overrideProblems(draft, new Date())).length === 0;
    if (valid && !override.isPending) override.mutate();
  }

  return (
    <Card className="border-indigo-200">
      <CardHeader>
        <CardTitle>Override the gate</CardTitle>
        <span className="text-xs text-slate-500">
          The decision stays {gate.decision?.outcome}; the override is recorded with your name and reason.
        </span>
      </CardHeader>
      <CardContent>
        <form className="space-y-3" onSubmit={submit} aria-label="Override the gate" noValidate>
          {override.isError ? <ErrorState error={override.error} /> : null}
          <div>
            <Label htmlFor="ovr-reason">Reason</Label>
            <textarea
              id="ovr-reason"
              rows={3}
              maxLength={OVERRIDE_REASON_MAX}
              value={draft.reason}
              onChange={(e) => setDraft({ ...draft, reason: e.target.value })}
              aria-invalid={Boolean(show("reason"))}
              aria-describedby="ovr-reason-help"
              placeholder="Why the release may go out despite the gate"
              className={textareaClass}
            />
            <p
              id="ovr-reason-help"
              className={cn("mt-1 text-xs", show("reason") ? "text-rose-700" : "text-slate-500")}
            >
              {show("reason") ?? "10 to 2000 characters."}
            </p>
          </div>
          <div className="flex flex-wrap gap-3">
            <div className="min-w-[16rem] flex-1">
              <Label htmlFor="ovr-ticket">Ticket URL (optional)</Label>
              <Input
                id="ovr-ticket"
                type="url"
                value={draft.ticketUrl}
                placeholder="https://tickets.example.com/OPS-12"
                onChange={(e) => setDraft({ ...draft, ticketUrl: e.target.value })}
                aria-invalid={Boolean(show("ticketUrl"))}
                aria-describedby="ovr-ticket-help"
              />
              <p id="ovr-ticket-help" className="mt-1 text-xs text-rose-700">
                {show("ticketUrl")}
              </p>
            </div>
            <div className="w-60">
              <Label htmlFor="ovr-expires">Expires (optional)</Label>
              <Input
                id="ovr-expires"
                type="datetime-local"
                value={draft.expiresAt}
                min={toLocalInput(openedAt)}
                onChange={(e) => setDraft({ ...draft, expiresAt: e.target.value })}
                aria-invalid={Boolean(show("expiresAt"))}
                aria-describedby="ovr-expires-help"
              />
              <p
                id="ovr-expires-help"
                className={cn("mt-1 text-xs", show("expiresAt") ? "text-rose-700" : "text-slate-500")}
              >
                {show("expiresAt") ?? "At most 90 days. Without it, it lasts until the next evaluation."}
              </p>
            </div>
          </div>
          <div className="flex gap-2">
            <Button type="submit" variant="danger" size="sm" disabled={override.isPending}>
              <ShieldAlert className="h-4 w-4" aria-hidden="true" />
              {override.isPending ? "Overriding…" : `Override ${gate.decision?.outcome ?? ""}`}
            </Button>
            <Button type="button" size="sm" variant="ghost" onClick={onCancel}>
              Cancel
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  );
}

/** The decision at a glance: counts, coverage, risk and costs (the Summary tab). */
export function SummarySection({ gate, meUserId }: { gate: ReleaseGate; meUserId?: string }) {
  const d = gate.decision;
  const s = gate.summary;
  const coverage = coverageRows(d?.coverage);
  const factors = riskContributions(d?.risk_index);
  return (
    <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
      <Card>
        <CardHeader>
          <CardTitle>Revision {gate.revision}</CardTitle>
        </CardHeader>
        <CardContent>
          <KeyValue
            items={[
              { label: "Status", value: gate.status === "DECIDED" ? "Decided" : "Evaluating" },
              {
                label: "Requested",
                value: `${formatDateTime(gate.requested_at)} by ${actorLabel(gate.requested_by, meUserId)}`,
              },
              { label: "Decided", value: formatDateTime(gate.decided_at) },
              {
                label: "Evaluation",
                value: gate.eval_run_id ? (
                  <Link href={`/evaluations/${gate.eval_run_id}`} className="text-indigo-700 hover:underline">
                    Eval run {shortId(gate.eval_run_id)}
                  </Link>
                ) : (
                  "nothing to run"
                ),
              },
              {
                label: "Scenarios",
                value: s ? `${s.evaluated} of ${s.scenarios} evaluated` : null,
              },
              {
                label: "In CI",
                value:
                  gate.exit_code == null
                    ? "waiting for the decision"
                    : `exit code ${gate.exit_code}${gate.ci_fails ? " (fails the job)" : " (the job passes)"}`,
              },
              { label: "Cost", value: s?.cost ? formatCost(s.cost.total_usd) : null },
              {
                label: "Rules version",
                value: d ? <code className="text-xs">{d.rules_version}</code> : null,
              },
            ]}
          />
        </CardContent>
      </Card>
      {d ? (
        <Card>
          <CardHeader>
            <CardTitle>Risk index {d.risk_index.value}/100</CardTitle>
            <span className="text-xs text-slate-500">Sorts releases; never decides.</span>
          </CardHeader>
          <CardContent className="space-y-2">
            {factors.length ? (
              <ul className="space-y-1 text-sm" aria-label="What adds to the risk index">
                {factors.map((f) => (
                  <li key={f.name} className="flex justify-between gap-3">
                    <span className="text-slate-700">
                      {f.name} <span className="text-slate-500">× {f.count}</span>
                    </span>
                    <span className="tabular-nums text-slate-900">+{f.contribution}</span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-sm text-slate-600">Nothing adds to it.</p>
            )}
            <p className="text-xs text-slate-500">{d.risk_index.formula}</p>
          </CardContent>
        </Card>
      ) : null}
      {coverage.length ? (
        <Card className="xl:col-span-2">
          <CardHeader>
            <CardTitle>Coverage</CardTitle>
          </CardHeader>
          <div className="relative overflow-x-auto">
            <table className="w-full text-left text-sm">
              <caption className="sr-only">What the evaluation covered</caption>
              <thead className="border-b border-slate-100 text-xs uppercase tracking-wide text-slate-500">
                <tr>
                  <th scope="col" className="px-3 py-2 font-medium">
                    Measure
                  </th>
                  <th scope="col" className="px-3 py-2 font-medium">
                    Covered
                  </th>
                  <th scope="col" className="px-3 py-2 font-medium">
                    Missing
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {coverage.map((c) => (
                  <tr key={c.name} data-testid="coverage-row">
                    <td className="px-3 py-2 text-slate-800">{c.name}</td>
                    <td className="px-3 py-2 tabular-nums">
                      <span className={c.complete ? "text-emerald-700" : "font-medium text-rose-700"}>
                        {c.covered}/{c.total}
                      </span>{" "}
                      <span className="text-xs text-slate-500">{formatPercent(c.covered / c.total, 0)}</span>
                    </td>
                    <td className="px-3 py-2 font-mono text-xs text-slate-600">
                      {c.missing?.length ? c.missing.join(", ") : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      ) : null}
    </div>
  );
}

function EvidenceItem({ ev, gate, projectId }: { ev: GateEvidence; gate: ReleaseGate; projectId: string }) {
  const facts: { label: string; value: React.ReactNode }[] = [];
  if (ev.candidate) facts.push({ label: "Candidate", value: ev.candidate });
  if (ev.baseline) facts.push({ label: "Baseline", value: ev.baseline });
  if (ev.divergence) facts.push({ label: "First divergence", value: ev.divergence });
  return (
    <li className="space-y-1.5 px-3 py-2.5" data-testid="evidence-item">
      <p className="text-sm text-slate-900">
        {ev.scenario ? (
          gate.eval_run_id ? (
            <Link
              href={evalCaseHref(gate.eval_run_id, ev.scenario)}
              className="font-mono font-medium text-indigo-700 hover:underline"
            >
              {ev.scenario}
            </Link>
          ) : (
            <span className="font-mono font-medium">{ev.scenario}</span>
          )
        ) : null}
        {ev.expectation ? <span className="font-mono text-slate-500"> · {ev.expectation}</span> : null}
        {ev.pre_existing ? (
          <Badge tone="neutral" className="ml-2">
            the baseline fails the same way
          </Badge>
        ) : null}
      </p>
      <p className="text-sm text-slate-700">{ev.summary}</p>
      {facts.length ? <KeyValue items={facts} className="text-xs" /> : null}
      {ev.observed?.length ? (
        <div>
          <p className="text-xs font-medium text-slate-500">Observed</p>
          <ul className="mt-0.5 space-y-0.5 font-mono text-xs text-slate-700">
            {ev.observed.map((o, i) => (
              <li key={`${o.ref ?? ""}-${i}`}>
                {o.detail}
                {o.ref ? <span className="text-slate-500"> ({o.ref})</span> : null}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {ev.trace_id ? (
        <Link href={traceHref(ev.trace_id, projectId)} className="text-xs text-indigo-700 hover:underline">
          Candidate trace {shortId(ev.trace_id, 12)}
        </Link>
      ) : null}
    </li>
  );
}

/** Every rule with its exact evidence (the Evals tab, spec §78). */
export function EvidenceByRule({ gate, projectId }: { gate: ReleaseGate; projectId: string }) {
  const rules = gate.decision?.rules ?? [];
  if (!gate.decision) return <EmptyState title="The evaluation has not decided yet" />;
  if (rules.length === 0) {
    return (
      <EmptyState title="No rule fired">
        Every required scenario was evaluated and nothing regressed.
        {gate.eval_run_id ? (
          <>
            {" "}
            <Link href={`/evaluations/${gate.eval_run_id}`} className="text-indigo-700 underline">
              Open the eval run
            </Link>
            .
          </>
        ) : null}
      </EmptyState>
    );
  }
  return (
    <div className="space-y-4">
      {rules.map((r) => (
        <Card key={r.rule} data-testid="rule-evidence">
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Badge tone={r.outcome === "BLOCK" ? "danger" : "warning"}>{r.outcome}</Badge>
              {r.title}
            </CardTitle>
            <code className="text-xs text-slate-500">{r.rule}</code>
          </CardHeader>
          <CardContent className="space-y-2 p-0">
            <p className="px-4 pt-3 text-sm text-slate-700">
              <span className="font-medium">Rule:</span> {r.statement}
            </p>
            <ul className="divide-y divide-slate-100" aria-label={`Evidence of ${r.title}`}>
              {r.evidence.map((ev, i) => (
                <EvidenceItem
                  key={`${ev.scenario ?? ""}-${ev.expectation ?? ""}-${i}`}
                  ev={ev}
                  gate={gate}
                  projectId={projectId}
                />
              ))}
            </ul>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

/** The suite the revision pinned, and the simulations that ran it (the Simulations tab). */
export function SuiteSection({ gate }: { gate: ReleaseGate }) {
  const run = useQuery({
    queryKey: ["eval-run", gate.eval_run_id],
    queryFn: ({ signal }) => api<EvalRunDetail>(`/eval-runs/${gate.eval_run_id}`, { signal }),
    enabled: Boolean(gate.eval_run_id),
  });
  const r = run.data?.run;
  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle>Simulations</CardTitle>
          {gate.eval_run_id ? (
            <Link
              href={`/evaluations/${gate.eval_run_id}`}
              className="text-xs text-indigo-700 hover:underline"
            >
              Open the eval run
            </Link>
          ) : null}
        </CardHeader>
        <CardContent>
          {!gate.eval_run_id ? (
            <p className="text-sm text-slate-600">
              Nothing was run: no scenario of the suite could be simulated.
            </p>
          ) : run.isPending ? (
            <Skeleton className="h-10 w-full" />
          ) : run.isError ? (
            <ErrorState error={run.error} />
          ) : r ? (
            <KeyValue
              items={[
                { label: "Eval run status", value: r.status },
                {
                  label: "Baseline run",
                  value: r.baseline_run_id ? (
                    <Link
                      href={`/simulations/${r.baseline_run_id}`}
                      className="text-indigo-700 hover:underline"
                    >
                      v{r.baseline_version} simulation
                    </Link>
                  ) : null,
                },
                {
                  label: "Candidate run",
                  value: r.candidate_run_id ? (
                    <Link
                      href={`/simulations/${r.candidate_run_id}`}
                      className="text-indigo-700 hover:underline"
                    >
                      v{r.candidate_version} simulation
                    </Link>
                  ) : null,
                },
                { label: "Seed", value: r.pinning?.seed ?? r.seed ?? null },
              ]}
            />
          ) : null}
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>Pinned suite</CardTitle>
          <span className="text-xs text-slate-500">
            {gate.suite.length} {gate.suite.length === 1 ? "scenario" : "scenarios"}, each at the version this
            revision pinned
          </span>
        </CardHeader>
        {gate.suite.length === 0 ? (
          <EmptyState title="The change required no scenario" />
        ) : (
          <div className="relative overflow-x-auto">
            <table className="w-full min-w-[40rem] text-left text-sm">
              <caption className="sr-only">Scenarios the release required</caption>
              <thead className="border-b border-slate-100 text-xs uppercase tracking-wide text-slate-500">
                <tr>
                  <th scope="col" className="px-3 py-2 font-medium">
                    Scenario
                  </th>
                  <th scope="col" className="px-3 py-2 font-medium">
                    Severity
                  </th>
                  <th scope="col" className="px-3 py-2 font-medium">
                    Why it runs
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {gate.suite.map((s) => (
                  <tr key={s.scenario_name} className="align-top" data-testid="suite-row">
                    <td className="px-3 py-2">
                      {gate.eval_run_id && s.scenario_version_id ? (
                        <Link
                          href={evalCaseHref(gate.eval_run_id, s.scenario_name)}
                          className="font-mono text-indigo-700 hover:underline"
                        >
                          {s.scenario_name}
                        </Link>
                      ) : (
                        <span className="font-mono">{s.scenario_name}</span>
                      )}
                      <div className="mt-1 flex flex-wrap gap-1">
                        {s.mandatory ? <Badge tone="brand">mandatory</Badge> : null}
                        {s.known_regression ? <Badge tone="danger">known regression</Badge> : null}
                        {s.scenario_version_id ? null : (
                          <Badge
                            tone="warning"
                            title="The scenario library did not confirm it; its result is missing"
                          >
                            not in the library
                          </Badge>
                        )}
                      </div>
                    </td>
                    <td className="px-3 py-2">
                      <SeverityBadge severity={s.severity} />
                    </td>
                    <td className="px-3 py-2 text-slate-700">
                      {s.why.length ? (
                        <ul className="list-disc space-y-0.5 pl-4 text-xs">
                          {s.why.map((w) => (
                            <li key={w}>{w}</li>
                          ))}
                        </ul>
                      ) : (
                        "—"
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}

/** Save the gate as served: the decision, what it rests on, and its hash. */
function downloadEvidence(release: Release, gate: ReleaseGate) {
  const blob = new Blob([`${JSON.stringify(gate, null, 2)}\n`], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = evidenceFileName(release, gate.revision);
  a.click();
  URL.revokeObjectURL(url);
}

/** The decision's hash and what it rests on (the Evidence tab, spec §91). */
export function EvidenceSection({ release, gate }: { release: Release; gate: ReleaseGate }) {
  const p = gate.policy;
  return (
    <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
      <Card>
        <CardHeader>
          <CardTitle>Immutable evidence</CardTitle>
          {gate.decision ? (
            <Button size="sm" onClick={() => downloadEvidence(release, gate)}>
              <Download className="h-4 w-4" aria-hidden="true" />
              Download JSON
            </Button>
          ) : null}
        </CardHeader>
        <CardContent className="space-y-3">
          {gate.evidence_sha256 ? (
            <>
              <div className="flex items-center gap-2">
                <code className="break-all text-xs" data-testid="evidence-hash">
                  {gate.evidence_sha256}
                </code>
                <CopyButton value={gate.evidence_sha256} label="evidence hash" />
              </div>
              {gate.evidence_verified ? (
                <p
                  className="flex items-center gap-2 text-sm text-emerald-800"
                  data-testid="evidence-verified"
                >
                  <ShieldCheck className="h-4 w-4" aria-hidden="true" />
                  Verified: the stored decision and its input still hash to it.
                </p>
              ) : (
                <p className="flex items-center gap-2 text-sm font-medium text-rose-800" role="alert">
                  <ShieldAlert className="h-4 w-4" aria-hidden="true" />
                  NOT VERIFIED: the stored decision no longer matches its hash.
                </p>
              )}
            </>
          ) : (
            <p className="text-sm text-slate-600">The hash is recorded when the gate decides.</p>
          )}
          <p className="text-xs text-slate-500">
            SHA-256 of the canonical JSON of the decision and the input it rests on: the policy, the
            change&apos;s impact, the pinned suite and the tools, as they were when revision {gate.revision}{" "}
            was requested. A decision is stored once and never rewritten; evaluating again adds a revision.
          </p>
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>Gate policy applied</CardTitle>
        </CardHeader>
        <CardContent>
          <KeyValue
            items={[
              { label: "Always run tags", value: p.always_run_tags.join(", ") || "none" },
              {
                label: "Known regressions",
                value: p.include_known_regressions ? "replayed" : "not replayed",
              },
              { label: "Graph depth", value: p.max_depth },
              { label: "Budget", value: formatCost(p.max_gate_cost_usd) },
              {
                label: "Latency regression",
                value: `over ${p.latency_regression_pct}% and ${p.latency_regression_min_ms} ms`,
              },
              { label: "Cost regression", value: `over ${p.cost_regression_pct}%` },
              { label: "Semantic regression", value: `a drop over ${p.semantic_regression_drop}` },
              { label: "Judge agreement", value: `at least ${formatPercent(p.judge_min_agreement, 0)}` },
              { label: "Reviewers may override", value: p.allow_reviewer_override ? "yes" : "no" },
              { label: "Warnings fail CI", value: p.warn_fails_ci ? "yes" : "no" },
            ]}
          />
        </CardContent>
      </Card>
    </div>
  );
}

/** The release's entries in the audit log (the Audit tab; needs settings.read). */
export function AuditSection({ releaseId }: { releaseId: string }) {
  const canRead = useCan("settings.read");
  const me = useMe();
  const audit = useQuery({
    queryKey: ["release-audit", releaseId],
    queryFn: ({ signal }) =>
      api<AuditPage>(
        withQuery(
          "/audit",
          queryOf<"listAudit">({ resource_type: "release", resource_id: releaseId, limit: 100 }),
        ),
        { signal },
      ),
    enabled: canRead,
  });
  if (!canRead) {
    return (
      <EmptyState title="The audit log is for administrators">
        Owners and administrators can read who created, evaluated and overrode this release.
      </EmptyState>
    );
  }
  if (audit.isPending) return <Skeleton className="h-24 w-full" />;
  if (audit.isError) return <ErrorState error={audit.error} />;
  const items = audit.data.items;
  if (items.length === 0) return <EmptyState title="No audit entries" />;
  return (
    <Card>
      <ol className="divide-y divide-slate-100" aria-label="Audit entries, newest first">
        {items.map((e) => (
          <li key={e.id} className="space-y-0.5 px-4 py-2.5 text-sm" data-testid="audit-entry">
            <p className="flex flex-wrap items-center gap-2">
              <span className="font-medium text-slate-900">{auditActionLabel(e.action)}</span>
              <span className="text-slate-600">by {actorLabel(e.actor, me?.user?.id)}</span>
              <span className="text-xs text-slate-500" title={e.occurred_at}>
                {formatDateTime(e.occurred_at)}
              </span>
            </p>
            {e.reason ? <p className="whitespace-pre-line text-slate-700">“{e.reason}”</p> : null}
            {auditFacts(e).length ? (
              <p className="text-xs text-slate-600">{auditFacts(e).join(" · ")}</p>
            ) : null}
            <p className="font-mono text-[11px] text-slate-500">entry {shortId(e.entry_hash, 16)}</p>
          </li>
        ))}
      </ol>
    </Card>
  );
}
