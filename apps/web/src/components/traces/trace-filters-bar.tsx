"use client";

import { ChevronDown, ChevronUp, X } from "lucide-react";
import { useId, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input, Label, Select } from "@/components/ui/input";
import {
  type FilterKey,
  type TraceFilters,
  activeFilterCount,
  isoToLocalInput,
  localInputToISO,
  withFilter,
} from "@/lib/trace-filters";
import type { Facet, FacetDimension, Project } from "@/lib/types";

type Facets = Partial<Record<FacetDimension, Facet[]>>;

interface SelectFilterProps {
  label: string;
  filterKey: FilterKey;
  filters: TraceFilters;
  onChange: (f: TraceFilters) => void;
  options: { value: string; label: string }[];
  anyLabel?: string;
}

function SelectFilter({ label, filterKey, filters, onChange, options, anyLabel = "Any" }: SelectFilterProps) {
  const id = useId();
  const current = filters[filterKey] ?? "";
  const withCurrent =
    current && !options.some((o) => o.value === current)
      ? [{ value: current, label: current }, ...options]
      : options;
  return (
    <div>
      <Label htmlFor={id}>{label}</Label>
      <Select
        id={id}
        value={current}
        onChange={(e) => onChange(withFilter(filters, filterKey, e.target.value))}
      >
        <option value="">{anyLabel}</option>
        {withCurrent.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </Select>
    </div>
  );
}

/** A text/number filter applied on blur or Enter (not on every keystroke). */
function CommitInput({
  label,
  filterKey,
  filters,
  onChange,
  type = "text",
  placeholder,
  toValue = (v: string) => v,
  fromValue = (v: string | undefined) => v ?? "",
}: {
  label: string;
  filterKey: FilterKey;
  filters: TraceFilters;
  onChange: (f: TraceFilters) => void;
  type?: string;
  placeholder?: string;
  toValue?: (v: string) => string | undefined;
  fromValue?: (v: string | undefined) => string;
}) {
  const id = useId();
  const committed = fromValue(filters[filterKey]);
  const [draft, setDraft] = useState(committed);
  // Reset the draft when the committed value changes elsewhere (e.g. "Clear").
  const [seen, setSeen] = useState(committed);
  if (seen !== committed) {
    setSeen(committed);
    setDraft(committed);
  }
  const commit = () => {
    const next = toValue(draft.trim());
    if ((next ?? "") !== (filters[filterKey] ?? "")) onChange(withFilter(filters, filterKey, next));
  };
  return (
    <div>
      <Label htmlFor={id}>{label}</Label>
      <Input
        id={id}
        type={type}
        inputMode={type === "number" ? "decimal" : undefined}
        min={type === "number" ? 0 : undefined}
        placeholder={placeholder}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") commit();
        }}
      />
    </div>
  );
}

function facetOptions(
  facets: Facets | undefined,
  dim: FacetDimension,
  labelOf: (v: string) => string = (v) => v,
) {
  return (facets?.[dim] ?? []).map((f) => ({ value: f.value, label: `${labelOf(f.value)} (${f.count})` }));
}

const STATUS_OPTIONS = [
  { value: "OK", label: "OK" },
  { value: "ERROR", label: "Error" },
  { value: "UNSET", label: "Unset" },
];
const OUTCOME_OPTIONS = [
  { value: "SUCCESS", label: "Success" },
  { value: "PARTIAL", label: "Partial" },
  { value: "FAILURE", label: "Failure" },
  { value: "UNKNOWN", label: "Unknown" },
];
const BOOL_OPTIONS = [
  { value: "true", label: "Yes" },
  { value: "false", label: "No" },
];

export function TraceFiltersBar({
  filters,
  onChange,
  facets,
  projects,
}: {
  filters: TraceFilters;
  onChange: (f: TraceFilters) => void;
  facets: Facets | undefined;
  projects: Project[];
}) {
  const advancedKeys: FilterKey[] = [
    "model",
    "release",
    "policy_decision",
    "source",
    "from",
    "to",
    "min_duration_ms",
    "max_duration_ms",
    "min_cost_usd",
    "max_cost_usd",
    "human_reviewed",
    "flagged",
  ];
  const [showMore, setShowMore] = useState(() => advancedKeys.some((k) => filters[k] !== undefined));
  const count = activeFilterCount(filters);
  const common = { filters, onChange };
  const underscores = (v: string) => v.replaceAll("_", " ");

  return (
    <section aria-label="Trace filters" className="rounded-lg border border-slate-200 bg-white p-3 shadow-sm">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 xl:grid-cols-8">
        <SelectFilter
          label="Project"
          filterKey="project_id"
          anyLabel="All projects"
          options={projects.map((p) => ({ value: p.id, label: p.name }))}
          {...common}
        />
        <SelectFilter label="Agent" filterKey="agent" options={facetOptions(facets, "agent")} {...common} />
        <SelectFilter
          label="Version"
          filterKey="agent_version"
          options={facetOptions(facets, "agent_version")}
          {...common}
        />
        <SelectFilter
          label="Environment"
          filterKey="environment"
          options={facetOptions(facets, "environment")}
          {...common}
        />
        <SelectFilter label="Status" filterKey="status" options={STATUS_OPTIONS} {...common} />
        <SelectFilter label="Outcome" filterKey="outcome" options={OUTCOME_OPTIONS} {...common} />
        <SelectFilter
          label="Failure signal"
          filterKey="signal"
          options={facetOptions(facets, "signal", underscores)}
          {...common}
        />
        <SelectFilter label="Tool" filterKey="tool" options={facetOptions(facets, "tool")} {...common} />
      </div>
      {showMore ? (
        <div className="mt-3 grid grid-cols-2 gap-3 border-t border-slate-100 pt-3 sm:grid-cols-4 xl:grid-cols-6">
          <SelectFilter label="Model" filterKey="model" options={facetOptions(facets, "model")} {...common} />
          <SelectFilter
            label="Release"
            filterKey="release"
            options={facetOptions(facets, "release")}
            {...common}
          />
          <SelectFilter
            label="Policy decision"
            filterKey="policy_decision"
            options={facetOptions(facets, "policy_decision", underscores)}
            {...common}
          />
          <SelectFilter
            label="Run source"
            filterKey="source"
            options={facetOptions(facets, "source")}
            {...common}
          />
          <CommitInput
            label="From"
            filterKey="from"
            type="datetime-local"
            toValue={localInputToISO}
            fromValue={isoToLocalInput}
            {...common}
          />
          <CommitInput
            label="To"
            filterKey="to"
            type="datetime-local"
            toValue={localInputToISO}
            fromValue={isoToLocalInput}
            {...common}
          />
          <CommitInput label="Min duration (ms)" filterKey="min_duration_ms" type="number" {...common} />
          <CommitInput label="Max duration (ms)" filterKey="max_duration_ms" type="number" {...common} />
          <CommitInput label="Min cost (USD)" filterKey="min_cost_usd" type="number" {...common} />
          <CommitInput label="Max cost (USD)" filterKey="max_cost_usd" type="number" {...common} />
          <SelectFilter
            label="Human reviewed"
            filterKey="human_reviewed"
            options={BOOL_OPTIONS}
            {...common}
          />
          <SelectFilter label="Flagged" filterKey="flagged" options={BOOL_OPTIONS} {...common} />
        </div>
      ) : null}
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <Button size="sm" variant="ghost" onClick={() => setShowMore((v) => !v)} aria-expanded={showMore}>
          {showMore ? (
            <ChevronUp className="h-4 w-4" aria-hidden="true" />
          ) : (
            <ChevronDown className="h-4 w-4" aria-hidden="true" />
          )}
          {showMore ? "Fewer filters" : "More filters"}
        </Button>
        {count > 0 ? (
          <Button size="sm" variant="ghost" onClick={() => onChange({})}>
            <X className="h-4 w-4" aria-hidden="true" />
            Clear {count} {count === 1 ? "filter" : "filters"}
          </Button>
        ) : null}
      </div>
    </section>
  );
}
