import { afterEach, describe, expect, it, vi } from "vitest";
import { isSameOriginRequest, isSecureRequest, safeNextPath, upstreamPath } from "@/lib/bff";

describe("upstreamPath", () => {
  it("maps segments under /api/v1 and encodes them", () => {
    expect(upstreamPath(["traces", "abc"])).toBe("/api/v1/traces/abc");
    expect(upstreamPath(["traces", "a b"])).toBe("/api/v1/traces/a%20b");
  });

  it.each([
    [[]],
    [[".."]],
    [["traces", ".."]],
    [["."]],
    [["a/b"]],
    [["a\\b"]],
    [["a?b"]],
    [["a#b"]],
    [["x\u0000"]],
    [[""]],
  ])("rejects unsafe segments %j", (segments) => {
    expect(upstreamPath(segments)).toBeNull();
  });

  it("rejects very deep or very long paths", () => {
    expect(upstreamPath(Array.from({ length: 17 }, () => "a"))).toBeNull();
    expect(upstreamPath(["a".repeat(257)])).toBeNull();
  });
});

describe("isSameOriginRequest", () => {
  const h = (init: Record<string, string>) => new Headers(init);
  it("allows safe methods from anywhere", () => {
    expect(isSameOriginRequest("GET", h({ "sec-fetch-site": "cross-site" }))).toBe(true);
  });
  it("uses Sec-Fetch-Site for unsafe methods", () => {
    expect(isSameOriginRequest("POST", h({ "sec-fetch-site": "same-origin" }))).toBe(true);
    expect(isSameOriginRequest("POST", h({ "sec-fetch-site": "same-site" }))).toBe(false);
    expect(
      isSameOriginRequest(
        "DELETE",
        h({ "sec-fetch-site": "cross-site", origin: "http://localhost:3000", host: "localhost:3000" }),
      ),
    ).toBe(false);
  });
  it("falls back to comparing Origin with Host", () => {
    expect(isSameOriginRequest("POST", h({ origin: "http://localhost:3000", host: "localhost:3000" }))).toBe(
      true,
    );
    expect(isSameOriginRequest("POST", h({ origin: "http://evil.example", host: "localhost:3000" }))).toBe(
      false,
    );
    expect(isSameOriginRequest("POST", h({ host: "localhost:3000" }))).toBe(false);
    expect(isSameOriginRequest("POST", h({ origin: "null", host: "localhost:3000" }))).toBe(false);
  });
});

describe("safeNextPath", () => {
  it.each([
    [null, "/overview"],
    ["/traces?agent=a", "/traces?agent=a"],
    ["https://evil.example", "/overview"],
    ["//evil.example", "/overview"],
    ["/\\evil.example", "/overview"],
    ["/api/v1/traces", "/overview"],
    ["/login", "/overview"],
  ])("%s -> %s", (input, want) => {
    expect(safeNextPath(input)).toBe(want);
  });
});

describe("isSecureRequest", () => {
  afterEach(() => vi.unstubAllEnvs());
  it("honors the proxy header, the URL and the override", () => {
    expect(isSecureRequest("http://x", new Headers({ "x-forwarded-proto": "https" }))).toBe(true);
    expect(isSecureRequest("https://x", new Headers())).toBe(true);
    expect(isSecureRequest("http://x", new Headers())).toBe(false);
    vi.stubEnv("COOKIE_SECURE", "true");
    expect(isSecureRequest("http://x", new Headers())).toBe(true);
  });
});
