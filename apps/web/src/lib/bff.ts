/**
 * Pure helpers of the backend-for-frontend (BFF). The browser never talks to
 * the control plane directly: route handlers forward /api/v1/* with the
 * session from an HttpOnly cookie, so tokens are never readable by scripts.
 */

export const SESSION_COOKIE = "agenttwin_session";

const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);
const FORBIDDEN_SEGMENT = /[\u0000-\u001f\u007f/\\?#]/;

/** Maps catch-all route segments to an upstream path; null if unsafe. */
export function upstreamPath(segments: readonly string[]): string | null {
  if (segments.length === 0 || segments.length > 16) return null;
  for (const s of segments) {
    if (!s || s === "." || s === ".." || s.length > 256 || FORBIDDEN_SEGMENT.test(s)) return null;
  }
  return "/api/v1/" + segments.map((s) => encodeURIComponent(s)).join("/");
}

/** State-changing requests must come from this origin (CSRF defense, in
 *  addition to the SameSite=Lax session cookie). */
export function isSameOriginRequest(method: string, headers: Headers): boolean {
  if (SAFE_METHODS.has(method.toUpperCase())) return true;
  const site = headers.get("sec-fetch-site");
  if (site) return site === "same-origin";
  const origin = headers.get("origin");
  const host = headers.get("x-forwarded-host") ?? headers.get("host");
  if (!origin || !host) return false;
  try {
    return new URL(origin).host === host;
  } catch {
    return false;
  }
}

/** Only relative in-app paths are valid post-login destinations. */
export function safeNextPath(next: string | null | undefined, fallback = "/overview"): string {
  if (!next || !next.startsWith("/") || next.startsWith("//") || next.startsWith("/\\")) return fallback;
  if (next.startsWith("/api/") || next.startsWith("/login")) return fallback;
  return next;
}

/** Response headers forwarded from the control plane to the browser. */
export const FORWARDED_RESPONSE_HEADERS = [
  "content-type",
  "content-disposition",
  "retry-after",
  "x-request-id",
  "etag",
  "last-modified",
] as const;

/** Request headers forwarded from the browser to the control plane. */
export const FORWARDED_REQUEST_HEADERS = [
  "accept",
  "content-type",
  "idempotency-key",
  "x-request-id",
] as const;

/** Whether the connection to the browser is TLS (directly or at a proxy). */
export function isSecureRequest(url: string, headers: Headers): boolean {
  const forced = process.env.COOKIE_SECURE;
  if (forced === "true") return true;
  if (forced === "false") return false;
  const proto = headers.get("x-forwarded-proto");
  if (proto) return proto.split(",")[0]?.trim() === "https";
  return url.startsWith("https:");
}
