import { describe, expect, it } from "vitest";

import { Redactor, parsePath, redactorFor, truncate, type Strategy } from "../src/redaction.js";
import { loadFixture, rng } from "./helpers.js";

interface RedactionCase {
  name: string;
  mode: "all" | "secrets";
  strategy?: Strategy;
  input: string;
  output: string;
}

const FIXTURE = loadFixture("redaction.json") as { cases: RedactionCase[] };

describe("redaction", () => {
  it.each(FIXTURE.cases.map((c) => [c.name, c] as const))("matches the shared fixture: %s", (_, c) => {
    const strategy = c.strategy ?? "mask";
    const red = c.mode === "all" ? Redactor.all(strategy) : Redactor.secrets(strategy);
    expect(red.text(c.input).text).toBe(c.output);
  });

  it("the fixture exercises every case (none silently lost)", () => {
    expect(FIXTURE.cases.length).toBeGreaterThanOrEqual(18);
  });

  it("is idempotent: redacting redacted text changes nothing", () => {
    const next = rng(7);
    for (let i = 0; i < 1500; i++) {
      const s = randomText(next);
      for (const strategy of ["mask", "hash"] as const) {
        const red = redactorFor({ strategy, pii: next() < 0.5 });
        const once = red.text(s).text;
        expect(once).not.toBeNull();
        const twice = red.text(once!);
        expect(twice).toEqual({ text: once, changed: false });
      }
    }
  });

  it("drop strategy drops the whole value", () => {
    const red = redactorFor({ strategy: "drop" });
    expect(red.text("mail jane@example.com")).toEqual({ text: null, changed: true });
    expect(red.text("nothing to see")).toEqual({ text: "nothing to see", changed: false });
  });

  it("applies JSON paths, then every string leaf, without mutating the input", () => {
    const value = {
      customer: { email: "a@b.co", name: "Jane", note: "call +44 20 7946 0958" },
      items: [
        { card: "4111111111111111", sku: "A" },
        { card: "x", sku: "B" },
      ],
      email_key_is_not_content: 1,
    };
    const { value: out, changed } = redactorFor({ jsonPaths: ["$.customer.name", "$.items[*].card"] }).value(
      value,
    );
    expect(changed).toBe(true);
    expect(out).toEqual({
      customer: { email: "[REDACTED:email]", name: "[REDACTED:field]", note: "call [REDACTED:phone]" },
      items: [
        { card: "[REDACTED:field]", sku: "A" },
        { card: "[REDACTED:field]", sku: "B" },
      ],
      email_key_is_not_content: 1,
    });
    expect(value.customer.name).toBe("Jane");

    const dropped = redactorFor({ strategy: "drop", jsonPaths: ["$.customer"] }).value(value).value;
    expect(dropped).not.toHaveProperty("customer");

    const hash = () =>
      redactorFor({ strategy: "hash", jsonPaths: ["$.customer.name"] }).value(value).value as typeof value;
    expect(hash().customer.name).toMatch(/^\[HASH:field:[0-9a-f]{12}\]$/);
    expect(hash().customer.name).toBe(hash().customer.name);
  });

  it("drop removes sensitive leaves from objects and arrays", () => {
    const { value } = redactorFor({ strategy: "drop" }).value({
      a: "x@y.io",
      b: ["ok", "call (415) 555-2671"],
      c: null,
    });
    expect(value).toEqual({ b: ["ok"], c: null });
  });

  it("json paths: indexes, quoted keys, wildcards on objects, missing fields", () => {
    const red = redactorFor({
      jsonPaths: ["$.list[1]", "$['a b']", "$.m[*]", "$.missing.deep", "$.list[9]"],
    });
    expect(red.value({ list: ["p", "q", "r"], "a b": 1, m: { x: 1, y: [2] } }).value).toEqual({
      list: ["p", "[REDACTED:field]", "r"],
      "a b": "[REDACTED:field]",
      m: { x: "[REDACTED:field]", y: "[REDACTED:field]" },
    });
    expect(redactorFor({ strategy: "drop", jsonPaths: ["$.l[*]"] }).value({ l: [1, 2] }).value).toEqual({
      l: [],
    });
  });

  it.each(["customer.email", "$", "$.a[b]", "$..a", "$.a.*"])("rejects the unsupported path %s", (path) => {
    expect(() => parsePath(path)).toThrow(SyntaxError);
    expect(() => redactorFor({ jsonPaths: [path] })).toThrow(SyntaxError);
  });

  it("custom patterns are reported as custom", () => {
    expect(redactorFor({ customPatterns: ["ACME-\\d{6}"] }).text("ticket ACME-123456 opened").text).toBe(
      "ticket [REDACTED:custom] opened",
    );
    expect(() => redactorFor({ customPatterns: ["("] })).toThrow(SyntaxError);
  });

  it("rules given without the global flag still match every occurrence", () => {
    const red = new Redactor([{ kind: "custom", pattern: /x+/ }]);
    expect(red.text("x ab xx").text).toBe("[REDACTED:custom] ab [REDACTED:custom]");
  });

  it("truncates on a character boundary and keeps valid UTF-8", () => {
    const next = rng(11);
    for (let i = 0; i < 1000; i++) {
      const s = randomText(next) + "şé😀".repeat(Math.floor(next() * 5));
      const limit = Math.floor(next() * 100);
      const { text, truncated } = truncate(s, limit);
      if (truncated) {
        expect(text.endsWith("…[truncated]")).toBe(true);
        expect(Buffer.byteLength(text)).toBeLessThanOrEqual(
          Math.max(limit, Buffer.byteLength("…[truncated]")),
        );
        expect(text).not.toContain("�");
      } else {
        expect(text).toBe(s);
      }
    }
    expect(truncate("héllo", 0)).toEqual({ text: "héllo", truncated: false });
  });
});

const FRAGMENTS = [
  "jane.doe@example.com",
  "password=",
  "secret: ",
  "Bearer ",
  "eyJhbGciOiJIUzI1NiJ9.",
  "atk_ab12cd34_",
  "sk-ant-api03-",
  "4111 1111 1111 1111",
  "+44 20 7946 0958",
  "(415) 555-2671",
  "[REDACTED:email]",
  "[HASH:card:0123456789ab]",
  "ORD-1001",
  "150.00 USD",
  " ",
  "\n",
  "-----BEGIN RSA PRIVATE KEY-----",
  "-----END RSA PRIVATE KEY-----",
];
const ALPHABET = "abcXYZ0123456789 \t\n.,:;=@+-_/'\"()[]{}şé";

export function randomText(next: () => number): string {
  let s = "";
  for (let i = Math.floor(next() * 12); i > 0; i--) {
    if (next() < 0.55) s += FRAGMENTS[Math.floor(next() * FRAGMENTS.length)];
    else
      for (let j = Math.floor(next() * 12); j > 0; j--) s += ALPHABET[Math.floor(next() * ALPHABET.length)];
  }
  return s;
}
