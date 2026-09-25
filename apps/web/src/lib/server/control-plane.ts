import { cookies } from "next/headers";
import { SESSION_COOKIE } from "@/lib/bff";
import type { ApiErrorBody } from "@/lib/types";

/** Base URL of the control plane as seen from the web server (never the browser). */
export function controlPlaneURL(): string {
  return (process.env.CONTROL_PLANE_URL ?? "http://localhost:8080").replace(/\/+$/, "");
}

export type ServerResult<T> =
  { ok: true; data: T } | { ok: false; status: number; code: string; message: string };

/** Server-side call to the control plane, optionally with the caller's session. */
export async function serverFetch<T>(
  path: string,
  opts: { auth?: boolean; timeoutMs?: number } = {},
): Promise<ServerResult<T>> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (opts.auth ?? true) {
    const token = (await cookies()).get(SESSION_COOKIE)?.value;
    if (!token) return { ok: false, status: 401, code: "UNAUTHENTICATED", message: "Sign in to continue." };
    headers.Authorization = `Bearer ${token}`;
  }
  let res: Response;
  try {
    res = await fetch(controlPlaneURL() + path, {
      headers,
      cache: "no-store",
      signal: AbortSignal.timeout(opts.timeoutMs ?? 10_000),
    });
  } catch {
    return {
      ok: false,
      status: 502,
      code: "CONTROL_PLANE_UNAVAILABLE",
      message: "The AgentTwin control plane is not reachable.",
    };
  }
  const payload: unknown = await res.json().catch(() => undefined);
  if (!res.ok) {
    const e = (payload as ApiErrorBody | undefined)?.error;
    return {
      ok: false,
      status: res.status,
      code: e?.code ?? `HTTP_${res.status}`,
      message: e?.message ?? `The control plane answered ${res.status}.`,
    };
  }
  return { ok: true, data: payload as T };
}
