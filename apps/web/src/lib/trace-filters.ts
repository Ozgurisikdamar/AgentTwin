/**
 * Trace explorer filters live in the URL so every view is shareable. This
 * module converts between URLSearchParams and a typed filter object and
 * builds the API query. Unknown or malformed values are dropped.
 */

export interface TraceFilters {
  project_id?: string;
  agent?: string;
  agent_version?: string;
  environment?: string;
  release?: string;
  model?: string;
  tool?: string;
  status?: "OK" | "ERROR" | "UNSET";
  outcome?: "SUCCESS" | "PARTIAL" | "FAILURE" | "UNKNOWN";
  signal?: string;
  policy_decision?: string;
  source?: string;
  from?: string;
  to?: string;
  min_duration_ms?: string;
  max_duration_ms?: string;
  min_cost_usd?: string;
  max_cost_usd?: string;
  human_reviewed?: "true" | "false";
  flagged?: "true" | "false";
}

export type FilterKey = keyof TraceFilters;

export const FILTER_KEYS: readonly FilterKey[] = [
  "project_id",
  "agent",
  "agent_version",
  "environment",
  "release",
  "model",
  "tool",
  "status",
  "outcome",
  "signal",
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

const ENUMS: Partial<Record<FilterKey, readonly string[]>> = {
  status: ["OK", "ERROR", "UNSET"],
  outcome: ["SUCCESS", "PARTIAL", "FAILURE", "UNKNOWN"],
  human_reviewed: ["true", "false"],
  flagged: ["true", "false"],
};
const NUMERIC: ReadonlySet<FilterKey> = new Set([
  "min_duration_ms",
  "max_duration_ms",
  "min_cost_usd",
  "max_cost_usd",
]);
const TIMES: ReadonlySet<FilterKey> = new Set(["from", "to"]);
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function valid(key: FilterKey, value: string): boolean {
  if (value.length === 0 || value.length > 256) return false;
  const allowed = ENUMS[key];
  if (allowed) return allowed.includes(value);
  if (NUMERIC.has(key)) return /^\d+(\.\d+)?$/.test(value);
  if (TIMES.has(key)) return !Number.isNaN(Date.parse(value));
  if (key === "project_id") return UUID.test(value);
  return true;
}

/** Reads filters from URL search params, ignoring invalid values. */
export function parseFilters(params: URLSearchParams): TraceFilters {
  const out: Record<string, string> = {};
  for (const key of FILTER_KEYS) {
    const raw = params.get(key)?.trim();
    if (raw && valid(key, raw)) out[key] = raw;
  }
  return out as TraceFilters;
}

/** Serializes filters in a stable key order (stable query cache keys). */
export function filtersToParams(filters: TraceFilters): URLSearchParams {
  const params = new URLSearchParams();
  for (const key of FILTER_KEYS) {
    const v = filters[key];
    if (v !== undefined && v !== "" && valid(key, v)) params.set(key, v);
  }
  return params;
}

/** Returns a copy with one filter set (or removed when value is empty). */
export function withFilter(filters: TraceFilters, key: FilterKey, value: string | undefined): TraceFilters {
  const next: Record<string, string> = { ...(filters as Record<string, string>) };
  if (value === undefined || value === "") delete next[key];
  else next[key] = value;
  return next as TraceFilters;
}

export function activeFilterCount(filters: TraceFilters): number {
  return FILTER_KEYS.filter((k) => filters[k] !== undefined && filters[k] !== "").length;
}

/** Converts a datetime-local input value (browser local time) to RFC 3339 UTC. */
export function localInputToISO(value: string): string | undefined {
  if (!value) return undefined;
  const t = new Date(value);
  return Number.isNaN(t.getTime()) ? undefined : t.toISOString();
}

/** Converts RFC 3339 to a datetime-local input value in browser local time. */
export function isoToLocalInput(iso: string | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
