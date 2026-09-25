"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Crosshair, Plus, Trash2 } from "lucide-react";
import { type FormEvent, useState } from "react";
import { useCan } from "@/components/shell/me-context";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Label, Select } from "@/components/ui/input";
import { KeyValue } from "@/components/ui/key-value";
import { Skeleton } from "@/components/ui/skeleton";
import { ErrorState } from "@/components/ui/states";
import { api } from "@/lib/api";
import type { ComponentDetail, Relation } from "@/lib/api/graph";
import { detailText, kindLabel, relationPhrase } from "@/lib/changes";
import { formatDateTime, formatRelative, humanize } from "@/lib/format";
import {
  CRITICALITIES,
  DEPENDENCY_KINDS,
  DEPENDENCY_RELATIONS,
  type MappingRow,
  confidencePercent,
  manualMapping,
  mappingBody,
} from "@/lib/graph";
import { useActionKey } from "@/lib/use-action-key";

const SOURCE_LABEL: Record<string, string> = {
  MANIFEST: "manifest",
  OPENAPI: "OpenAPI",
  MCP: "MCP",
  OBSERVED: "observed",
  MANUAL: "manual",
  SCENARIO: "scenario",
  INFERRED: "inferred",
};

function RelationRow({ r, label, onSelect }: { r: Relation; label: string; onSelect: (id: string) => void }) {
  const phrase = relationPhrase(r.type);
  return (
    <li className="space-y-1 py-2" data-testid="relation" data-type={r.type} data-direction={r.direction}>
      <p className="text-sm">
        {r.direction === "in" ? (
          <>
            <button
              type="button"
              className="font-mono text-indigo-700 hover:underline"
              onClick={() => onSelect(r.component_id)}
            >
              {r.label}
            </button>{" "}
            <span className="text-slate-600">{phrase}</span>{" "}
            <span className="font-mono text-slate-900">{label}</span>
          </>
        ) : (
          <>
            <span className="font-mono text-slate-900">{label}</span>{" "}
            <span className="text-slate-600">{phrase}</span>{" "}
            <button
              type="button"
              className="font-mono text-indigo-700 hover:underline"
              onClick={() => onSelect(r.component_id)}
            >
              {r.label}
            </button>
          </>
        )}{" "}
        <span className="text-xs text-slate-500">({kindLabel(r.component.kind).toLowerCase()})</span>
      </p>
      <div className="flex flex-wrap items-center gap-1 text-xs">
        <Badge title="Confidence">{confidencePercent(r.confidence)}</Badge>
        {r.sources.map((s) => (
          <Badge key={s} tone={s === "OBSERVED" ? "success" : s === "INFERRED" ? "warning" : "neutral"}>
            {SOURCE_LABEL[s] ?? humanize(s)}
          </Badge>
        ))}
        {r.certain ? null : <Badge tone="warning">not certain</Badge>}
      </div>
      <details className="text-xs text-slate-600">
        <summary className="cursor-pointer hover:text-slate-900">Evidence ({r.evidence.length})</summary>
        <ul className="mt-1 space-y-1 pl-2">
          {r.evidence.map((e, i) => {
            const detail = detailText(e.detail);
            return (
              <li key={`${e.source}-${e.source_ref}-${i}`}>
                <span className="font-medium">{SOURCE_LABEL[e.source] ?? e.source}</span>{" "}
                <span className="font-mono">{e.source_ref}</span>
                {e.source === "OBSERVED"
                  ? ` · seen ${e.observations} ${e.observations === 1 ? "time" : "times"}`
                  : ""}
                {" · "}
                <span title={e.last_seen_at}>last {formatRelative(e.last_seen_at)}</span>
                {detail ? <span className="block text-slate-500">{detail}</span> : null}
              </li>
            );
          })}
        </ul>
      </details>
    </li>
  );
}

/** A tool's manual mapping (spec §20.5): the systems it depends on, replaced as a whole. */
function MappingEditor({ projectId, detail }: { projectId: string; detail: ComponentDetail }) {
  const qc = useQueryClient();
  const initial = manualMapping(detail.relations);
  const [rows, setRows] = useState<MappingRow[]>(initial);
  const [saved, setSaved] = useState<number | null>(null);
  const saveKey = useActionKey("tool-mapping");
  const tool = detail.component.key;

  const save = useMutation({
    mutationFn: () =>
      api(`/graph/mappings/${encodeURIComponent(tool)}`, {
        method: "PUT",
        idempotencyKey: saveKey.key,
        body: { project_id: projectId, depends_on: mappingBody(rows) },
      }),
    onSuccess: () => {
      setSaved(mappingBody(rows).length);
      void qc.invalidateQueries({ queryKey: ["graph"] });
      void qc.invalidateQueries({ queryKey: ["graph-component"] });
    },
    onSettled: (_d, error) => saveKey.settle(error),
  });

  const update = (i: number, patch: Partial<MappingRow>) => {
    setSaved(null);
    setRows(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  };

  function submit(e: FormEvent) {
    e.preventDefault();
    if (!save.isPending) save.mutate();
  }

  return (
    <form className="space-y-2" onSubmit={submit} aria-label={`Manual mapping of ${tool}`}>
      <p className="text-xs text-slate-600">
        The systems {tool} depends on, as you know them. Saving replaces the manual mapping; what manifests,
        imports and traffic say is kept.
      </p>
      {rows.length === 0 ? <p className="text-xs text-slate-500">No manual mapping.</p> : null}
      {rows.map((r, i) => (
        <fieldset key={i} className="flex flex-wrap items-end gap-2" data-testid="mapping-row">
          <legend className="sr-only">Dependency {i + 1}</legend>
          <div className="w-32">
            <Label htmlFor={`map-kind-${i}`}>Kind</Label>
            <Select
              id={`map-kind-${i}`}
              value={r.kind}
              onChange={(e) => update(i, { kind: e.target.value as MappingRow["kind"] })}
            >
              {DEPENDENCY_KINDS.map((k) => (
                <option key={k} value={k}>
                  {kindLabel(k)}
                </option>
              ))}
            </Select>
          </div>
          <div className="min-w-[8rem] flex-1">
            <Label htmlFor={`map-name-${i}`}>Name</Label>
            <Input
              id={`map-name-${i}`}
              value={r.name}
              maxLength={200}
              onChange={(e) => update(i, { name: e.target.value })}
            />
          </div>
          <div className="w-32">
            <Label htmlFor={`map-relation-${i}`}>Relation</Label>
            <Select
              id={`map-relation-${i}`}
              value={r.relation}
              onChange={(e) => update(i, { relation: e.target.value as MappingRow["relation"] })}
            >
              {DEPENDENCY_RELATIONS.map((t) => (
                <option key={t} value={t}>
                  {relationPhrase(t)}
                </option>
              ))}
            </Select>
          </div>
          <div className="w-28">
            <Label htmlFor={`map-criticality-${i}`}>Criticality</Label>
            <Select
              id={`map-criticality-${i}`}
              value={r.criticality}
              onChange={(e) => update(i, { criticality: e.target.value as MappingRow["criticality"] })}
            >
              <option value="">unchanged</option>
              {CRITICALITIES.map((c) => (
                <option key={c} value={c}>
                  {c.toLowerCase()}
                </option>
              ))}
            </Select>
          </div>
          <Button
            size="icon"
            variant="ghost"
            aria-label={`Remove dependency ${i + 1}`}
            onClick={() => {
              setSaved(null);
              setRows(rows.filter((_, j) => j !== i));
            }}
          >
            <Trash2 className="h-4 w-4" aria-hidden="true" />
          </Button>
        </fieldset>
      ))}
      {save.isError ? <ErrorState error={save.error} /> : null}
      <div className="flex flex-wrap items-center gap-2">
        <Button
          size="sm"
          onClick={() => {
            setSaved(null);
            setRows([...rows, { kind: "SERVICE", name: "", relation: "DEPENDS_ON", criticality: "" }]);
          }}
          disabled={rows.length >= 50}
        >
          <Plus className="h-4 w-4" aria-hidden="true" />
          Add dependency
        </Button>
        <Button type="submit" size="sm" variant="primary" disabled={save.isPending}>
          {save.isPending ? "Saving…" : "Save mapping"}
        </Button>
        {saved !== null ? (
          <span className="text-xs text-emerald-800" role="status">
            {saved === 0
              ? "Manual mapping removed."
              : `Saved: ${saved} ${saved === 1 ? "dependency" : "dependencies"}.`}
          </span>
        ) : null}
      </div>
    </form>
  );
}

export function ComponentPanel({
  componentId,
  projectId,
  isFocus,
  onSelect,
  onFocus,
}: {
  componentId: string;
  projectId: string;
  isFocus: boolean;
  onSelect: (id: string) => void;
  onFocus: (id: string) => void;
}) {
  const canMap = useCan("graph.write");
  const detail = useQuery({
    queryKey: ["graph-component", componentId],
    queryFn: ({ signal }) => api<ComponentDetail>(`/graph/components/${componentId}`, { signal }),
  });

  if (detail.isPending) {
    return (
      <Card aria-busy="true" aria-label="Loading the component">
        <CardContent className="space-y-2">
          <Skeleton className="h-6 w-2/3" />
          <Skeleton className="h-24 w-full" />
        </CardContent>
      </Card>
    );
  }
  if (detail.isError) {
    return (
      <Card>
        <CardContent>
          <ErrorState error={detail.error} />
        </CardContent>
      </Card>
    );
  }
  const d = detail.data;
  const c = d.component;
  const attributes = Object.entries(c.attributes ?? {})
    .map(([k, v]) => ({ label: humanize(k), value: detailText(v) }))
    .filter((a) => a.value);
  const outgoing = d.relations.filter((r) => r.direction === "out");
  const incoming = d.relations.filter((r) => r.direction === "in");

  return (
    <Card data-testid="component-panel" data-kind={c.kind} data-key={c.key}>
      <CardHeader>
        <CardTitle className="flex min-w-0 flex-wrap items-center gap-2">
          <Badge>{kindLabel(c.kind)}</Badge>
          <span className="truncate font-mono">{c.label}</span>
        </CardTitle>
        {isFocus ? (
          <Badge tone="brand">focus</Badge>
        ) : (
          <Button size="sm" onClick={() => onFocus(c.id)}>
            <Crosshair className="h-4 w-4" aria-hidden="true" />
            Center here
          </Button>
        )}
      </CardHeader>
      <CardContent className="space-y-4">
        <KeyValue
          className="text-xs"
          items={[
            ...(c.key !== c.label
              ? [{ label: "Key", value: <span className="font-mono">{c.key}</span> }]
              : []),
            ...attributes,
            { label: "First seen", value: formatDateTime(c.first_seen_at) },
            { label: "Last seen", value: formatDateTime(c.last_seen_at) },
          ]}
        />
        <section aria-label="Acts on">
          <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-500">
            Acts on ({outgoing.length})
          </h3>
          {outgoing.length ? (
            <ul className="divide-y divide-slate-100">
              {outgoing.map((r) => (
                <RelationRow key={r.id} r={r} label={c.label} onSelect={onSelect} />
              ))}
            </ul>
          ) : (
            <p className="py-1 text-xs text-slate-500">Nothing the graph knows.</p>
          )}
        </section>
        <section aria-label="Used by">
          <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-500">
            Used by ({incoming.length})
          </h3>
          {incoming.length ? (
            <ul className="divide-y divide-slate-100">
              {incoming.map((r) => (
                <RelationRow key={r.id} r={r} label={c.label} onSelect={onSelect} />
              ))}
            </ul>
          ) : (
            <p className="py-1 text-xs text-slate-500">Nothing the graph knows.</p>
          )}
        </section>
        {d.truncated ? (
          <p className="text-xs text-amber-800">
            More relationships exist than are shown (200 per direction).
          </p>
        ) : null}
        {c.kind === "TOOL" && canMap ? (
          <section aria-label="Manual mapping" className="border-t border-slate-100 pt-3">
            <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">
              Manual mapping
            </h3>
            <MappingEditor key={c.id} projectId={projectId} detail={d} />
          </section>
        ) : null}
      </CardContent>
    </Card>
  );
}
