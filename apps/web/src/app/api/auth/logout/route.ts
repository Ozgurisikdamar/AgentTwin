import { type NextRequest, NextResponse } from "next/server";
import { SESSION_COOKIE, isSameOriginRequest } from "@/lib/bff";

export const dynamic = "force-dynamic";

export async function POST(req: NextRequest): Promise<Response> {
  if (!isSameOriginRequest("POST", req.headers)) {
    return NextResponse.json(
      { error: { code: "CROSS_SITE_REQUEST", message: "Cross-site requests are not allowed." } },
      { status: 403 },
    );
  }
  const response = new NextResponse(null, { status: 204 });
  response.cookies.delete(SESSION_COOKIE);
  return response;
}
