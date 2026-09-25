"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Archive, GitCompareArrows, Plus, Trash2 } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { PageHeader } from "@/components/common/page-header";
import { ClassificationBadge, SideStatusBadge } from "@/components/evaluations/badges";
import { useCan, useMe } from "@/components/shell/me-context";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Label, Select } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { ApiError, api, withQuery } from "@/lib/api";
import {
  type BodyOf,
  type DatasetCase,
  type DatasetDetail,
  type DatasetResponse,
  queryOf,
} from "@/lib/api/evaluation";
import { formatDateTime, formatRelative, humanize } from "@/lib/format";
import { actorLabel } from "@/lib/simulations";
import { useActionKey } from "@/lib/use-action-key";
import { useUrlQuery } from "@/lib/use-url-query";
import { ScenarioPicker, useProjectScenarios } from "./scenario-picker";

function LastResult({ c }: { c: DatasetCase }) {
  const r = c.last_result;
  if (!r) return <span className="text-xs text-slate-500">not evaluated yet</span>;
  return (
    <div className="space-y-1">
      <Link
        href={`/evaluations/${r.eval_run_id}/cases/${encodeURIComponent(c.scenario)}`}
        className="inline-flex items-center gap-1 hover:underline"
        aria-label={`Last result of ${c.scenario}: ${humanize(r.classification)}`}
      >
        <ClassificationBadge classification={r.classification} />
      </Link>
      <div className="flex flex-wrap items-center gap-1 text-xs text-slate-600">
        v{r.baseline_version} <SideStatusBadge status={r.baseline_status} /> → v{r.candidate_version}{" "}
        <SideStatusBadge status={r.candidate_status} />
      </div>
    </div>
  );
}

function AddCases({
  detail,
  onDone,
}: {
  detail: DatasetDetail;
  onDone: (next: DatasetDetail | null) => void;
}) {
  const [chosen, setChosen] = useState<Set<string>>(new Set());
  const [note, setNote] = useState("");
  const key = useActionKey("dataset-cases");
  const add = useMutation({
    mutationFn: () => {
      const body: BodyOf<"addDatasetCases"> = {
        cases: [...chosen].sort().map((scenario) => ({ scenario })),
        ...(note.trim() ? { note: note.trim() } : {}),
      };
      return api<DatasetDetail>(`/datasets/${detail.dataset.id}/cases`, {
        method: "POST",
        idempotencyKey: key.key,
        body,
      });
    },
    onSuccess: (res) => onDone(res),
    onSettled: (_data, error) => key.settle(error),
  });
  return (
    <Card className="border-indigo-200" data-testid="add-cases">
      <CardHeader>
        <CardTitle>Add cases</CardTitle>
        <span className="text-xs text-slate-500">makes version {detail.dataset.latest_version + 1}</span>
      </CardHeader>
      <ScenarioPicker
        projectId={detail.dataset.project_id}
        selected={chosen}
        onChange={setChosen}
        exclude={detail.version.cases.map((c) => c.scenario)}
        legend="Scenarios to add"
      />
      <CardContent className="space-y-3 border-t border-slate-100 pt-3">
        <div className="max-w-xl">
          <Label htmlFor="ds-add-note">Note on the new version (optional)</Label>
          <Input id="ds-add-note" value={note} onChange={(e) => setNote(e.target.value)} />
        </div>
        {add.isError ? <ErrorState error={add.error} /> : null}
        <div className="flex gap-2">
          <Button
            variant="primary"
            size="sm"
            disabled={chosen.size === 0 || add.isPending}
            onClick={() => add.mutate()}
          >
            {add.isPending ? "Adding…" : `Add ${chosen.size} ${chosen.size === 1 ? "case" : "cases"}`}
          </Button>
          <Button variant="ghost" size="sm" onClick={() => onDone(null)}>
            Cancel
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

export function DatasetDetailView({ datasetId }: { datasetId: string }) {
  const qc = useQueryClient();
  const me = useMe();
  const canWrite = useCan("scenario.write");
  const canRun = useCan("eval.run");
  const { params, update } = useUrlQuery();
  const requested = Number.parseInt(params.get("version") ?? "", 10);
  const version = Number.isInteger(requested) && requested > 0 ? requested : undefined;
  const [adding, setAdding] = useState(false);
  const [removing, setRemoving] = useState<string | null>(null);
  const [archiving, setArchiving] = useState(false);
  const removeKey = useActionKey("dataset-remove");
  const archiveKey = useActionKey("dataset-archive");

  const detail = useQuery({
    queryKey: ["dataset", datasetId, version ?? ""],
    queryFn: ({ signal }) =>
      api<DatasetDetail>(withQuery(`/datasets/${datasetId}`, queryOf<"getDataset">({ version })), { signal }),
  });
  const scenarios = useProjectScenarios(detail.data?.dataset.project_id ?? "");
  const scenarioIds = new Map((scenarios.data?.items ?? []).map((s) => [s.name, s.id]));

  function changed(next: DatasetDetail) {
    qc.setQueryData(["dataset", datasetId, ""], next);
    void qc.invalidateQueries({ queryKey: ["datasets"] });
    update({ version: "" });
  }

  const remove = useMutation({
    mutationFn: (scenario: string) =>
      api<DatasetDetail>(`/datasets/${datasetId}/cases/${encodeURIComponent(scenario)}`, {
        method: "DELETE",
        idempotencyKey: removeKey.key,
      }),
    onSuccess: (res) => {
      setRemoving(null);
      changed(res);
    },
    onSettled: (_data, error) => removeKey.settle(error),
  });
  const archive = useMutation({
    mutationFn: () =>
      api<DatasetResponse>(`/datasets/${datasetId}/archive`, {
        method: "POST",
        idempotencyKey: archiveKey.key,
      }),
    onSuccess: () => {
      setArchiving(false);
      void qc.invalidateQueries({ queryKey: ["dataset", datasetId] });
      void qc.invalidateQueries({ queryKey: ["datasets"] });
    },
    onSettled: (_data, error) => archiveKey.settle(error),
  });

  if (detail.isPending) {
    return (
      <div className="space-y-4" aria-busy="true" aria-label="Loading dataset">
        <Skeleton className="h-10 w-80" />
        <Skeleton className="h-72 w-full" />
      </div>
    );
  }
  if (detail.isError) {
    const e = detail.error;
    if (e instanceof ApiError && e.status === 404) {
      return (
        <EmptyState title={version ? "No such version of this dataset" : "Dataset not found"}>
          {version ? (
            <button
              type="button"
              className="text-indigo-700 underline"
              onClick={() => update({ version: "" })}
            >
              Show the latest version
            </button>
          ) : (
            <Link href="/datasets" className="text-indigo-700 underline">
              Back to datasets
            </Link>
          )}
        </EmptyState>
      );
    }
    return <ErrorState error={e} />;
  }

  const { dataset: ds, version: v, versions } = detail.data;
  const latest = v.version === ds.latest_version;
  const editable = canWrite && !ds.archived && latest;

  return (
    <div className="space-y-4">
      <PageHeader
        eyebrow={
          <Link href="/datasets" className="text-indigo-700 hover:underline">
            Datasets
          </Link>
        }
        title={
          <span className="inline-flex flex-wrap items-center gap-2">
            {ds.name}
            <Badge tone="brand" data-testid="dataset-version">
              v{v.version}
            </Badge>
            {!latest ? <Badge tone="warning">not the latest (v{ds.latest_version})</Badge> : null}
            {ds.archived ? <Badge tone="neutral">archived</Badge> : null}
          </span>
        }
        description={
          <span>
            {ds.description ?? "No description."}{" "}
            <span className="text-slate-500">
              · {v.case_count} {v.case_count === 1 ? "case" : "cases"}
            </span>
          </span>
        }
        actions={
          <>
            {canRun && !ds.archived && v.case_count > 0 ? (
              <Link
                href={`/evaluations/new?project_id=${ds.project_id}&dataset_id=${ds.id}`}
                className={buttonVariants({ variant: "primary", size: "sm" })}
              >
                <GitCompareArrows className="h-4 w-4" aria-hidden="true" />
                Evaluate
              </Link>
            ) : null}
            {editable && !adding ? (
              <Button size="sm" onClick={() => setAdding(true)}>
                <Plus className="h-4 w-4" aria-hidden="true" />
                Add cases
              </Button>
            ) : null}
            {canWrite && !ds.archived && !archiving ? (
              <Button size="sm" variant="ghost" onClick={() => setArchiving(true)}>
                <Archive className="h-4 w-4" aria-hidden="true" />
                Archive
              </Button>
            ) : null}
          </>
        }
      />
      {archiving ? (
        <div
          role="alertdialog"
          aria-label="Archive the dataset"
          className="flex flex-wrap items-center gap-3 rounded-md border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900"
        >
          <span>
            Archive {ds.name}? It stays readable with its versions and results, but it can no longer change or
            be evaluated.
          </span>
          <Button size="sm" variant="danger" onClick={() => archive.mutate()} disabled={archive.isPending}>
            {archive.isPending ? "Archiving…" : "Archive"}
          </Button>
          <Button size="sm" variant="ghost" onClick={() => setArchiving(false)}>
            Keep it
          </Button>
        </div>
      ) : null}
      {archive.isError ? <ErrorState error={archive.error} /> : null}
      {remove.isError ? <ErrorState error={remove.error} /> : null}
      {ds.tags.length ? (
        <div className="flex flex-wrap gap-1">
          {ds.tags.map((t) => (
            <Badge key={t}>{t}</Badge>
          ))}
        </div>
      ) : null}

      {adding ? (
        <AddCases
          detail={detail.data}
          onDone={(next) => {
            setAdding(false);
            if (next) changed(next);
          }}
        />
      ) : null}

      <div className="grid gap-4 xl:grid-cols-4">
        <Card className="xl:col-span-3">
          <CardHeader>
            <CardTitle>Cases</CardTitle>
            <span className="text-xs text-slate-500">
              last result: the latest completed evaluation of the dataset
            </span>
          </CardHeader>
          {v.cases.length === 0 ? (
            <EmptyState title="This version has no cases" />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[52rem] text-left text-sm">
                <caption className="sr-only">Cases of version {v.version}</caption>
                <thead className="border-b border-slate-100 text-xs uppercase tracking-wide text-slate-500">
                  <tr>
                    <th scope="col" className="px-3 py-2 font-medium">
                      Scenario
                    </th>
                    <th scope="col" className="px-3 py-2 font-medium">
                      Source
                    </th>
                    <th scope="col" className="px-3 py-2 font-medium">
                      Added
                    </th>
                    <th scope="col" className="px-3 py-2 font-medium">
                      Last result
                    </th>
                    {editable ? (
                      <th scope="col" className="px-3 py-2 font-medium">
                        <span className="sr-only">Actions</span>
                      </th>
                    ) : null}
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {v.cases.map((c) => {
                    const id = scenarioIds.get(c.scenario);
                    return (
                      <tr
                        key={c.scenario}
                        className="align-top"
                        data-testid="dataset-case"
                        data-scenario={c.scenario}
                      >
                        <td className="px-3 py-2.5">
                          {id ? (
                            <Link
                              href={`/scenarios/${id}`}
                              className="font-medium text-indigo-700 hover:underline"
                            >
                              {c.scenario}
                            </Link>
                          ) : (
                            <span className="font-medium text-slate-900">{c.scenario}</span>
                          )}
                          {c.tags.length ? (
                            <div className="mt-1 flex flex-wrap gap-1">
                              {c.tags.map((t) => (
                                <Badge key={t}>{t}</Badge>
                              ))}
                            </div>
                          ) : null}
                          {c.note ? <p className="mt-1 text-xs text-slate-600">{c.note}</p> : null}
                        </td>
                        <td className="px-3 py-2.5 text-xs text-slate-700">
                          {humanize(c.source)}
                          <div className="mt-1">
                            <Badge tone={c.privacy === "redacted" ? "info" : "neutral"}>{c.privacy}</Badge>
                          </div>
                          {c.trace_id ? (
                            <Link
                              href={`/traces/${c.trace_id}`}
                              className="mt-1 block text-indigo-700 hover:underline"
                            >
                              from a trace
                            </Link>
                          ) : null}
                        </td>
                        <td className="px-3 py-2.5 text-xs text-slate-700" title={c.added_at}>
                          {actorLabel(c.added_by, me?.user?.id)}
                          <div className="text-slate-500">{formatRelative(c.added_at)}</div>
                        </td>
                        <td className="px-3 py-2.5">
                          <LastResult c={c} />
                        </td>
                        {editable ? (
                          <td className="px-3 py-2.5 text-right">
                            {removing === c.scenario ? (
                              <span className="inline-flex items-center gap-1">
                                <Button
                                  size="sm"
                                  variant="danger"
                                  className="h-7 px-2 text-xs"
                                  disabled={remove.isPending}
                                  onClick={() => remove.mutate(c.scenario)}
                                >
                                  {remove.isPending ? "Removing…" : "Remove"}
                                </Button>
                                <Button
                                  size="sm"
                                  variant="ghost"
                                  className="h-7 px-2 text-xs"
                                  onClick={() => setRemoving(null)}
                                >
                                  Keep
                                </Button>
                              </span>
                            ) : (
                              <Button
                                size="sm"
                                variant="ghost"
                                className="h-7 px-2 text-xs"
                                aria-label={`Remove ${c.scenario}`}
                                onClick={() => setRemoving(c.scenario)}
                              >
                                <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
                              </Button>
                            )}
                          </td>
                        ) : null}
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Versions</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <div>
              <Label htmlFor="ds-version">Show version</Label>
              <Select
                id="ds-version"
                value={String(v.version)}
                onChange={(e) =>
                  update({ version: Number(e.target.value) === ds.latest_version ? "" : e.target.value })
                }
              >
                {versions.map((x) => (
                  <option key={x.version} value={String(x.version)}>
                    v{x.version}
                  </option>
                ))}
              </Select>
            </div>
            <ol className="space-y-2 text-xs" aria-label="Version history" data-testid="dataset-versions">
              {versions.map((x) => (
                <li key={x.version} className={x.version === v.version ? "font-medium" : undefined}>
                  <span className="text-slate-900">v{x.version}</span>{" "}
                  <span className="text-slate-600">
                    · {x.case_count} {x.case_count === 1 ? "case" : "cases"}
                    {x.note ? ` · ${x.note}` : ""}
                  </span>
                  <div className="text-slate-500" title={x.created_at}>
                    {actorLabel(x.created_by, me?.user?.id)} · {formatDateTime(x.created_at)}
                  </div>
                </li>
              ))}
            </ol>
            <Link
              href={`/evaluations?dataset_id=${ds.id}`}
              className="block text-xs text-indigo-700 hover:underline"
            >
              Evaluations of this dataset
            </Link>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
