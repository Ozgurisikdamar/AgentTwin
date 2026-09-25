"use client";

import { useQuery } from "@tanstack/react-query";
import { useId, useMemo, useState } from "react";
import { SeverityBadge } from "@/components/simulations/badges";
import { Button } from "@/components/ui/button";
import { Input, Label } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/ui/states";
import { api, withQuery } from "@/lib/api";
import { queryOf } from "@/lib/api/simulation";
import type { Scenario, ScenarioPage } from "@/lib/types";

/** The project's scenarios (not archived), for choosing dataset cases. */
export function useProjectScenarios(projectId: string) {
  return useQuery({
    queryKey: ["scenarios", projectId, ""],
    queryFn: ({ signal }) =>
      api<ScenarioPage>(
        withQuery("/scenarios", queryOf<"listScenarios">({ project_id: projectId, limit: 200 })),
        {
          signal,
        },
      ),
    enabled: Boolean(projectId),
  });
}

/** Scenarios matching a filter on their name, agent or tags. */
export function filterScenarios(scenarios: readonly Scenario[], text: string): Scenario[] {
  const q = text.trim().toLowerCase();
  if (!q) return [...scenarios];
  return scenarios.filter(
    (s) =>
      s.name.includes(q) ||
      (s.agent ?? "").toLowerCase().includes(q) ||
      s.tags.some((t) => t.toLowerCase().includes(q)),
  );
}

/**
 * Checkboxes over the project's scenarios. `exclude` are already in the
 * dataset (shown, but not selectable).
 */
export function ScenarioPicker({
  projectId,
  selected,
  onChange,
  exclude = [],
  legend,
}: {
  projectId: string;
  selected: ReadonlySet<string>;
  onChange: (next: Set<string>) => void;
  exclude?: readonly string[];
  legend: string;
}) {
  const scenarios = useProjectScenarios(projectId);
  const filterId = useId();
  const [text, setText] = useState("");
  const excluded = useMemo(() => new Set(exclude), [exclude]);
  const all = scenarios.data?.items ?? [];
  const shown = filterScenarios(all, text);
  const choosable = shown.filter((s) => !excluded.has(s.name));

  if (scenarios.isPending) return <Skeleton className="m-4 h-24" />;
  if (scenarios.isError) return <ErrorState error={scenarios.error} className="m-4" />;
  if (all.length === 0) return <EmptyState title="This project has no scenarios yet" />;
  return (
    <fieldset className="divide-y divide-slate-100">
      <legend className="sr-only">{legend}</legend>
      <div className="flex flex-wrap items-end gap-2 px-4 py-2">
        <div className="w-64">
          <Label htmlFor={filterId}>Filter</Label>
          <Input
            id={filterId}
            placeholder="name, agent or tag"
            value={text}
            onChange={(e) => setText(e.target.value)}
          />
        </div>
        <Button
          size="sm"
          variant="ghost"
          onClick={() => onChange(new Set([...selected, ...choosable.map((s) => s.name)]))}
        >
          Select shown
        </Button>
        <Button size="sm" variant="ghost" onClick={() => onChange(new Set())}>
          Clear
        </Button>
        <span className="ml-auto text-xs text-slate-500" aria-live="polite">
          {selected.size} selected
        </span>
      </div>
      {shown.length === 0 ? <p className="px-4 py-3 text-sm text-slate-600">No scenario matches.</p> : null}
      {shown.map((s) => {
        const already = excluded.has(s.name);
        return (
          <label
            key={s.id}
            className={`flex items-start gap-3 px-4 py-2 ${already ? "opacity-60" : "cursor-pointer hover:bg-slate-50"}`}
          >
            <input
              type="checkbox"
              className="mt-1 h-4 w-4 accent-indigo-600"
              disabled={already}
              checked={already || selected.has(s.name)}
              onChange={(e) => {
                const next = new Set(selected);
                if (e.target.checked) next.add(s.name);
                else next.delete(s.name);
                onChange(next);
              }}
            />
            <span className="min-w-0 flex-1">
              <span className="flex flex-wrap items-center gap-2">
                <span className="font-medium text-slate-900">{s.name}</span>
                <SeverityBadge severity={s.severity} />
                {s.agent ? <span className="text-xs text-slate-500">{s.agent}</span> : null}
                {already ? <span className="text-xs text-slate-500">already in the dataset</span> : null}
              </span>
              {s.description ? (
                <span className="mt-0.5 block text-xs text-slate-600">{s.description}</span>
              ) : null}
            </span>
          </label>
        );
      })}
    </fieldset>
  );
}
