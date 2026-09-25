/** Display formatting. Every function is total: bad input yields "—". */

const DASH = "—";

export function formatDuration(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return DASH;
  if (ms < 1) return `${ms.toFixed(2)} ms`;
  if (ms < 1000) return `${ms < 10 ? ms.toFixed(1) : Math.round(ms)} ms`;
  const s = ms / 1000;
  if (s < 60) return `${s < 10 ? s.toFixed(2) : s.toFixed(1)} s`;
  const m = Math.floor(s / 60);
  const rest = Math.round(s - m * 60);
  return rest === 60 ? `${m + 1}m 0s` : `${m}m ${rest}s`;
}

export function formatCost(usd: number | null | undefined, known = true): string {
  if (!known || usd == null || !Number.isFinite(usd)) return DASH;
  if (usd === 0) return "$0";
  if (usd < 0.01) return `$${usd.toFixed(4)}`;
  return `$${usd.toFixed(2)}`;
}

export function formatNumber(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return DASH;
  return new Intl.NumberFormat("en-US").format(n);
}

export function formatPercent(ratio: number | null | undefined, digits = 1): string {
  if (ratio == null || !Number.isFinite(ratio)) return DASH;
  return `${(ratio * 100).toFixed(digits)}%`;
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return DASH;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return DASH;
  return d
    .toISOString()
    .replace("T", " ")
    .replace(/\.\d+Z$/, " UTC")
    .replace(/Z$/, " UTC");
}

export function formatRelative(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return DASH;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return DASH;
  const diff = Math.round((now.getTime() - d.getTime()) / 1000);
  if (diff < 0) return "just now";
  if (diff < 45) return `${diff}s ago`;
  if (diff < 3600) return `${Math.round(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.round(diff / 3600)}h ago`;
  return `${Math.round(diff / 86400)}d ago`;
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/**
 * A short, recognisable form of an identifier or hash. A hash keeps its first
 * n characters, as git does. A UUID keeps its last n: a UUIDv7 starts with
 * its creation time, so ids made within minutes of each other (a user, an API
 * key, the runs they started) would all read alike; its end is random.
 */
export function shortId(id: string | null | undefined, n = 8): string {
  if (!id) return DASH;
  if (UUID.test(id)) return id.replace(/-/g, "").slice(-n);
  return id.length <= n ? id : id.slice(0, n);
}

/** Human label for snake_case/UPPER_CASE machine values. */
export function humanize(value: string | null | undefined): string {
  if (!value) return DASH;
  const s = value
    .replace(/[_.-]+/g, " ")
    .trim()
    .toLowerCase();
  return s.charAt(0).toUpperCase() + s.slice(1);
}

/** "1 scenario", "9 scenarios": a count with its noun (regular plurals only). */
export function countOf(n: number, noun: string): string {
  return `${formatNumber(n)} ${n === 1 ? noun : `${noun}s`}`;
}
