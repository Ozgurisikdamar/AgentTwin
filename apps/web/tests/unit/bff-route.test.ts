// @vitest-environment node
import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { POST as login } from "@/app/api/auth/login/route";
import { GET, POST } from "@/app/api/v1/[...path]/route";

const ctx = (...path: string[]) => ({ params: Promise.resolve({ path }) });

function request(
  url: string,
  init: { method?: string; headers?: Record<string, string>; body?: string } = {},
) {
  return new NextRequest(new URL(url, "http://localhost:3000"), {
    method: init.method ?? "GET",
    headers: init.headers,
    body: init.body,
  });
}

describe("BFF /api/v1 proxy", () => {
  let upstream: ReturnType<typeof vi.fn>;
  beforeEach(() => {
    vi.stubEnv("CONTROL_PLANE_URL", "http://control-plane:8080/");
    upstream = vi.fn(
      async () =>
        new Response(JSON.stringify({ items: [] }), {
          status: 200,
          headers: {
            "Content-Type": "application/json",
            "X-Request-Id": "rid-9",
            "Set-Cookie": "leak=1",
            Server: "go",
          },
        }),
    );
    vi.stubGlobal("fetch", upstream);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("forwards with the session as a bearer token and filters headers", async () => {
    const res = await GET(
      request("/api/v1/traces?limit=5&agent=a", {
        headers: {
          cookie: "agenttwin_session=tok-123; other=x",
          "x-forwarded-for": "1.2.3.4",
          accept: "application/json",
        },
      }),
      ctx("traces"),
    );
    expect(res.status).toBe(200);
    const [url, init] = upstream.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://control-plane:8080/api/v1/traces?limit=5&agent=a");
    const sent = init.headers as Headers;
    expect(sent.get("authorization")).toBe("Bearer tok-123");
    expect(sent.get("cookie")).toBeNull();
    expect(sent.get("x-forwarded-for")).toBeNull();
    expect(res.headers.get("x-request-id")).toBe("rid-9");
    expect(res.headers.get("set-cookie")).toBeNull();
    expect(res.headers.get("server")).toBeNull();
    expect(res.headers.get("cache-control")).toBe("no-store");
  });

  it("rejects requests without a session", async () => {
    const res = await GET(request("/api/v1/traces"), ctx("traces"));
    expect(res.status).toBe(401);
    expect(upstream).not.toHaveBeenCalled();
  });

  it("rejects cross-site writes before contacting the control plane", async () => {
    const res = await POST(
      request("/api/v1/projects", {
        method: "POST",
        headers: {
          cookie: "agenttwin_session=tok",
          "sec-fetch-site": "cross-site",
          "content-type": "application/json",
        },
        body: "{}",
      }),
      ctx("projects"),
    );
    expect(res.status).toBe(403);
    expect(upstream).not.toHaveBeenCalled();
  });

  it("rejects path traversal", async () => {
    const res = await GET(
      request("/api/v1/x", { headers: { cookie: "agenttwin_session=tok" } }),
      ctx("..", "internal"),
    );
    expect(res.status).toBe(400);
    expect(upstream).not.toHaveBeenCalled();
  });

  it("drops the session cookie when the control plane rejects it", async () => {
    upstream.mockResolvedValueOnce(
      new Response(JSON.stringify({ error: { code: "UNAUTHORIZED" } }), {
        status: 401,
        headers: { "Content-Type": "application/json" },
      }),
    );
    const res = await GET(
      request("/api/v1/me", { headers: { cookie: "agenttwin_session=expired" } }),
      ctx("me"),
    );
    expect(res.status).toBe(401);
    expect(res.headers.get("set-cookie")).toMatch(/agenttwin_session=;.*Expires=Thu, 01 Jan 1970/i);
  });

  it("answers 502 without internals when the control plane is down", async () => {
    upstream.mockRejectedValueOnce(new TypeError("connect ECONNREFUSED 172.18.0.5:8080"));
    const res = await GET(request("/api/v1/me", { headers: { cookie: "agenttwin_session=t" } }), ctx("me"));
    expect(res.status).toBe(502);
    const body = await res.text();
    expect(body).toContain("CONTROL_PLANE_UNAVAILABLE");
    expect(body).not.toContain("172.18.0.5");
  });

  it("forwards same-origin writes with their body", async () => {
    await POST(
      request("/api/v1/traces/abc/flag", {
        method: "POST",
        headers: {
          cookie: "agenttwin_session=tok",
          "sec-fetch-site": "same-origin",
          "content-type": "application/json",
          "idempotency-key": "k1",
        },
        body: '{"reason":"x"}',
      }),
      ctx("traces", "abc", "flag"),
    );
    const [, init] = upstream.mock.calls[0] as [string, RequestInit];
    expect(init.method).toBe("POST");
    expect(new TextDecoder().decode(init.body as ArrayBuffer)).toBe('{"reason":"x"}');
    expect((init.headers as Headers).get("idempotency-key")).toBe("k1");
  });
});

describe("login route", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("stores the session in an HttpOnly SameSite cookie and never returns the token", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        Response.json({
          token: "secret-session-token",
          expires_at: "2030-01-01T00:00:00Z",
          user: { id: "u" },
          role: "OWNER",
          organization_id: "o",
        }),
      ),
    );
    const res = await login(
      request("/api/auth/login", {
        method: "POST",
        headers: { "sec-fetch-site": "same-origin", "content-type": "application/json" },
        body: JSON.stringify({ email: "owner@demo.agenttwin.dev" }),
      }),
    );
    expect(res.status).toBe(200);
    const cookie = res.headers.get("set-cookie") ?? "";
    expect(cookie).toContain("agenttwin_session=secret-session-token");
    expect(cookie).toMatch(/HttpOnly/i);
    expect(cookie).toMatch(/SameSite=lax/i);
    expect(cookie).not.toMatch(/Secure/);
    expect(await res.text()).not.toContain("secret-session-token");
  });

  it("marks the cookie Secure behind TLS and rejects cross-site logins", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => Response.json({ token: "t", expires_at: "2030-01-01T00:00:00Z" })),
    );
    const res = await login(
      request("/api/auth/login", {
        method: "POST",
        headers: { "sec-fetch-site": "same-origin", "x-forwarded-proto": "https" },
        body: JSON.stringify({ email: "a@b.c" }),
      }),
    );
    expect(res.headers.get("set-cookie")).toMatch(/Secure/);
    const cross = await login(
      request("/api/auth/login", { method: "POST", headers: { "sec-fetch-site": "cross-site" }, body: "{}" }),
    );
    expect(cross.status).toBe(403);
  });

  it("passes through a rejected login", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        Response.json({ error: { code: "UNKNOWN_DEMO_USER", message: "No demo user" } }, { status: 401 }),
      ),
    );
    const res = await login(
      request("/api/auth/login", {
        method: "POST",
        headers: { "sec-fetch-site": "same-origin" },
        body: JSON.stringify({ email: "nobody@x.y" }),
      }),
    );
    expect(res.status).toBe(401);
    expect(res.headers.get("set-cookie")).toBeNull();
    expect(await res.json()).toMatchObject({ error: { code: "UNKNOWN_DEMO_USER" } });
  });
});
