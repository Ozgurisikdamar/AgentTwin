import { type NextRequest, NextResponse } from "next/server";
import { SESSION_COOKIE, safeNextPath } from "@/lib/bff";

export const dynamic = "force-dynamic";

/** Clears a session the control plane no longer accepts and returns to login. */
export async function GET(req: NextRequest): Promise<Response> {
  const url = req.nextUrl.clone();
  const next = safeNextPath(req.nextUrl.searchParams.get("next"));
  url.pathname = "/login";
  url.search = "";
  url.searchParams.set("next", next);
  url.searchParams.set("expired", "1");
  const response = NextResponse.redirect(url, 303);
  response.cookies.delete(SESSION_COOKIE);
  return response;
}
