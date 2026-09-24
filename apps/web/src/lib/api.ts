/**
 * Browser-side API client. Every call goes to the same-origin BFF
 * (/api/v1/*), which attaches the session from an HttpOnly cookie.
 */
import type { ApiErrorBody } from "./types";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly requestId?: string,
    readonly details?: Record<string, unknown>,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export interface RequestOptions {
  method?: string;
  body?: unknown;
  signal?: AbortSignal;
  idempotencyKey?: string;
}

/** Redirects to the login page when the session expired. Overridable in tests. */
export let onUnauthorized = (): void => {
  if (typeof window === "undefined") return;
  const next = window.location.pathname + window.location.search;
  // A full navigation on purpose: the route handler clears the cookie and
  // redirects to the login page, which also resets all client state.
  // eslint-disable-next-line @next/next/no-location-assign-relative-destination
  window.location.assign(`/api/auth/expired?next=${encodeURIComponent(next)}`);
};

export function setUnauthorizedHandler(fn: () => void): void {
  onUnauthorized = fn;
}

export async function api<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  if (!path.startsWith("/")) throw new Error("api path must start with /");
  const headers: Record<string, string> = { Accept: "application/json" };
  let body: BodyInit | undefined;
  if (opts.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(opts.body);
  }
  if (opts.idempotencyKey) headers["Idempotency-Key"] = opts.idempotencyKey;
  let res: Response;
  try {
    res = await fetch(`/api/v1${path}`, {
      method: opts.method ?? "GET",
      headers,
      body,
      signal: opts.signal,
      credentials: "same-origin",
      cache: "no-store",
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    throw new ApiError(
      0,
      "NETWORK_ERROR",
      "The AgentTwin server could not be reached. Check your connection.",
    );
  }
  const requestId = res.headers.get("x-request-id") ?? undefined;
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  let payload: unknown = undefined;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = undefined;
    }
  }
  if (!res.ok) {
    const e = (payload as ApiErrorBody | undefined)?.error;
    if (res.status === 401) onUnauthorized();
    throw new ApiError(
      res.status,
      e?.code ?? `HTTP_${res.status}`,
      e?.message ?? `Request failed with status ${res.status}.`,
      e?.request_id ?? requestId,
      e?.details,
    );
  }
  return payload as T;
}

/** Appends URLSearchParams to a path. */
export function withQuery(path: string, params: URLSearchParams): string {
  const qs = params.toString();
  return qs ? `${path}?${qs}` : path;
}
