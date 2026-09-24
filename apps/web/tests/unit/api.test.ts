import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api, setUnauthorizedHandler } from "@/lib/api";

function mockFetch(status: number, body: unknown, headers: Record<string, string> = {}) {
  return vi.fn(
    async () =>
      new Response(body === undefined ? null : JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json", ...headers },
      }),
  );
}

describe("api client", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("calls the same-origin BFF and returns JSON", async () => {
    const f = mockFetch(200, { items: [1] });
    vi.stubGlobal("fetch", f);
    await expect(api("/traces?limit=1")).resolves.toEqual({ items: [1] });
    expect(f).toHaveBeenCalledWith(
      "/api/v1/traces?limit=1",
      expect.objectContaining({ credentials: "same-origin" }),
    );
  });

  it("maps API errors to ApiError with code and request id", async () => {
    vi.stubGlobal(
      "fetch",
      mockFetch(409, { error: { code: "VERSION_EXISTS", message: "immutable", request_id: "rid-1" } }),
    );
    const err = (await api("/x").catch((e: unknown) => e)) as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    expect([err.status, err.code, err.message, err.requestId]).toEqual([
      409,
      "VERSION_EXISTS",
      "immutable",
      "rid-1",
    ]);
  });

  it("signals an expired session on 401", async () => {
    const handler = vi.fn();
    setUnauthorizedHandler(handler);
    vi.stubGlobal("fetch", mockFetch(401, { error: { code: "UNAUTHENTICATED", message: "Sign in" } }));
    await expect(api("/me")).rejects.toMatchObject({ status: 401 });
    expect(handler).toHaveBeenCalledOnce();
  });

  it("reports network failures without leaking internals", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => Promise.reject(new TypeError("fetch failed: ECONNREFUSED 10.0.0.1"))),
    );
    const err = (await api("/x").catch((e: unknown) => e)) as ApiError;
    expect(err.code).toBe("NETWORK_ERROR");
    expect(err.message).not.toContain("10.0.0.1");
  });

  it("sends JSON bodies and idempotency keys", async () => {
    const f = mockFetch(201, { ok: true });
    vi.stubGlobal("fetch", f);
    await api("/things", { method: "POST", body: { a: 1 }, idempotencyKey: "k-1" });
    const init = (f.mock.calls[0] as unknown as [string, RequestInit])[1];
    expect(init.method).toBe("POST");
    expect(init.body).toBe('{"a":1}');
    expect((init.headers as Record<string, string>)["Idempotency-Key"]).toBe("k-1");
  });
});
