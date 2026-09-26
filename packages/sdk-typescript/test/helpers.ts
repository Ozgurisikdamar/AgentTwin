import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

export const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
const FIXTURES = path.join(REPO, "packages", "contracts", "fixtures");

/**
 * Parses JSON keeping integer literals that a double cannot hold exactly as
 * bigint, so a fixture's 9223372036854775807 reaches the code as written.
 */
export function parseExact(text: string): unknown {
  return JSON.parse(text, (_key, value: unknown, context?: { source?: string }) => {
    if (typeof value === "number" && !Number.isSafeInteger(value) && /^-?\d+$/.test(context?.source ?? "")) {
      return BigInt(context!.source!);
    }
    return value;
  });
}

export function loadFixture(name: string): unknown {
  return parseExact(readFileSync(path.join(FIXTURES, name), "utf8"));
}

/** JSON text for a value that may hold bigints (written as integer literals). */
export function toJsonText(value: unknown): string {
  if (typeof value === "bigint") return value.toString();
  if (Array.isArray(value)) return `[${value.map(toJsonText).join(",")}]`;
  if (value !== null && typeof value === "object") {
    return `{${Object.entries(value)
      .map(([k, v]) => `${JSON.stringify(k)}:${toJsonText(v)}`)
      .join(",")}}`;
  }
  return JSON.stringify(value);
}

export type ParityRequest =
  | { op: "canonical"; value: unknown }
  | { op: "redact"; mode: "all" | "secrets"; strategy: "mask" | "hash"; input: string };

/**
 * The Go reference implementation (packages/gokit/cmd/parity), or undefined
 * when Go is unavailable and AGENTTWIN_REQUIRE_PARITY is not 1.
 */
export function goParity(): ((reqs: ParityRequest[]) => { output: string; error?: string }[]) | undefined {
  const out = path.join(REPO, ".artifacts", "parity-ts");
  try {
    mkdirSync(path.dirname(out), { recursive: true });
    execFileSync("go", ["build", "-o", out, "./packages/gokit/cmd/parity"], { cwd: REPO, stdio: "pipe" });
  } catch (err) {
    // Only a missing Go toolchain skips; a build failure is a failure.
    const missing = (err as NodeJS.ErrnoException).code === "ENOENT";
    if (!missing || process.env.AGENTTWIN_REQUIRE_PARITY === "1") throw err;
    return undefined;
  }
  return (reqs) => {
    const input = `[${reqs.map((r) => (r.op === "canonical" ? `{"op":"canonical","value":${toJsonText(r.value)}}` : JSON.stringify(r))).join(",")}]`;
    const stdout = execFileSync(out, { input, maxBuffer: 256 * 1024 * 1024 });
    return JSON.parse(stdout.toString("utf8")) as { output: string; error?: string }[];
  };
}

/** A small seeded PRNG (mulberry32), so a failing random input is reproducible. */
export function rng(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
