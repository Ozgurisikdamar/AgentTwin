import { describe, expect, it } from "vitest";

import { canonicalJson, compareCodePoints, contentHash, sha256Hex } from "../src/hashing.js";
import { loadFixture, rng } from "./helpers.js";

interface CanonicalCase {
  name: string;
  input: unknown;
  canonical: string;
}

const FIXTURE = loadFixture("canonical-json.json") as { cases: CanonicalCase[] };

describe("canonical JSON", () => {
  it.each(FIXTURE.cases.map((c) => [c.name, c] as const))("matches the shared fixture: %s", (_, c) => {
    expect(canonicalJson(c.input)).toBe(c.canonical);
  });

  it("the fixture exercises every case (none silently lost)", () => {
    expect(FIXTURE.cases.length).toBeGreaterThanOrEqual(12);
  });

  it("hashes are independent of key order", () => {
    expect(contentHash({ b: 1, a: [1, { d: 2, c: 3 }] })).toBe(contentHash({ a: [1, { c: 3, d: 2 }], b: 1 }));
    expect(contentHash({ a: 1 })).toBe(sha256Hex('{"a":1}'));
  });

  it("sha256Hex hashes strings as UTF-8", () => {
    expect(sha256Hex("ş")).toBe(sha256Hex(Buffer.from("ş", "utf8")));
    expect(sha256Hex("")).toBe("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
  });

  it("rejects what JSON cannot represent", () => {
    expect(() => canonicalJson(Number.NaN)).toThrow(RangeError);
    expect(() => canonicalJson({ x: Infinity })).toThrow(RangeError);
    expect(() => canonicalJson(undefined)).toThrow(TypeError);
    expect(() => canonicalJson([1, undefined])).toThrow(TypeError);
    expect(() => canonicalJson({ f: () => 1 })).toThrow(TypeError);
    expect(() => canonicalJson(new Date(0))).toThrow(TypeError);
    expect(() => canonicalJson(new Map())).toThrow(TypeError);
    const cyclic: Record<string, unknown> = {};
    cyclic.self = cyclic;
    expect(() => canonicalJson(cyclic)).toThrow(TypeError);
  });

  it("leaves out undefined properties and accepts shared (non-cyclic) references", () => {
    const shared = { k: 1 };
    expect(canonicalJson({ a: undefined, b: shared, c: shared })).toBe('{"b":{"k":1},"c":{"k":1}}');
    expect(canonicalJson(Object.assign(Object.create(null) as object, { z: 1 }))).toBe('{"z":1}');
  });

  it("bigints are exact within int64 and floats beyond", () => {
    expect(canonicalJson(-(2n ** 63n))).toBe("-9223372036854775808");
    // Beyond int64 a bigint is a double, written as Go writes that float64.
    expect(canonicalJson(2n ** 63n)).toBe("9223372036854776000");
    expect(canonicalJson(10n ** 22n)).toBe("1e+22");
  });

  it("escapes separators; a lone surrogate is U+FFFD, as in the UTF-8 the server receives", () => {
    expect(canonicalJson("a\u2028b\u2029")).toBe('"a\\u2028b\\u2029"');
    expect(canonicalJson("x\ud800y\udc00")).toBe('"x\ufffdy\ufffd"');
    expect(canonicalJson("x\ud800y")).toBe(canonicalJson(Buffer.from("x\ud800y").toString("utf8")));
    expect(canonicalJson("😀")).toBe('"😀"');
    expect(canonicalJson("\u0000\u001f\b\f")).toBe('"\\u0000\\u001f\\b\\f"');
  });

  it("orders keys by code point, not by UTF-16 code unit", () => {
    // U+1F600 is above U+FF5E as a code point, below it as UTF-16 code units.
    expect(canonicalJson({ "😀": 1, "～": 2, a: 3 })).toBe('{"a":3,"～":2,"😀":1}');
    expect(compareCodePoints("ab", "a")).toBeGreaterThan(0);
    expect(compareCodePoints("", "")).toBe(0);
  });

  it("is a fixed point: canonical JSON parsed and encoded again is unchanged", () => {
    const next = rng(20260926);
    for (let i = 0; i < 2000; i++) {
      const v = randomValue(next);
      const once = canonicalJson(v);
      expect(canonicalJson(JSON.parse(once))).toBe(once);
    }
  });
});

function randomValue(next: () => number, depth = 0): unknown {
  const roll = next();
  if (depth >= 3 || roll < 0.5) {
    const kind = Math.floor(next() * 5);
    if (kind === 0) return null;
    if (kind === 1) return next() < 0.5;
    if (kind === 2) return (next() - 0.5) * 10 ** Math.floor(next() * 40 - 20);
    if (kind === 3) return Math.round((next() - 0.5) * 1e6);
    return String.fromCharCode(
      ...Array.from({ length: Math.floor(next() * 6) }, () => Math.floor(next() * 0x3000)),
    );
  }
  if (roll < 0.75) return Array.from({ length: Math.floor(next() * 4) }, () => randomValue(next, depth + 1));
  const obj: Record<string, unknown> = {};
  for (let i = 0; i < Math.floor(next() * 4); i++)
    obj[`k${Math.floor(next() * 100)}`] = randomValue(next, depth + 1);
  return obj;
}
