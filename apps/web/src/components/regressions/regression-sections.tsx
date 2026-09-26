"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FlaskConical, GitMerge, Save } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { type FormEvent, useState } from "react";
import { useMe } from "@/components/shell/me-context";
import { SeverityBadge } from "@/components/simulations/badges";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Label, Select } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { ApiError, api, withQuery } from "@/lib/api";
import {
  type FailureLabel,
  type MergeResponse,
  type PromoteResponse,
  type Regression,
  type RegressionDetail,
  type RegressionDraft,
  type RegressionPage,
  type RegressionResponse,
  type Severity,
  type TriageRequest,
  queryOf,
} from "@/lib/api/evaluation";
import { formatDateTime, shortId } from "@/lib/format";
import {
  FAILURE_LABELS,
  MAX_TAGS,
  SEVERITIES,
  assigneeProblem,
  eventSummary,
  parseTags,
  regressionActor,
  taxonomyLabel,
} from "@/lib/regressions";
import { parseYaml } from "@/lib/scenario-yaml";
import { useActionKey } from "@/lib/use-action-key";
import { cn } from "@/lib/utils";
import { RegressionStatusBadge } from "./regression-badges";

export const textareaClass =
  "w-full rounded-md border border-slate-300 bg-white px-2.5 py-2 text-sm text-slate-900 focus-visible:border-indigo-500 focus-visible:outline-2 focus-visible:outline-indigo-200";

const REASON_MAX = 2000;

/** What changed a regression: its detail, the inbox and (for a promotion) datasets. */
function useRefresh(regressionId: string) {
  const qc = useQueryClient();
  return (detail?: Regression) => {
    if (detail) {
      qc.setQueryData<RegressionDetail>(["regression", regressionId], (old) =>
        old ? { ...old, regression: detail } : old,
      );
    }
    void qc.invalidateQueries({ queryKey: ["regression", regressionId] });
    void qc.invalidateQueries({ queryKey: ["regressions"] });
  };
}

/** Problems the service named in a 409 (an incomplete draft, a merged group…). */
export function ProblemList({ error }: { error: unknown }) {
  const problems = error instanceof ApiError ? error.details?.problems : undefined;
  if (!Array.isArray(problems) || problems.length === 0) return null;
  return (
    <ul className="list-disc space-y-1 pl-5 text-sm text-rose-800" aria-label="What the service needs">
      {problems.map((p) => (
        <li key={String(p)}>{String(p)}</li>
      ))}
    </ul>
  );
}

// ------------------------------------------------------------------ failures

/** Every failure of the group, newest first (at most the latest 50). */
export function FailuresSection({ detail }: { detail: RegressionDetail }) {
  const r = detail.regression;
  if (detail.occurrences.length === 0) {
    return (
      <Card>
        <EmptyState title="No failures left in this group">
          {r.merged_into ? "They moved to the group it was merged into." : null}
        </EmptyState>
      </Card>
    );
  }
  return (
    <Card>
      <CardHeader>
        <CardTitle>Failures</CardTitle>
        <span className="text-xs text-slate-500">
          {r.occurrence_count > detail.occurrences.length
            ? `The latest ${detail.occurrences.length} of ${r.occurrence_count}`
            : `${r.occurrence_count} ${r.occurrence_count === 1 ? "failure" : "failures"}`}
        </span>
      </CardHeader>
      <div className="relative overflow-x-auto">
        <table className="w-full min-w-[56rem] text-left text-sm">
          <caption className="sr-only">Failures of this regression, newest first</caption>
          <thead className="border-b border-slate-100 text-xs uppercase tracking-wide text-slate-500">
            <tr>
              {["Trace", "Started", "Version", "Why it is a failure", "Severity", "Joined"].map((c) => (
                <th key={c} scope="col" className="px-3 py-2 font-medium">
                  {c}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {detail.occurrences.map((o) => (
              <tr key={o.trace_id} className="align-top" data-testid="occurrence-row">
                <td className="px-3 py-2.5">
                  <Link
                    href={`/traces/${o.trace_id}`}
                    className="font-mono text-xs text-indigo-700 hover:underline"
                    aria-label={`Trace ${o.trace_id}`}
                  >
                    {o.trace_id.slice(0, 12)}
                  </Link>
                  {o.trace_id === r.representative_trace_id ? (
                    <Badge tone="info" className="ml-2">
                      representative
                    </Badge>
                  ) : null}
                </td>
                <td className="px-3 py-2.5 text-slate-700">{formatDateTime(o.started_at)}</td>
                <td className="px-3 py-2.5">
                  {o.agent_version ? <Badge>v{o.agent_version}</Badge> : "—"}
                  {o.environment ? <div className="mt-1 text-xs text-slate-500">{o.environment}</div> : null}
                </td>
                <td className="px-3 py-2.5 text-slate-800">
                  <div className="font-medium">{o.title}</div>
                  <ul className="mt-1 list-disc space-y-0.5 pl-4 text-xs text-slate-600">
                    {o.reasons.map((reason) => (
                      <li key={reason}>{reason}</li>
                    ))}
                  </ul>
                </td>
                <td className="px-3 py-2.5">
                  <SeverityBadge severity={o.severity} />
                </td>
                <td className="px-3 py-2.5 text-xs text-slate-600">
                  <span className="font-medium text-slate-800">{o.join_kind}</span>
                  {o.similarity !== null ? ` (${o.similarity.toFixed(2)})` : null}
                  <div>{o.join_reason}</div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

// ------------------------------------------------------------------ history

export function HistorySection({ detail }: { detail: RegressionDetail }) {
  const me = useMe();
  return (
    <Card>
      <CardHeader>
        <CardTitle>History</CardTitle>
        <span className="text-xs text-slate-500">Every decision, with who made it and why.</span>
      </CardHeader>
      <CardContent>
        <ol className="space-y-3" aria-label="History of this regression">
          {detail.events.map((e) => (
            <li key={e.seq} className="text-sm" data-testid="regression-event">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-medium text-slate-900">{eventSummary(e)}</span>
                {e.to_status && e.to_status !== e.from_status ? (
                  <RegressionStatusBadge status={e.to_status} />
                ) : null}
              </div>
              <div className="text-xs text-slate-500">
                {formatDateTime(e.at)} · {regressionActor(e.actor, me?.user?.id)}
              </div>
              {e.reason ? <p className="mt-1 text-slate-700">“{e.reason}”</p> : null}
            </li>
          ))}
        </ol>
      </CardContent>
    </Card>
  );
}

// ------------------------------------------------------------------ triage

/** Label, severity, tags and assignee (`review.write`); only what changed is sent. */
export function TriageForm({ regression: r, onDone }: { regression: Regression; onDone: () => void }) {
  const refresh = useRefresh(r.id);
  const key = useActionKey("regression-triage");
  const [taxonomy, setTaxonomy] = useState<FailureLabel>(r.taxonomy);
  const [severity, setSeverity] = useState<Severity>(r.severity);
  const [tags, setTags] = useState(r.tags.join(", "));
  const [assignee, setAssignee] = useState(r.assignee ?? "");
  const [reason, setReason] = useState("");

  const parsed = parseTags(tags);
  const assigneeError = assigneeProblem(assignee);
  const body: TriageRequest = {};
  if (taxonomy !== r.taxonomy) body.taxonomy = taxonomy;
  if (severity !== r.severity) body.severity = severity;
  if (parsed.tags.join(",") !== r.tags.join(",")) body.tags = parsed.tags;
  if ((assignee.trim() || null) !== r.assignee) body.assignee = assignee.trim() || null;
  const changed = Object.keys(body).length > 0;
  const tagError = parsed.invalid.length
    ? `Not a tag: ${parsed.invalid.join(", ")} (lowercase letters, digits and _ : . -)`
    : parsed.tags.length > MAX_TAGS
      ? `At most ${MAX_TAGS} tags.`
      : null;

  const save = useMutation({
    mutationFn: () =>
      api<RegressionResponse>(`/regressions/${r.id}`, {
        method: "PATCH",
        idempotencyKey: key.key,
        body: { ...body, ...(reason.trim() ? { reason: reason.trim() } : {}) } satisfies TriageRequest,
      }),
    onSuccess: (out) => {
      refresh(out.regression);
      onDone();
    },
    onSettled: (_d, error) => key.settle(error),
  });

  function submit(e: FormEvent) {
    e.preventDefault();
    if (changed && !tagError && !assigneeError && !save.isPending) save.mutate();
  }

  return (
    <Card className="border-indigo-200">
      <CardHeader>
        <CardTitle>Triage</CardTitle>
        <span className="text-xs text-slate-500">
          A label or severity you set is kept when the group fails again.
        </span>
      </CardHeader>
      <CardContent>
        <form className="space-y-3" onSubmit={submit} aria-label="Triage" noValidate>
          {save.isError ? <ErrorState error={save.error} /> : null}
          <div className="flex flex-wrap gap-3">
            <div className="w-64">
              <Label htmlFor="tri-taxonomy">Label</Label>
              <Select
                id="tri-taxonomy"
                value={taxonomy}
                onChange={(e) => setTaxonomy(e.target.value as FailureLabel)}
              >
                {FAILURE_LABELS.map((l) => (
                  <option key={l} value={l}>
                    {taxonomyLabel(l)}
                    {l === r.suggested_taxonomy ? " (suggested)" : ""}
                  </option>
                ))}
              </Select>
            </div>
            <div className="w-40">
              <Label htmlFor="tri-severity">Severity</Label>
              <Select
                id="tri-severity"
                value={severity}
                onChange={(e) => setSeverity(e.target.value as Severity)}
              >
                {SEVERITIES.map((s) => (
                  <option key={s} value={s}>
                    {s}
                    {s === r.suggested_severity ? " (suggested)" : ""}
                  </option>
                ))}
              </Select>
            </div>
            <div className="min-w-[14rem] flex-1">
              <Label htmlFor="tri-assignee">Assignee</Label>
              <Input
                id="tri-assignee"
                value={assignee}
                placeholder="user:alex (empty: nobody)"
                aria-invalid={Boolean(assigneeError)}
                onChange={(e) => setAssignee(e.target.value)}
              />
              {assigneeError ? (
                <p className="mt-1 text-xs text-rose-700" role="alert">
                  {assigneeError}
                </p>
              ) : null}
            </div>
          </div>
          <div>
            <Label htmlFor="tri-tags">Tags</Label>
            <Input
              id="tri-tags"
              value={tags}
              placeholder="payments, refunds"
              aria-invalid={Boolean(tagError)}
              onChange={(e) => setTags(e.target.value)}
            />
            {tagError ? (
              <p className="mt-1 text-xs text-rose-700" role="alert">
                {tagError}
              </p>
            ) : null}
          </div>
          <div>
            <Label htmlFor="tri-reason">Reason (optional)</Label>
            <Input
              id="tri-reason"
              value={reason}
              maxLength={REASON_MAX}
              placeholder="Kept in the history and the audit log"
              onChange={(e) => setReason(e.target.value)}
            />
          </div>
          <div className="flex gap-2">
            <Button
              type="submit"
              size="sm"
              variant="primary"
              disabled={!changed || Boolean(tagError) || Boolean(assigneeError) || save.isPending}
            >
              <Save className="h-4 w-4" aria-hidden="true" />
              {save.isPending ? "Saving…" : "Save"}
            </Button>
            <Button type="button" size="sm" variant="ghost" onClick={onDone}>
              Cancel
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  );
}

// ------------------------------------------------------------------ status changes

const ACTION_WORDS = {
  confirm: { title: "Confirm", verb: "Confirm", help: "You agree it is a failure worth a test." },
  dismiss: { title: "Dismiss", verb: "Dismiss", help: "Not a failure worth a test. Say why: it is kept." },
  reopen: { title: "Reopen", verb: "Reopen", help: "It is back. Say why: it is kept." },
} as const;

/** Confirm, dismiss or reopen: a reason is required to dismiss or reopen. */
export function StatusForm({
  regression: r,
  action,
  onDone,
}: {
  regression: Regression;
  action: keyof typeof ACTION_WORDS;
  onDone: () => void;
}) {
  const refresh = useRefresh(r.id);
  const key = useActionKey(`regression-${action}`);
  const [reason, setReason] = useState("");
  const required = action !== "confirm";
  const words = ACTION_WORDS[action];
  const change = useMutation({
    mutationFn: () =>
      api<RegressionResponse>(`/regressions/${r.id}/${action}`, {
        method: "POST",
        idempotencyKey: key.key,
        body: reason.trim() ? { reason: reason.trim() } : {},
      }),
    onSuccess: (out) => {
      refresh(out.regression);
      onDone();
    },
    onSettled: (_d, error) => key.settle(error),
  });
  const ready = !required || reason.trim().length > 0;

  function submit(e: FormEvent) {
    e.preventDefault();
    if (ready && !change.isPending) change.mutate();
  }

  return (
    <Card className="border-indigo-200">
      <CardHeader>
        <CardTitle>{words.title} this regression</CardTitle>
        <span className="text-xs text-slate-500">{words.help}</span>
      </CardHeader>
      <CardContent>
        <form className="space-y-3" onSubmit={submit} aria-label={`${words.title} the regression`} noValidate>
          {change.isError ? <ErrorState error={change.error} /> : null}
          <div>
            <Label htmlFor="status-reason">{required ? "Reason" : "Reason (optional)"}</Label>
            <textarea
              id="status-reason"
              rows={2}
              maxLength={REASON_MAX}
              value={reason}
              required={required}
              onChange={(e) => setReason(e.target.value)}
              className={textareaClass}
            />
          </div>
          <div className="flex gap-2">
            <Button
              type="submit"
              size="sm"
              variant={action === "dismiss" ? "danger" : "primary"}
              disabled={!ready || change.isPending}
            >
              {change.isPending ? "Saving…" : words.verb}
            </Button>
            <Button type="button" size="sm" variant="ghost" onClick={onDone}>
              Cancel
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  );
}

// ------------------------------------------------------------------ merge

/** Merges this group into another of the same agent: its failures move there. */
export function MergeForm({ regression: r, onDone }: { regression: Regression; onDone: () => void }) {
  const router = useRouter();
  const qc = useQueryClient();
  const key = useActionKey("regression-merge");
  const [into, setInto] = useState("");
  const [reason, setReason] = useState("");
  const targets = useQuery({
    queryKey: ["regressions", r.project_id, "merge-targets", r.agent],
    queryFn: ({ signal }) =>
      api<RegressionPage>(
        withQuery(
          "/regressions/candidates",
          queryOf<"listRegressions">({ project_id: r.project_id, agent: r.agent, limit: 200 }),
        ),
        { signal },
      ),
  });
  const options = (targets.data?.items ?? []).filter((t) => t.id !== r.id && !t.merged_into);
  const merge = useMutation({
    mutationFn: () =>
      api<MergeResponse>(`/regressions/${r.id}/merge`, {
        method: "POST",
        idempotencyKey: key.key,
        body: { into, ...(reason.trim() ? { reason: reason.trim() } : {}) },
      }),
    onSuccess: (out) => {
      qc.setQueryData<RegressionDetail>(["regression", r.id], (old) =>
        old ? { ...old, regression: out.merged } : old,
      );
      void qc.invalidateQueries({ queryKey: ["regressions"] });
      void qc.invalidateQueries({ queryKey: ["regression"] });
      router.push(`/regressions/${out.regression.id}`);
    },
    onSettled: (_d, error) => key.settle(error),
  });

  function submit(e: FormEvent) {
    e.preventDefault();
    if (into && !merge.isPending) merge.mutate();
  }

  return (
    <Card className="border-indigo-200">
      <CardHeader>
        <CardTitle>Merge into another regression</CardTitle>
        <span className="text-xs text-slate-500">
          Its {r.occurrence_count} {r.occurrence_count === 1 ? "failure moves" : "failures move"} there; this
          group then only points to it.
        </span>
      </CardHeader>
      <CardContent>
        <form className="space-y-3" onSubmit={submit} aria-label="Merge the regression" noValidate>
          {targets.isError ? <ErrorState error={targets.error} /> : null}
          {merge.isError ? <ErrorState error={merge.error} /> : null}
          <div className="flex flex-wrap gap-3">
            <div className="min-w-[20rem] flex-1">
              <Label htmlFor="merge-into">Merge into</Label>
              <Select id="merge-into" value={into} onChange={(e) => setInto(e.target.value)}>
                <option value="">
                  {targets.isPending
                    ? "Loading…"
                    : options.length
                      ? "Choose a regression of this agent"
                      : "No other regression of this agent"}
                </option>
                {options.map((t) => (
                  <option key={t.id} value={t.id}>
                    {`${t.title} · ${t.status.toLowerCase()} · ${t.occurrence_count} · ${shortId(t.id)}`}
                  </option>
                ))}
              </Select>
            </div>
            <div className="min-w-[14rem] flex-1">
              <Label htmlFor="merge-reason">Reason (optional)</Label>
              <Input
                id="merge-reason"
                value={reason}
                maxLength={REASON_MAX}
                onChange={(e) => setReason(e.target.value)}
              />
            </div>
          </div>
          <div className="flex gap-2">
            <Button type="submit" size="sm" variant="primary" disabled={!into || merge.isPending}>
              <GitMerge className="h-4 w-4" aria-hidden="true" />
              {merge.isPending ? "Merging…" : "Merge"}
            </Button>
            <Button type="button" size="sm" variant="ghost" onClick={onDone}>
              Cancel
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  );
}

// ------------------------------------------------------------------ draft and promotion

/** The scenario the representative trace suggests, and its promotion into a regression test. */
export function DraftSection({ regression: r, canPromote }: { regression: Regression; canPromote: boolean }) {
  // Kept here: once the page reads the promoted regression the form is gone,
  // and what the promotion answered (version, redactions, warnings) with it.
  const [result, setResult] = useState<PromoteResponse | null>(null);
  const draft = useQuery({
    queryKey: ["regression-draft", r.id, r.representative_trace_id],
    queryFn: ({ signal }) => api<RegressionDraft>(`/regressions/${r.id}/draft`, { signal }),
    enabled: !r.scenario_name && !r.merged_into,
    retry: false,
  });
  if (r.merged_into) {
    return (
      <Card>
        <EmptyState title="Merged into another regression">Its draft is the other group&apos;s.</EmptyState>
      </Card>
    );
  }
  if (result?.regression.id === r.id && r.scenario_name)
    return <PromotedCard regression={r} result={result} />;
  if (r.scenario_name) return <PromotedCard regression={r} />;
  if (draft.isPending) return <Skeleton className="h-64 w-full" />;
  if (draft.isError) {
    return (
      <Card>
        <CardContent className="space-y-2 pt-4">
          <ErrorState error={draft.error} />
          {draft.error instanceof ApiError && draft.error.code === "REGRESSION_TRACE_GONE" ? (
            <p className="text-sm text-slate-600">
              Write the scenario yourself: promote it with a document from the{" "}
              <Link href="/scenarios/new" className="text-indigo-700 underline">
                scenario editor
              </Link>
              .
            </p>
          ) : null}
        </CardContent>
      </Card>
    );
  }
  return <DraftAndPromote regression={r} draft={draft.data} canPromote={canPromote} onPromoted={setResult} />;
}

function DraftAndPromote({
  regression: r,
  draft: out,
  canPromote,
  onPromoted,
}: {
  regression: Regression;
  draft: RegressionDraft;
  canPromote: boolean;
  onPromoted: (result: PromoteResponse) => void;
}) {
  const d = out.draft;
  const qc = useQueryClient();
  const refresh = useRefresh(r.id);
  const key = useActionKey("regression-promote");
  const [text, setText] = useState(d.yaml);
  const [reason, setReason] = useState("");
  const [result, setResult] = useState<PromoteResponse | null>(null);
  const edited = text !== d.yaml;
  const parsed = edited ? parseYaml(text) : null;
  const syntax = parsed && !parsed.ok ? parsed : null;
  // As drafted, it is promoted as it is (the service rebuilds it); edited, the person's text is sent.
  const ready = (edited ? parsed?.ok === true : d.complete) && !syntax;

  const promote = useMutation({
    mutationFn: () =>
      api<PromoteResponse>(`/regressions/${r.id}/promote`, {
        method: "POST",
        idempotencyKey: key.key,
        body: { ...(edited ? { yaml: text } : {}), ...(reason.trim() ? { reason: reason.trim() } : {}) },
      }),
    onSuccess: (res) => {
      setResult(res);
      onPromoted(res);
      refresh(res.regression);
      void qc.invalidateQueries({ queryKey: ["datasets"] });
    },
    onSettled: (_d, error) => key.settle(error),
  });

  function submit(e: FormEvent) {
    e.preventDefault();
    if (ready && !promote.isPending) promote.mutate();
  }

  if (result) return <PromotedCard regression={result.regression} result={result} />;

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle>What the draft assumed</CardTitle>
          <Badge tone={d.complete ? "success" : "warning"} data-testid="draft-state">
            {d.complete ? "Complete" : "Needs a person"}
          </Badge>
        </CardHeader>
        <CardContent className="space-y-3 text-sm">
          {d.problems.length ? (
            <div role="alert" className="rounded-md border border-amber-200 bg-amber-50 p-3">
              <p className="font-medium text-amber-900">Only a person can write:</p>
              <ul className="mt-1 list-disc space-y-1 pl-5 text-amber-900" aria-label="What the draft needs">
                {d.problems.map((p) => (
                  <li key={p}>{p}</li>
                ))}
              </ul>
            </div>
          ) : null}
          {d.notes.length ? (
            <ul className="list-disc space-y-1 pl-5 text-slate-700" aria-label="Draft notes">
              {d.notes.map((n) => (
                <li key={n}>{n}</li>
              ))}
            </ul>
          ) : null}
          {d.mappings.length ? (
            <div className="relative overflow-x-auto">
              <table className="w-full min-w-[36rem] text-left text-sm">
                <caption className="text-left text-xs font-medium uppercase tracking-wide text-slate-500">
                  Production records and the twin records that stand for them
                </caption>
                <thead className="border-b border-slate-100 text-xs text-slate-500">
                  <tr>
                    <th scope="col" className="px-2 py-1.5 font-medium">
                      Production
                    </th>
                    <th scope="col" className="px-2 py-1.5 font-medium">
                      Twin
                    </th>
                    <th scope="col" className="px-2 py-1.5 font-medium">
                      Differs
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {d.mappings.map((m) => (
                    <tr key={`${m.collection}:${m.source_id}`} data-testid="mapping-row">
                      <td className="px-2 py-1.5">
                        <code>
                          {m.collection}/{m.source_id}
                        </code>
                      </td>
                      <td className="px-2 py-1.5">
                        {m.target_id ? <code>{m.target_id}</code> : "—"}
                        {m.added ? <Badge className="ml-2">added</Badge> : null}
                      </td>
                      <td className="px-2 py-1.5 text-xs text-slate-600">
                        {m.differing.length
                          ? m.differing
                              .map(
                                (f) =>
                                  `${f.field}: ${JSON.stringify(f.observed)} → ${JSON.stringify(f.twin)}`,
                              )
                              .join("; ")
                          : "nothing"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
        </CardContent>
      </Card>
      <Card className={canPromote ? "border-indigo-200" : undefined}>
        <CardHeader>
          <CardTitle>{canPromote ? "Promote to a regression test" : "Scenario draft"}</CardTitle>
          <span className="text-xs text-slate-500">
            From trace{" "}
            <Link href={`/traces/${out.trace_id}`} className="font-mono text-indigo-700 hover:underline">
              {out.trace_id.slice(0, 12)}
            </Link>
            {canPromote ? "; joins production-regressions, which every release of this agent runs." : null}
          </span>
        </CardHeader>
        <CardContent>
          {canPromote ? (
            <form
              className="space-y-3"
              onSubmit={submit}
              aria-label="Promote to a regression test"
              noValidate
            >
              {promote.isError ? (
                <div className="space-y-2">
                  <ErrorState error={promote.error} />
                  <ProblemList error={promote.error} />
                </div>
              ) : null}
              <div>
                <Label htmlFor="promote-yaml">Scenario (YAML)</Label>
                <textarea
                  id="promote-yaml"
                  rows={Math.min(32, Math.max(12, text.split("\n").length + 1))}
                  value={text}
                  spellCheck={false}
                  aria-invalid={Boolean(syntax)}
                  aria-describedby="promote-yaml-help"
                  onChange={(e) => setText(e.target.value)}
                  className={cn(textareaClass, "font-mono text-xs")}
                />
                <p
                  id="promote-yaml-help"
                  className={cn("mt-1 text-xs", syntax ? "text-rose-700" : "text-slate-500")}
                >
                  {syntax
                    ? `YAML error${syntax.line ? ` on line ${syntax.line}` : ""}: ${syntax.error}`
                    : edited
                      ? "Your edit is promoted; its source stays this trace and its input is redacted."
                      : d.complete
                        ? "Promoted as drafted; edit it to change what the test expects."
                        : "Write what the draft needs before promoting it."}
                </p>
              </div>
              <div>
                <Label htmlFor="promote-reason">Reason (optional)</Label>
                <Input
                  id="promote-reason"
                  value={reason}
                  maxLength={REASON_MAX}
                  onChange={(e) => setReason(e.target.value)}
                />
              </div>
              <div className="flex gap-2">
                <Button type="submit" size="sm" variant="primary" disabled={!ready || promote.isPending}>
                  <FlaskConical className="h-4 w-4" aria-hidden="true" />
                  {promote.isPending ? "Promoting…" : "Promote to test"}
                </Button>
                {edited ? (
                  <Button type="button" size="sm" variant="ghost" onClick={() => setText(d.yaml)}>
                    Reset to the draft
                  </Button>
                ) : null}
              </div>
            </form>
          ) : (
            <pre
              className="max-h-[32rem] relative overflow-auto rounded-md bg-slate-50 p-3 text-xs"
              aria-label="Scenario YAML"
            >
              {d.yaml}
            </pre>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

/** A regression that is a test: where the test lives, and whether a version fixed it. */
function PromotedCard({ regression: r, result }: { regression: Regression; result?: PromoteResponse }) {
  const me = useMe();
  return (
    <Card className="border-emerald-200" data-testid="regression-test">
      <CardHeader>
        <CardTitle>{result ? "Promoted to a regression test" : "Regression test"}</CardTitle>
        <RegressionStatusBadge status={r.status} />
      </CardHeader>
      <CardContent className="space-y-2 text-sm text-slate-800">
        <p>
          <span className="text-slate-500">Scenario </span>
          {r.scenario_id ? (
            <Link
              href={`/scenarios/${r.scenario_id}`}
              className="font-medium text-indigo-700 hover:underline"
            >
              {r.scenario_name}
            </Link>
          ) : (
            <span className="font-medium">{r.scenario_name}</span>
          )}
          {result
            ? ` (version ${result.scenario.version}${result.scenario.created ? "" : ", already registered"})`
            : null}
        </p>
        <p>
          <span className="text-slate-500">Dataset </span>
          {r.dataset_id ? (
            <Link href={`/datasets/${r.dataset_id}`} className="font-medium text-indigo-700 hover:underline">
              {result?.dataset.name ?? "production-regressions"}
            </Link>
          ) : (
            "—"
          )}
          {r.dataset_version ? ` v${r.dataset_version}` : null}
        </p>
        {r.promoted_by ? (
          <p className="text-xs text-slate-500">
            Promoted {formatDateTime(r.promoted_at)} by {regressionActor(r.promoted_by, me?.user?.id)}
          </p>
        ) : null}
        {result && result.redacted > 0 ? (
          <p className="text-xs text-slate-600">
            {result.redacted} {result.redacted === 1 ? "value" : "values"} of its input were redacted.
          </p>
        ) : null}
        {result?.scenario.warnings.length ? (
          <ul className="list-disc pl-5 text-xs text-amber-800">
            {result.scenario.warnings.map((w) => (
              <li key={w}>{w}</li>
            ))}
          </ul>
        ) : null}
        {r.fixed_version ? (
          <p className="text-emerald-800">
            Fixed in <Badge tone="success">v{r.fixed_version}</Badge>
            {r.fixed_eval_run_id ? (
              <>
                {" "}
                by{" "}
                <Link href={`/evaluations/${r.fixed_eval_run_id}`} className="text-indigo-700 underline">
                  evaluation run {shortId(r.fixed_eval_run_id)}
                </Link>
              </>
            ) : null}
            .
          </p>
        ) : (
          <p className="text-slate-600">
            Every release of {r.agent} runs it as a known regression; it is fixed when a version passes it.
          </p>
        )}
      </CardContent>
    </Card>
  );
}
