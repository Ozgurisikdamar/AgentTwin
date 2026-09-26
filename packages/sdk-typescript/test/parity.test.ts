/**
 * Differential tests: the TypeScript implementation must agree with the Go
 * reference implementation (gokit/hashx, gokit/redact) on thousands of random
 * inputs, not only on the hand-written fixtures. Inputs come from a seeded
 * generator (AGENTTWIN_PARITY_SEED); the seed is in every failure message.
 */
import { describe, expect, it } from "vitest";

import { canonicalJson } from "../src/hashing.js";
import { Redactor } from "../src/redaction.js";
import { goParity, rng } from "./helpers.js";

const SEED = Number(process.env.AGENTTWIN_PARITY_SEED ?? "20260924");
const CASES = Number(process.env.AGENTTWIN_PARITY_CASES ?? "3000");
const go = goParity();

const FRAGMENTS = [
  "jane.doe@example.com",
  "x@y.io",
  "password=",
  "passwd: ",
  "secret=",
  "api_key: '",
  "access-token=",
  "Bearer ",
  "bearer\t",
  "eyJhbGciOiJIUzI1NiJ9.",
  "eyJzdWIiOiIxMjMifQ.",
  "atk_ab12cd34_",
  "sk-ant-api03-",
  "AKIA",
  "ghp_",
  "xoxb-",
  "4111 1111 1111 1111",
  "4111-1111-1111-1111",
  "1234 5678 9012 3456",
  "+44 20 7946 0958",
  "+1.415.555.2671",
  "(415) 555-2671",
  "415-555-2671",
  "[REDACTED:email]",
  "[HASH:card:0123456789ab]",
  "[REDACTED:",
  "ORD-1001",
  "150.00 USD",
  "-----BEGIN RSA PRIVATE KEY-----",
  "-----END RSA PRIVATE KEY-----",
  "ş",
  "é",
  "😀",
  "\u2028",
  "\u0000",
];
const ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 \t\n.,:;=@+-_/'\"()[]{}";

function pick<T>(next: () => number, items: readonly T[]): T {
  return items[Math.floor(next() * items.length)]!;
}

function randomText(next: () => number): string {
  let s = "";
  for (let i = Math.floor(next() * 11); i > 0; i--) {
    if (next() < 0.55) s += pick(next, FRAGMENTS);
    else for (let j = Math.floor(next() * 17); j > 0; j--) s += pick(next, [...ALPHABET]);
  }
  return s;
}

function randomJson(next: () => number, depth = 0): unknown {
  const roll = next();
  if (depth >= 3 || roll < 0.45) {
    switch (Math.floor(next() * 7)) {
      case 0:
        return null;
      case 1:
        return next() < 0.5;
      case 2:
        if (next() < 0.2) {
          const big = BigInt(Math.floor(next() * 2 ** 53)) * BigInt(Math.floor(next() * 2 ** 17));
          return next() < 0.5 ? -big : big;
        }
        return Math.floor(next() * 2001) - 1000;
      case 3: {
        const exp = pick(next, [-30, -8, -5, -4, -3, 0, 3, 5, 6, 7, 15, 20, 21, 22, 300]);
        return (next() * 20 - 10) * 10 ** exp;
      }
      case 4:
        return Math.floor(next() * 2e6) - 1e6;
      default:
        return randomText(next);
    }
  }
  if (roll < 0.72) return Array.from({ length: Math.floor(next() * 5) }, () => randomJson(next, depth + 1));
  const obj: Record<string, unknown> = {};
  for (let i = Math.floor(next() * 5); i > 0; i--)
    obj[randomText(next).slice(0, 8)] = randomJson(next, depth + 1);
  return obj;
}

describe.skipIf(go === undefined)("parity with the Go reference implementation", () => {
  it("redaction agrees with gokit/redact", { timeout: 120_000 }, () => {
    const next = rng(SEED);
    const inputs = Array.from({ length: CASES }, () => randomText(next));
    for (const mode of ["all", "secrets"] as const) {
      for (const strategy of ["mask", "hash"] as const) {
        const want = go!(inputs.map((input) => ({ op: "redact", mode, strategy, input })));
        const red = mode === "all" ? Redactor.all(strategy) : Redactor.secrets(strategy);
        inputs.forEach((input, i) => {
          expect(
            red.text(input).text,
            `seed=${SEED} mode=${mode} strategy=${strategy} input=${JSON.stringify(input)}`,
          ).toBe(want[i]!.output);
        });
      }
    }
  });

  it("canonical JSON agrees with gokit/hashx", { timeout: 120_000 }, () => {
    const next = rng(SEED + 1);
    const values = Array.from({ length: CASES }, () => randomJson(next));
    const want = go!(values.map((value) => ({ op: "canonical", value })));
    values.forEach((value, i) => {
      expect(want[i]!.error).toBeUndefined();
      expect(canonicalJson(value), `seed=${SEED + 1} case=${i}`).toBe(want[i]!.output);
    });
  });
});
