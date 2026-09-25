"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, XCircle } from "lucide-react";
import Link from "next/link";
import { type FormEvent, useState } from "react";
import { PageHeader } from "@/components/common/page-header";
import { useCan, useMe } from "@/components/shell/me-context";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/input";
import { KeyValue } from "@/components/ui/key-value";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { api, withQuery } from "@/lib/api";
import {
  type Approval,
  type ApprovalDetail as ApprovalDetailData,
  type Decision,
  type PolicyPage,
  queryOf,
} from "@/lib/api/runtime";
import { formatDateTime, formatDuration, formatRelative, shortId } from "@/lib/format";
import { approvalStatusMeaning, argumentRows, canDecide, changeText, expiry } from "@/lib/runtime";
import { actorLabel } from "@/lib/simulations";
import { useActionKey } from "@/lib/use-action-key";
import { useProject } from "./project-picker";
import { ApprovalStatusBadge, AttemptBadge, EffectBadge, OutcomeBadge, RiskBadge } from "./runtime-badges";

const textareaClass =
  "w-full rounded-md border border-slate-300 bg-white px-2.5 py-2 text-sm text-slate-900 focus-visible:border-indigo-500 focus-visible:outline-2 focus-visible:outline-indigo-200";
const REASON_MAX = 2000;

/** Approve or deny the exact action, with a reason (recorded and audited). */
function DecideForm({ approval: a }: { approval: Approval }) {
  const qc = useQueryClient();
  const [reason, setReason] = useState("");
  const approveKey = useActionKey("approval-approve");
  const denyKey = useActionKey("approval-deny");
  const decide = useMutation({
    mutationFn: (action: "approve" | "deny") =>
      api<Approval>(`/approvals/${a.id}/${action}`, {
        method: "POST",
        idempotencyKey: action === "approve" ? approveKey.key : denyKey.key,
        body: { project_id: a.project_id, reason: reason.trim() },
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["approval", a.id] });
      void qc.invalidateQueries({ queryKey: ["approvals"] });
    },
    onSettled: (_d, error, action) => (action === "approve" ? approveKey : denyKey).settle(error),
  });
  const ready = reason.trim().length > 0 && !decide.isPending;

  function submit(e: FormEvent, action: "approve" | "deny") {
    e.preventDefault();
    if (ready) decide.mutate(action);
  }

  return (
    <Card className="border-amber-300" data-testid="decide-form">
      <CardHeader>
        <CardTitle>Decide</CardTitle>
        <span className="text-xs text-slate-500">
          Approving lets exactly the action below run once; the agent then repeats it with a token.
        </span>
      </CardHeader>
      <CardContent>
        <form
          className="space-y-3"
          onSubmit={(e) => submit(e, "approve")}
          aria-label="Decide the request"
          noValidate
        >
          {decide.isError ? <ErrorState error={decide.error} /> : null}
          <div>
            <Label htmlFor="decision-reason">Reason</Label>
            <textarea
              id="decision-reason"
              rows={2}
              maxLength={REASON_MAX}
              value={reason}
              required
              onChange={(e) => setReason(e.target.value)}
              className={textareaClass}
              placeholder="Why you approve or deny it (recorded and audited)"
            />
          </div>
          <div className="flex gap-2">
            <Button type="submit" size="sm" variant="primary" disabled={!ready}>
              <Check className="h-4 w-4" aria-hidden="true" />
              {decide.isPending && decide.variables === "approve" ? "Approving…" : "Approve this action"}
            </Button>
            <Button
              type="button"
              size="sm"
              variant="danger"
              disabled={!ready}
              onClick={(e) => submit(e, "deny")}
            >
              <XCircle className="h-4 w-4" aria-hidden="true" />
              {decide.isPending && decide.variables === "deny" ? "Denying…" : "Deny"}
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  );
}

/** The exact action a person approves: tool, arguments, hash. */
function ExactAction({ approval: a }: { approval: Approval }) {
  const rows = argumentRows(a.arguments);
  return (
    <Card>
      <CardHeader>
        <CardTitle>The exact action</CardTitle>
        <span className="text-xs text-slate-500">Any change to it needs its own approval.</span>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <code className="rounded bg-slate-100 px-1.5 py-0.5 font-semibold">{a.tool}</code>
          <RiskBadge risk={a.risk} />
          <span className="text-slate-600">
            by {a.agent} v{a.agent_version} in {a.environment}
          </span>
        </div>
        {rows.length === 0 ? (
          <p className="text-sm text-slate-600">No arguments.</p>
        ) : (
          <table className="w-full text-left text-sm" aria-label="Arguments">
            <thead className="text-xs uppercase tracking-wide text-slate-500">
              <tr>
                <th scope="col" className="py-1 pr-4 font-medium">
                  Argument
                </th>
                <th scope="col" className="py-1 font-medium">
                  Value
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {rows.map((r) => (
                <tr key={r.path} data-testid="argument-row">
                  <td className="py-1.5 pr-4 font-mono text-xs text-slate-700">{r.path}</td>
                  <td className="py-1.5 font-mono text-xs text-slate-900">{r.value}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <KeyValue
          className="text-xs"
          items={[
            { label: "Action hash", value: <code title={a.action_hash}>{a.action_hash.slice(0, 16)}</code> },
          ]}
        />
      </CardContent>
    </Card>
  );
}

/** Why the gateway held it: the policy, rule and its message. */
function Why({ approval: a, policyId }: { approval: Approval; policyId: string | undefined }) {
  const now = new Date();
  const due = expiry(a.expires_at, now);
  const me = useMe();
  return (
    <Card>
      <CardHeader>
        <CardTitle>Why it needs a person</CardTitle>
        {a.trace_id ? (
          <Link href={`/traces/${a.trace_id}`} className="text-xs text-indigo-700 hover:underline">
            Open the trace
          </Link>
        ) : null}
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        <p className="text-slate-900" data-testid="approval-reason">
          {a.reason}
        </p>
        <KeyValue
          className="text-xs"
          items={[
            {
              label: "Policy",
              value: policyId ? (
                <Link
                  href={`/policies/${policyId}?project_id=${a.project_id}`}
                  className="text-indigo-700 hover:underline"
                >
                  {a.policy}
                </Link>
              ) : (
                a.policy
              ),
            },
            { label: "Rule", value: <code>{a.rule}</code> },
            {
              label: "Expires",
              value: (
                <span title={a.expires_at}>
                  {formatDateTime(a.expires_at)} ({due.text})
                </span>
              ),
            },
            {
              label: "Requested",
              value: `${formatDateTime(a.created_at)} by ${actorLabel(a.requested_by, me?.user?.id)}`,
            },
            {
              label: "Decided",
              value: a.decided_at
                ? `${formatDateTime(a.decided_at)} by ${actorLabel(a.decided_by, me?.user?.id)}`
                : null,
            },
            { label: "Their reason", value: a.decision_reason },
            { label: "Used", value: a.used_at ? formatDateTime(a.used_at) : null },
            {
              label: "Trace",
              value: a.trace_id ? (
                <Link href={`/traces/${a.trace_id}`} className="font-mono text-indigo-700 hover:underline">
                  {a.trace_id}
                </Link>
              ) : null,
            },
            { label: "Request id", value: <code>{a.id}</code> },
          ]}
        />
      </CardContent>
    </Card>
  );
}

/** A decision of the gateway on this action: effect, outcome, the policies that decided. */
function DecisionCard({ title, decision: d }: { title: string; decision: Decision }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
        <span className="text-xs text-slate-500" title={d.created_at}>
          {formatDateTime(d.created_at)}
        </span>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        <div className="flex flex-wrap items-center gap-2">
          {d.effect ? <EffectBadge effect={d.effect} /> : null}
          <OutcomeBadge outcome={d.outcome} />
          {d.fail_mode_applied ? <span className="text-xs text-amber-900">the fail mode applied</span> : null}
        </div>
        <KeyValue
          className="text-xs"
          items={[
            { label: "Message", value: d.message },
            {
              label: "Tool answered",
              value: d.upstream_status === null ? null : `HTTP ${d.upstream_status}`,
            },
            { label: "Error", value: d.error_code },
            { label: "Latency", value: d.latency_ms === null ? null : formatDuration(d.latency_ms) },
            { label: "Idempotency key", value: d.idempotency_key ? <code>{d.idempotency_key}</code> : null },
            { label: "Decision id", value: <code>{d.id}</code> },
          ]}
        />
        {d.decisions.length > 0 ? (
          <ul className="space-y-1 text-xs" aria-label="Policies that decided">
            {d.decisions.map((p) => (
              <li key={`${p.policy}-${p.version}`} className="flex flex-wrap items-center gap-2">
                <EffectBadge effect={p.effect} />
                <span>
                  {p.policy} v{p.version}
                  {p.rule ? (
                    <>
                      {" "}
                      · <code>{p.rule}</code>
                    </>
                  ) : (
                    " · default"
                  )}
                </span>
                <span className="text-slate-500">{p.message}</span>
              </li>
            ))}
          </ul>
        ) : null}
      </CardContent>
    </Card>
  );
}

/** Every time a token was presented for this approval, and how it differed. */
function Attempts({ detail }: { detail: ApprovalDetailData }) {
  const me = useMe();
  const now = new Date();
  return (
    <Card>
      <CardHeader>
        <CardTitle>Uses of the approval</CardTitle>
        <span className="text-xs text-slate-500">A changed request cannot reuse it.</span>
      </CardHeader>
      {detail.attempts.length === 0 ? (
        <EmptyState title="Not used yet">
          Once approved, the agent claims a single-use token and repeats this exact action with it.
        </EmptyState>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[48rem] text-left text-sm">
            <caption className="sr-only">Uses of the approval, oldest first</caption>
            <thead className="border-b border-slate-100 text-xs uppercase tracking-wide text-slate-500">
              <tr>
                {["When", "Result", "How it differed", "By", "Trace"].map((c) => (
                  <th key={c} scope="col" className="px-3 py-2 font-medium">
                    {c}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {detail.attempts.map((t) => (
                <tr key={t.id} className="align-top" data-testid="attempt-row" data-result={t.result}>
                  <td className="px-3 py-2 text-slate-700" title={t.created_at}>
                    {formatRelative(t.created_at, now)}
                  </td>
                  <td className="px-3 py-2">
                    <AttemptBadge result={t.result} />
                  </td>
                  <td className="px-3 py-2">
                    {t.changes.length === 0 ? (
                      <span className="text-slate-500">the same action</span>
                    ) : (
                      <ul className="space-y-0.5 font-mono text-xs text-rose-800" aria-label="Changes">
                        {t.changes.map((c) => (
                          <li key={c.path}>{changeText(c)}</li>
                        ))}
                      </ul>
                    )}
                  </td>
                  <td className="px-3 py-2 text-slate-700">{actorLabel(t.subject, me?.user?.id)}</td>
                  <td className="px-3 py-2">
                    {t.trace_id ? (
                      <Link
                        href={`/traces/${t.trace_id}`}
                        className="font-mono text-xs text-indigo-700 hover:underline"
                      >
                        {t.trace_id.slice(0, 12)}
                      </Link>
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
  );
}

export function ApprovalDetail({ approvalId }: { approvalId: string }) {
  const canApprove = useCan("approval.decide");
  // The project comes from the URL (the inbox links with it), else the first.
  const { projectId, projects } = useProject();
  const detail = useQuery({
    queryKey: ["approval", approvalId, projectId],
    queryFn: ({ signal }) =>
      api<ApprovalDetailData>(
        withQuery(`/approvals/${approvalId}`, queryOf<"getApproval">({ project_id: projectId })),
        { signal },
      ),
    enabled: Boolean(projectId),
    refetchInterval: (q) =>
      q.state.data?.status === "PENDING" || q.state.data?.status === "APPROVED" ? 10_000 : false,
  });
  const tool = detail.data?.tool;
  const policies = useQuery({
    queryKey: ["policies", projectId, tool],
    queryFn: ({ signal }) =>
      api<PolicyPage>(withQuery("/policies", queryOf<"listPolicies">({ project_id: projectId, tool })), {
        signal,
      }),
    enabled: Boolean(projectId && tool),
    staleTime: 60_000,
  });

  if (projects.isError) return <ErrorState error={projects.error} />;
  if (!projectId || detail.isPending) {
    return (
      <div className="space-y-4" aria-busy="true" aria-label="Loading the approval request">
        <Skeleton className="h-10 w-2/3" />
        <Skeleton className="h-40 w-full" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }
  if (detail.isError) {
    return (
      <div className="space-y-3">
        <Link href="/approvals" className="text-sm text-indigo-700 hover:underline">
          ← Approvals
        </Link>
        <ErrorState error={detail.error} />
      </div>
    );
  }
  const a = detail.data;
  const now = new Date();
  const open = canDecide(a, now);
  const policyId = policies.data?.items.find((p) => p.name === a.policy)?.id;

  return (
    <div className="space-y-4">
      <PageHeader
        eyebrow={
          <Link href={`/approvals?project_id=${a.project_id}`} className="text-indigo-700 hover:underline">
            Approvals
          </Link>
        }
        title={
          <span className="flex flex-wrap items-center gap-2" data-testid="approval-header">
            {a.summary}
            <ApprovalStatusBadge status={a.status} />
          </span>
        }
        description={approvalStatusMeaning(a.status)}
      />
      {open && canApprove ? <DecideForm approval={a} /> : null}
      {a.status === "PENDING" && !open ? (
        <div
          role="note"
          className="rounded-md border border-slate-200 bg-slate-50 p-3 text-sm text-slate-800"
        >
          This request expired {expiry(a.expires_at, now).text.replace("expired ", "")}; it can no longer be
          approved. The agent has to ask again.
        </div>
      ) : null}
      {open && !canApprove ? (
        <div
          role="note"
          className="rounded-md border border-slate-200 bg-slate-50 p-3 text-sm text-slate-800"
        >
          Your role cannot decide approval requests.
        </div>
      ) : null}
      <div className="grid gap-4 xl:grid-cols-2">
        <ExactAction approval={a} />
        <Why approval={a} policyId={policyId} />
      </div>
      <Attempts detail={a} />
      <div className="grid gap-4 xl:grid-cols-2">
        {a.decision ? <DecisionCard title="The decision that asked" decision={a.decision} /> : null}
        {a.used_decision ? <DecisionCard title="The approved run" decision={a.used_decision} /> : null}
      </div>
      <p className="text-xs text-slate-500">
        Request {shortId(a.id)} · decision {shortId(a.decision_id)}
      </p>
    </div>
  );
}
