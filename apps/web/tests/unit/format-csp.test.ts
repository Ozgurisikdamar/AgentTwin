import { describe, expect, it } from "vitest";
import { contentSecurityPolicy, newNonce } from "@/lib/csp";
import { formatCost, formatDuration, formatPercent, formatRelative, humanize, shortId } from "@/lib/format";

describe("format", () => {
  it("formats durations across magnitudes", () => {
    expect(formatDuration(0.25)).toBe("0.25 ms");
    expect(formatDuration(6.938)).toBe("6.9 ms");
    expect(formatDuration(250)).toBe("250 ms");
    expect(formatDuration(1500)).toBe("1.50 s");
    expect(formatDuration(95_000)).toBe("1m 35s");
    expect(formatDuration(null)).toBe("—");
    expect(formatDuration(Number.NaN)).toBe("—");
    expect(formatDuration(-1)).toBe("—");
  });
  it("formats cost, percent, relative time and ids", () => {
    expect(formatCost(0.0021)).toBe("$0.0021");
    expect(formatCost(1.5)).toBe("$1.50");
    expect(formatCost(0)).toBe("$0");
    expect(formatCost(1, false)).toBe("—");
    expect(formatPercent(0.125)).toBe("12.5%");
    const now = new Date("2026-09-24T12:00:00Z");
    expect(formatRelative("2026-09-24T11:59:30Z", now)).toBe("30s ago");
    expect(formatRelative("2026-09-24T10:00:00Z", now)).toBe("2h ago");
    expect(formatRelative("not a date", now)).toBe("—");
    expect(shortId("0123456789abcdef", 8)).toBe("01234567");
    // A UUID keeps its random end: two UUIDv7s of the same minute differ there.
    expect(shortId("01a0d92e-0943-701b-a499-9776fbc2defb")).toBe("fbc2defb");
    expect(shortId("01a0d92e-ede4-7b9b-bbe9-1b63017a349d")).toBe("017a349d");
    expect(shortId("01A0D92E-EDE4-7B9B-BBE9-1B63017A349D", 12)).toBe("1B63017A349D");
    expect(shortId("01a0d92e-ede4-7b9b-bbe9-1b63017a349d", 40)).toBe("01a0d92eede47b9bbbe91b63017a349d");
    // Not quite a UUID: treated as a hash.
    expect(shortId("01a0d92e-ede4-7b9b-bbe9-1b63017a349", 8)).toBe("01a0d92e");
    expect(shortId("run-01a0d92e-ede4-7b9b-bbe9-1b63017a349d", 8)).toBe("run-01a0");
    expect(shortId("short", 8)).toBe("short");
    expect(shortId(null)).toBe("—");
    expect(humanize("WRITE_IRREVERSIBLE")).toBe("Write irreversible");
    expect(humanize("state_assertion")).toBe("State assertion");
  });
});

describe("content security policy", () => {
  it("allows only nonce'd scripts and never inline script", () => {
    const csp = contentSecurityPolicy("abc", { dev: false, https: true });
    expect(csp).toContain("script-src 'self' 'nonce-abc' 'strict-dynamic'");
    expect(csp).not.toContain("unsafe-eval");
    expect(csp).toContain("frame-ancestors 'none'");
    expect(csp).toContain("upgrade-insecure-requests");
    const scriptSrc = csp.split(";").find((d) => d.trim().startsWith("script-src"));
    expect(scriptSrc).not.toContain("unsafe-inline");
  });
  it("adds eval only in development and upgrades only over https", () => {
    const csp = contentSecurityPolicy("n", { dev: true, https: false });
    expect(csp).toContain("'unsafe-eval'");
    expect(csp).not.toContain("upgrade-insecure-requests");
  });
  it("generates unpredictable nonces", () => {
    const a = newNonce();
    expect(a).toMatch(/^[A-Za-z0-9+/]{22}==$/);
    expect(newNonce()).not.toBe(a);
  });
});
