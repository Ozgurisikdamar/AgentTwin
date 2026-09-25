import { type NextRequest, NextResponse } from "next/server";
import { SESSION_COOKIE, isSameOriginRequest, isSecureRequest } from "@/lib/bff";
import { controlPlaneURL } from "@/lib/server/control-plane";

export const dynamic = "force-dynamic";

function problem(status: number, code: string, message: string): NextResponse {
  return NextResponse.json({ error: { code, message } }, { status });
}

/** Development sign-in: exchanges a seeded demo account for a session cookie. */
export async function POST(req: NextRequest): Promise<Response> {
  if (!isSameOriginRequest("POST", req.headers)) {
    return problem(403, "CROSS_SITE_REQUEST", "Cross-site requests are not allowed.");
  }
  let email: unknown;
  try {
    ({ email } = (await req.json()) as { email?: unknown });
  } catch {
    return problem(400, "INVALID_JSON", "Send a JSON body with an email.");
  }
  if (typeof email !== "string" || email.trim() === "" || email.length > 320) {
    return problem(400, "INVALID_EMAIL", "Enter the email of a demo account.");
  }
  let res: Response;
  try {
    res = await fetch(controlPlaneURL() + "/api/v1/auth/dev/login", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ email: email.trim() }),
      cache: "no-store",
      signal: AbortSignal.timeout(10_000),
    });
  } catch {
    return problem(502, "CONTROL_PLANE_UNAVAILABLE", "The AgentTwin control plane is not reachable.");
  }
  const payload = (await res.json().catch(() => ({}))) as {
    token?: string;
    expires_at?: string;
    user?: unknown;
    role?: string;
    organization_id?: string;
  };
  if (!res.ok) return NextResponse.json(payload, { status: res.status });
  if (!payload.token || !payload.expires_at) {
    return problem(
      502,
      "INVALID_UPSTREAM_RESPONSE",
      "The control plane returned an unexpected login response.",
    );
  }
  const response = NextResponse.json({
    user: payload.user,
    role: payload.role,
    organization_id: payload.organization_id,
    expires_at: payload.expires_at,
  });
  response.cookies.set({
    name: SESSION_COOKIE,
    value: payload.token,
    httpOnly: true,
    sameSite: "lax",
    secure: isSecureRequest(req.url, req.headers),
    path: "/",
    expires: new Date(payload.expires_at),
  });
  return response;
}
