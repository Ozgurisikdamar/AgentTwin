/**
 * BFF: forwards /api/v1/* to the control plane with the session from the
 * HttpOnly cookie. The browser never sees the session token.
 */
import { type NextRequest, NextResponse } from "next/server";
import {
  FORWARDED_REQUEST_HEADERS,
  FORWARDED_RESPONSE_HEADERS,
  SESSION_COOKIE,
  isSameOriginRequest,
  upstreamPath,
} from "@/lib/bff";
import { controlPlaneURL } from "@/lib/server/control-plane";

export const dynamic = "force-dynamic";

const MAX_BODY_BYTES = 16 * 1024 * 1024;

function problem(status: number, code: string, message: string): NextResponse {
  return NextResponse.json(
    { error: { code, message } },
    { status, headers: { "Cache-Control": "no-store" } },
  );
}

async function forward(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }): Promise<Response> {
  const { path } = await ctx.params;
  const upstream = upstreamPath(path);
  if (!upstream) return problem(400, "INVALID_PATH", "The request path is not valid.");
  if (!isSameOriginRequest(req.method, req.headers)) {
    return problem(403, "CROSS_SITE_REQUEST", "Cross-site requests are not allowed.");
  }
  const token = req.cookies.get(SESSION_COOKIE)?.value;
  if (!token) return problem(401, "UNAUTHENTICATED", "Sign in to continue.");

  const headers = new Headers();
  for (const name of FORWARDED_REQUEST_HEADERS) {
    const value = req.headers.get(name);
    if (value) headers.set(name, value);
  }
  headers.set("Authorization", `Bearer ${token}`);

  let body: ArrayBuffer | undefined;
  if (req.method !== "GET" && req.method !== "HEAD") {
    if (Number(req.headers.get("content-length") ?? "0") > MAX_BODY_BYTES) {
      return problem(413, "PAYLOAD_TOO_LARGE", "The request body is too large.");
    }
    body = await req.arrayBuffer();
    if (body.byteLength > MAX_BODY_BYTES)
      return problem(413, "PAYLOAD_TOO_LARGE", "The request body is too large.");
  }

  let res: Response;
  try {
    res = await fetch(controlPlaneURL() + upstream + req.nextUrl.search, {
      method: req.method,
      headers,
      body,
      redirect: "manual",
      cache: "no-store",
      signal: req.signal,
    });
  } catch {
    return problem(
      502,
      "CONTROL_PLANE_UNAVAILABLE",
      "The AgentTwin control plane is not reachable. Run `make doctor` to diagnose the local stack.",
    );
  }
  const out = new Headers({ "Cache-Control": "no-store" });
  for (const name of FORWARDED_RESPONSE_HEADERS) {
    const value = res.headers.get(name);
    if (value) out.set(name, value);
  }
  const response = new NextResponse(res.status === 204 ? null : res.body, {
    status: res.status,
    headers: out,
  });
  // An expired or revoked session: drop the cookie so the next page load
  // goes to the login page instead of failing again.
  if (res.status === 401) response.cookies.delete(SESSION_COOKIE);
  return response;
}

export { forward as GET, forward as POST, forward as PUT, forward as PATCH, forward as DELETE };
