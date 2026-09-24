import { type NextRequest, NextResponse } from "next/server";
import { SESSION_COOKIE, isSecureRequest } from "@/lib/bff";
import { contentSecurityPolicy, newNonce } from "@/lib/csp";

const PUBLIC_PAGES = ["/login"];

/**
 * Runs before every page request: sends visitors without a session to the
 * login page (an optimistic check — the control plane still authorizes every
 * API call) and sets a fresh nonce-based Content-Security-Policy.
 */
export function proxy(request: NextRequest): NextResponse {
  const { pathname, search } = request.nextUrl;
  const isPublic = PUBLIC_PAGES.some((p) => pathname === p || pathname.startsWith(`${p}/`));
  if (!isPublic && !request.cookies.has(SESSION_COOKIE)) {
    const url = request.nextUrl.clone();
    url.pathname = "/login";
    url.search = "";
    if (pathname !== "/") url.searchParams.set("next", pathname + search);
    return NextResponse.redirect(url);
  }
  const nonce = newNonce();
  const csp = contentSecurityPolicy(nonce, {
    dev: process.env.NODE_ENV === "development",
    https: isSecureRequest(request.url, request.headers),
  });
  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("x-nonce", nonce);
  requestHeaders.set("Content-Security-Policy", csp);
  const response = NextResponse.next({ request: { headers: requestHeaders } });
  response.headers.set("Content-Security-Policy", csp);
  return response;
}

export const config = {
  matcher: [
    {
      source: "/((?!api|_next/static|_next/image|favicon.ico|healthz).*)",
      missing: [
        { type: "header", key: "next-router-prefetch" },
        { type: "header", key: "purpose", value: "prefetch" },
      ],
    },
  ],
};
