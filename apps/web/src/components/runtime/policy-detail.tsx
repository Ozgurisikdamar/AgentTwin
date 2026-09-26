"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FlaskConical, PauseCircle, PencilLine, PlayCircle, Save } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { PageHeader } from "@/components/common/page-header";
import { useCan, useMe } from "@/components/shell/me-context";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Label } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { ErrorState } from "@/components/ui/states";
import { api, withQuery } from "@/lib/api";
import {
  type BodyOf,
  type Policy,
  type PolicyDetail as PolicyDetailData,
  type PolicyTestReport,
  type PolicyVersionAdded,
  type PolicyVersionDetail,
  queryOf,
} from "@/lib/api/runtime";
import { formatDateTime, formatRelative } from "@/lib/format";
import { policyShape, problemsOf } from "@/lib/runtime";
import { actorLabel } from "@/lib/simulations";
import { useActionKey } from "@/lib/use-action-key";
import { useUrlQuery } from "@/lib/use-url-query";
import {
  DocumentEditor,
  ProblemList,
  RulesTable,
  TestReportView,
  TestsTable,
  ThresholdList,
  VersionSettings,
} from "./policy-sections";
import { useProject } from "./project-picker";

const REASON_MAX = 2000;

/** The version the page shows: the URL's if the policy has it, else the active one, else the latest. */
export function selectedVersion(detail: PolicyDetailData, param: string | null): number {
  const asked = Number(param);
  if (Number.isInteger(asked) && detail.versions.some((v) => v.version === asked)) return asked;
  return detail.active?.version ?? detail.latest_version;
}

function VersionsCard({
  detail,
  selected,
  onSelect,
}: {
  detail: PolicyDetailData;
  selected: number;
  onSelect: (v: number) => void;
}) {
  const me = useMe();
  const now = new Date();
  return (
    <Card>
      <CardHeader>
        <CardTitle>Versions</CardTitle>
        <span className="text-xs text-slate-500">Versions are immutable; one is active at a time.</span>
      </CardHeader>
      <div className="relative overflow-x-auto">
        <table className="w-full min-w-[40rem] text-left text-sm">
          <caption className="sr-only">Versions, newest first</caption>
          <thead className="border-b border-slate-100 text-xs uppercase tracking-wide text-slate-500">
            <tr>
              {["Version", "", "Document hash", "Created", "By"].map((c, i) => (
                <th key={`${c}-${i}`} scope="col" className="px-3 py-2 font-medium">
                  {c || <span className="sr-only">State</span>}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {detail.versions.map((v) => (
              <tr
                key={v.id}
                data-testid="version-row"
                aria-current={v.version === selected ? "true" : undefined}
                className={v.version === selected ? "bg-indigo-50/60" : undefined}
              >
                <td className="px-3 py-2">
                  <button
                    type="button"
                    className="font-medium text-indigo-700 hover:underline"
                    onClick={() => onSelect(v.version)}
                    aria-label={`Show version ${v.version}`}
                  >
                    v{v.version}
                  </button>
                </td>
                <td className="px-3 py-2">{v.active ? <Badge tone="success">Active</Badge> : null}</td>
                <td className="px-3 py-2">
                  <code className="text-xs" title={v.spec_hash}>
                    {v.spec_hash.slice(0, 12)}
                  </code>
                </td>
                <td className="px-3 py-2 text-slate-700" title={v.created_at}>
                  {formatRelative(v.created_at, now)}
                </td>
                <td className="px-3 py-2 text-slate-700">{actorLabel(v.created_by, me?.user?.id)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

/** Run a saved version's tests; activate it, or deactivate the policy. */
function VersionActions({
  policy,
  version,
  projectId,
  onReport,
}: {
  policy: Policy;
  version: number;
  projectId: string;
  onReport: (r: PolicyTestReport | null) => void;
}) {
  const qc = useQueryClient();
  const canTest = useCan("policy.test");
  const canActivate = useCan("policy.activate");
  const [reason, setReason] = useState("");
  const activateKey = useActionKey("policy-activate");
  const deactivateKey = useActionKey("policy-deactivate");
  const isActive = policy.active?.version === version;

  const test = useMutation({
    mutationFn: () =>
      api<PolicyTestReport>("/policies/test", {
        method: "POST",
        body: { project_id: projectId, policy_id: policy.id, version } satisfies BodyOf<"testPolicy">,
      }),
    onMutate: () => onReport(null),
    onSuccess: (r) => onReport(r),
  });
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["policy", policy.id] });
    void qc.invalidateQueries({ queryKey: ["policies"] });
  };
  const activate = useMutation({
    mutationFn: () =>
      api<Policy>(`/policies/${policy.id}/activate`, {
        method: "POST",
        idempotencyKey: activateKey.key,
        body: {
          project_id: projectId,
          version,
          reason: reason.trim() || undefined,
        } satisfies BodyOf<"activatePolicy">,
      }),
    onSuccess: () => {
      setReason("");
      refresh();
    },
    onSettled: (_d, error) => activateKey.settle(error),
  });
  const deactivate = useMutation({
    mutationFn: () =>
      api<Policy>(`/policies/${policy.id}/deactivate`, {
        method: "POST",
        idempotencyKey: deactivateKey.key,
        body: { project_id: projectId, reason: reason.trim() } satisfies BodyOf<"deactivatePolicy">,
      }),
    onSuccess: () => {
      setReason("");
      refresh();
    },
    onSettled: (_d, error) => deactivateKey.settle(error),
  });
  const busy = activate.isPending || deactivate.isPending;
  const failed = activate.error ?? deactivate.error;

  if (!canTest && !canActivate) return null;
  return (
    <Card data-testid="version-actions">
      <CardHeader>
        <CardTitle>Test and activate v{version}</CardTitle>
        <span className="text-xs text-slate-500">
          Activation takes effect from the next call and is audited.
        </span>
      </CardHeader>
      <CardContent className="space-y-3">
        {test.isError ? <ErrorState error={test.error} /> : null}
        {failed ? (
          <>
            <ErrorState error={failed} />
            <ProblemList problems={problemsOf(failed)} title="Why the gateway refused" />
          </>
        ) : null}
        {canActivate ? (
          <div className="max-w-xl">
            <Label htmlFor="policy-reason">
              Reason {isActive ? "(required to deactivate)" : "(optional)"}
            </Label>
            <Input
              id="policy-reason"
              maxLength={REASON_MAX}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder={isActive ? "Why the tool no longer needs this guard" : "Why this version now"}
            />
          </div>
        ) : null}
        <div className="flex flex-wrap gap-2">
          {canTest ? (
            <Button size="sm" onClick={() => test.mutate()} disabled={test.isPending}>
              <FlaskConical className="h-4 w-4" aria-hidden="true" />
              {test.isPending ? "Running tests…" : "Run tests"}
            </Button>
          ) : null}
          {canActivate && !isActive ? (
            <Button size="sm" variant="primary" onClick={() => activate.mutate()} disabled={busy}>
              <PlayCircle className="h-4 w-4" aria-hidden="true" />
              {activate.isPending ? "Activating…" : `Activate v${version}`}
            </Button>
          ) : null}
          {canActivate && isActive ? (
            <Button
              size="sm"
              variant="danger"
              onClick={() => deactivate.mutate()}
              disabled={busy || reason.trim() === ""}
            >
              <PauseCircle className="h-4 w-4" aria-hidden="true" />
              {deactivate.isPending ? "Deactivating…" : "Deactivate the policy"}
            </Button>
          ) : null}
        </div>
      </CardContent>
    </Card>
  );
}

/** Edit a version into the policy's next version: test the draft, then save it. */
function NewVersion({
  policy,
  base,
  projectId,
  onSaved,
}: {
  policy: Policy;
  base: PolicyVersionDetail;
  projectId: string;
  onSaved: (version: number) => void;
}) {
  const qc = useQueryClient();
  const canTest = useCan("policy.test");
  const [open, setOpen] = useState(false);
  const [document, setDocument] = useState(base.document);
  const [report, setReport] = useState<PolicyTestReport | null>(null);
  const [unchanged, setUnchanged] = useState<number | null>(null);
  const saveKey = useActionKey("policy-version");
  const test = useMutation({
    mutationFn: () =>
      api<PolicyTestReport>("/policies/test", {
        method: "POST",
        body: { project_id: projectId, document } satisfies BodyOf<"testPolicy">,
      }),
    onMutate: () => setReport(null),
    onSuccess: setReport,
  });
  const save = useMutation({
    mutationFn: () =>
      api<PolicyVersionAdded>(`/policies/${policy.id}/versions`, {
        method: "POST",
        idempotencyKey: saveKey.key,
        body: { project_id: projectId, document } satisfies BodyOf<"addPolicyVersion">,
      }),
    onMutate: () => setUnchanged(null),
    onSuccess: (r) => {
      void qc.invalidateQueries({ queryKey: ["policy", policy.id] });
      void qc.invalidateQueries({ queryKey: ["policies"] });
      if (!r.created) {
        setUnchanged(r.version.version);
        return;
      }
      setOpen(false);
      onSaved(r.version.version);
    },
    onSettled: (_d, error) => saveKey.settle(error),
  });

  if (!open) {
    return (
      <div>
        <Button
          size="sm"
          onClick={() => {
            setDocument(base.document);
            setReport(null);
            setUnchanged(null);
            setOpen(true);
          }}
        >
          <PencilLine className="h-4 w-4" aria-hidden="true" />
          Edit as a new version
        </Button>
      </div>
    );
  }
  const error = save.error ?? test.error;
  return (
    <Card data-testid="new-version">
      <CardHeader>
        <CardTitle>New version of {policy.name}</CardTitle>
        <span className="text-xs text-slate-500">
          Starts from v{base.version}. Saving stores v{policy.latest_version + 1}; it guards nothing until
          activated.
        </span>
      </CardHeader>
      <CardContent className="space-y-3">
        <DocumentEditor id="policy-document" value={document} onChange={setDocument} />
        {error ? (
          <>
            <ErrorState error={error} />
            <ProblemList problems={problemsOf(error)} title="Problems in the document" />
          </>
        ) : null}
        {unchanged !== null ? (
          <p role="status" className="text-sm text-slate-700">
            It decides exactly what v{unchanged} decides; nothing new was stored.
          </p>
        ) : null}
        <div className="flex flex-wrap gap-2">
          {canTest ? (
            <Button size="sm" onClick={() => test.mutate()} disabled={test.isPending || !document.trim()}>
              <FlaskConical className="h-4 w-4" aria-hidden="true" />
              {test.isPending ? "Testing…" : "Test the draft"}
            </Button>
          ) : null}
          <Button
            size="sm"
            variant="primary"
            onClick={() => save.mutate()}
            disabled={save.isPending || !document.trim()}
          >
            <Save className="h-4 w-4" aria-hidden="true" />
            {save.isPending ? "Saving…" : "Save as a new version"}
          </Button>
          <Button size="sm" variant="ghost" onClick={() => setOpen(false)}>
            Cancel
          </Button>
        </div>
        {report ? <TestReportView report={report} /> : null}
      </CardContent>
    </Card>
  );
}

function VersionView({ version: v }: { version: PolicyVersionDetail }) {
  const shape = policyShape(v.spec);
  const me = useMe();
  return (
    <Card data-testid="version-view">
      <CardHeader>
        <CardTitle>
          <span className="flex items-center gap-2">
            Version {v.version}
            {v.active ? <Badge tone="success">Active</Badge> : <Badge tone="neutral">Not active</Badge>}
          </span>
        </CardTitle>
        <span className="text-xs text-slate-500">
          {formatDateTime(v.created_at)} by {actorLabel(v.created_by, me?.user?.id)}
        </span>
      </CardHeader>
      <CardContent className="space-y-4">
        <VersionSettings shape={shape} />
        <div>
          <p className="mb-1 text-xs font-medium uppercase tracking-wide text-slate-500">
            Rules — the first that matches decides
          </p>
          <RulesTable shape={shape} />
        </div>
        {v.thresholds.length > 0 ? (
          <div>
            <p className="mb-1 text-xs font-medium uppercase tracking-wide text-slate-500">Thresholds</p>
            <ThresholdList thresholds={v.thresholds} />
          </div>
        ) : null}
        <div>
          <p className="mb-1 text-xs font-medium uppercase tracking-wide text-slate-500">Tests</p>
          <TestsTable shape={shape} />
        </div>
        <details className="rounded-md border border-slate-200">
          <summary className="cursor-pointer px-3 py-2 text-sm text-slate-700">The document</summary>
          <pre className="relative overflow-x-auto border-t border-slate-200 bg-slate-50 p-3 text-xs leading-5 text-slate-800">
            {v.document}
          </pre>
          <p className="px-3 pb-2 text-xs text-slate-500">
            Rules can read:{" "}
            {v.variables.map((x) => (
              <code key={x} className="mr-1">
                {x}
              </code>
            ))}
          </p>
        </details>
      </CardContent>
    </Card>
  );
}

export function PolicyDetail({ policyId }: { policyId: string }) {
  const { params, update } = useUrlQuery();
  const canWrite = useCan("policy.write");
  const { projectId, projects } = useProject();
  const [report, setReport] = useState<{ version: number; report: PolicyTestReport } | null>(null);

  const detail = useQuery({
    queryKey: ["policy", policyId, projectId],
    queryFn: ({ signal }) =>
      api<PolicyDetailData>(
        withQuery(`/policies/${policyId}`, queryOf<"getPolicy">({ project_id: projectId })),
        { signal },
      ),
    enabled: Boolean(projectId),
  });
  const selected = detail.data ? selectedVersion(detail.data, params.get("version")) : 0;
  const version = useQuery({
    queryKey: ["policy", policyId, projectId, "version", selected],
    queryFn: ({ signal }) =>
      api<PolicyVersionDetail>(
        withQuery(
          `/policies/${policyId}/versions/${selected}`,
          queryOf<"getPolicyVersion">({ project_id: projectId }),
        ),
        { signal },
      ),
    enabled: Boolean(projectId && selected),
  });

  if (projects.isError) return <ErrorState error={projects.error} />;
  if (!projectId || detail.isPending) {
    return (
      <div className="space-y-4" aria-busy="true" aria-label="Loading the policy">
        <Skeleton className="h-10 w-2/3" />
        <Skeleton className="h-40 w-full" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }
  if (detail.isError) {
    return (
      <div className="space-y-3">
        <Link href="/policies" className="text-sm text-indigo-700 hover:underline">
          ← Policies
        </Link>
        <ErrorState error={detail.error} />
      </div>
    );
  }
  const p = detail.data;
  const select = (v: number) => {
    setReport(null);
    update({ version: String(v) });
  };

  return (
    <div className="space-y-4">
      <PageHeader
        eyebrow={
          <Link href={`/policies?project_id=${projectId}`} className="text-indigo-700 hover:underline">
            Policies
          </Link>
        }
        title={
          <span className="flex flex-wrap items-center gap-2" data-testid="policy-header">
            {p.name}
            {p.active ? (
              <Badge tone="success">v{p.active.version} active</Badge>
            ) : (
              <Badge tone="neutral">Not active</Badge>
            )}
          </span>
        }
        description={
          <span>
            Guards <code>{p.tool}</code>. {p.description}
          </span>
        }
      />
      {!p.active ? (
        <div
          role="note"
          className="rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-950"
        >
          No version is active: this policy decides nothing until one is.
        </div>
      ) : null}
      <VersionsCard detail={p} selected={selected} onSelect={select} />
      {version.isError ? <ErrorState error={version.error} /> : null}
      {version.isPending ? <Skeleton className="h-64 w-full" /> : null}
      {version.data ? (
        <>
          <VersionView version={version.data} />
          <VersionActions
            policy={p}
            version={selected}
            projectId={projectId}
            onReport={(r) => setReport(r ? { version: selected, report: r } : null)}
          />
          {report && report.version === selected ? <TestReportView report={report.report} /> : null}
          {canWrite ? (
            <NewVersion
              key={`${version.data.id}`}
              policy={p}
              base={version.data}
              projectId={projectId}
              onSaved={select}
            />
          ) : null}
        </>
      ) : null}
    </div>
  );
}
