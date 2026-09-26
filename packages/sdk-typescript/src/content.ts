/**
 * What content may leave the process (ADR-0008), and the content identities
 * the SDK records instead of content.
 */

import type { Config, ContentMode } from "./config.js";
import { canonicalJson, sha256Hex } from "./hashing.js";
import { customRules, redactorFor, Redactor, SECRET_RULES, truncate } from "./redaction.js";

/** Decides what content is recorded: nothing (`off`, the default), redacted
 * content, or full content with secrets still masked. */
export class ContentPolicy {
  readonly mode: ContentMode;
  readonly maxBytes: number;
  private readonly redactor: Redactor | undefined;

  constructor(config: Config) {
    this.mode = config.contentMode;
    this.maxBytes = config.maxContentBytes;
    const red = config.redaction;
    if (this.mode === "redacted") {
      this.redactor = redactorFor(red);
    } else if (this.mode === "full") {
      this.redactor = new Redactor(
        [...SECRET_RULES, ...customRules(red.customPatterns)],
        red.strategy ?? "mask",
        red.jsonPaths ?? [],
      );
    } else {
      this.redactor = undefined;
    }
  }

  get redacted(): boolean {
    return this.mode === "redacted";
  }

  /** Free text as it may be recorded, or undefined when it may not. */
  text(s: string | null | undefined): string | undefined {
    if (this.redactor === undefined || s === null || s === undefined) return undefined;
    const out = this.redactor.text(String(s)).text;
    return out === null ? undefined : truncate(out, this.maxBytes).text;
  }

  /** A structured value, JSON-encoded as it may be recorded, or undefined. */
  value(v: unknown): string | undefined {
    if (this.redactor === undefined || v === null || v === undefined) return undefined;
    try {
      const red = this.redactor.value(toJsonValue(v)).value;
      if (red === null || red === undefined) return undefined;
      return truncate(stringify(red), this.maxBytes).text;
    } catch {
      return undefined; // never fail the host on odd values
    }
  }
}

/**
 * A plain JSON value for anything: what `JSON.stringify` would write
 * (`toJSON`, undefined and functions left out of objects and null in
 * arrays, non-finite numbers as null), except that bigints stay bigints,
 * Maps and Sets become objects and arrays, and cycles become "[Circular]".
 */
export function toJsonValue(v: unknown, seen: Set<object> = new Set()): unknown {
  if (v === null) return null;
  switch (typeof v) {
    case "string":
    case "boolean":
    case "bigint":
      return v;
    case "number":
      return Number.isFinite(v) ? v : null;
    case "object":
      break;
    default:
      return undefined; // undefined, function, symbol
  }
  const obj = v as object & { toJSON?: unknown };
  if (typeof obj.toJSON === "function") return toJsonValue((obj.toJSON as () => unknown)(), seen);
  if (seen.has(obj)) return "[Circular]";
  seen.add(obj);
  try {
    if (Array.isArray(obj) || obj instanceof Set) {
      return [...(obj as Iterable<unknown>)].map((item) => toJsonValue(item, seen) ?? null);
    }
    const entries: [unknown, unknown][] = obj instanceof Map ? [...obj.entries()] : Object.entries(obj);
    const out: Record<string, unknown> = {};
    for (const [key, item] of entries) {
      const value = toJsonValue(item, seen);
      if (value !== undefined) out[String(key)] = value;
    }
    return out;
  } finally {
    seen.delete(obj);
  }
}

/** JSON text of a plain JSON value that may hold bigints. */
function stringify(v: unknown): string {
  return JSON.stringify(v, (_k, x: unknown) => (typeof x === "bigint" ? RawBigint.of(x) : x)).replace(
    RawBigint.pattern,
    (_m, digits: string) => digits,
  );
}

/** Marks a bigint in JSON.stringify output so it can be written unquoted. */
const RawBigint = {
  of: (x: bigint) => `\u0000bigint:${x.toString()}\u0000`,
  pattern: /"\\u0000bigint:(-?\d+)\\u0000"/g,
};

/** SHA-256 of the canonical JSON of tool arguments (never throws). */
export function argsHash(args: unknown): string {
  try {
    return sha256Hex(canonicalJson(toJsonValue(args) ?? null));
  } catch {
    return sha256Hex(String(args));
  }
}

/** Short, non-reversible identity of an idempotency key. */
export function idempotencyKeyHash(key: string): string {
  return sha256Hex(key).slice(0, 16);
}
