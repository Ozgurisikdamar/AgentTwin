/**
 * Client-side redaction of secrets and personal data.
 *
 * The rules and their semantics mirror `gokit/redact` and the Python SDK
 * exactly. The patterns use ASCII classes, Go's `\s` set `[\t\n\f\r ]` and no
 * `u` flag, so JavaScript, RE2 and Python's `re` match the same text; parity
 * is verified against `packages/contracts/fixtures/redaction.json`.
 *
 * Strategies:
 *
 * - `mask`: replace a match with `[REDACTED:<kind>]`
 * - `hash`: replace a match with `[HASH:<kind>:<12 hex of sha256>]`, so equal
 *   values stay comparable without being revealed
 * - `drop`: drop the whole value when anything sensitive is found
 */

import { canonicalJson, sha256Hex } from "./hashing.js";

export type Strategy = "mask" | "hash" | "drop";
export const STRATEGIES: readonly Strategy[] = ["mask", "hash", "drop"];

/** One detector. `group` selects the sub-match that is replaced. */
export interface Rule {
  readonly kind: string;
  readonly pattern: RegExp;
  readonly group?: number;
  readonly valid?: (match: string) => boolean;
}

const WS = "[\\t\\n\\f\\r ]"; // Go RE2 \s

function re(source: string, flags = ""): RegExp {
  return new RegExp(source, `gd${flags}`);
}

function luhn(s: string): boolean {
  const digits = [...s].filter((c) => c >= "0" && c <= "9").map((c) => c.charCodeAt(0) - 48);
  if (digits.length < 13 || digits.length > 19) return false;
  let total = 0;
  let double = false;
  for (let i = digits.length - 1; i >= 0; i--) {
    let d = digits[i]!;
    if (double) {
      d *= 2;
      if (d > 9) d -= 9;
    }
    total += d;
    double = !double;
  }
  return total % 10 === 0;
}

function phoneDigits(s: string): boolean {
  const n = [...s].filter((c) => c >= "0" && c <= "9").length;
  return n >= 9 && n <= 15;
}

export const SECRET_RULES: readonly Rule[] = [
  {
    kind: "private_key",
    pattern: re("-----BEGIN [A-Z ]*PRIVATE KEY-----[\\s\\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
  },
  { kind: "jwt", pattern: re("\\beyJ[A-Za-z0-9_-]{5,}\\.[A-Za-z0-9_-]{5,}\\.[A-Za-z0-9_-]{5,}") },
  { kind: "bearer", pattern: re(`\\bbearer${WS}+([A-Za-z0-9._~+/=-]{8,})`, "i"), group: 1 },
  {
    kind: "api_key",
    pattern: re(
      "\\b(?:atk_[a-z0-9]{8}_[A-Za-z0-9_-]{8,}|sk-[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16}" +
        "|gh[pousr]_[A-Za-z0-9]{30,}|xox[baprs]-[A-Za-z0-9-]{10,})",
    ),
  },
  {
    kind: "credential",
    pattern: re(
      "\\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|client[_-]?secret)\\b" +
        `["']?${WS}*[:=]${WS}*["']?([^\\t\\n\\f\\r "',;]{4,})`,
      "i",
    ),
    group: 1,
  },
];

export const PII_RULES: readonly Rule[] = [
  { kind: "email", pattern: re("\\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}\\b") },
  { kind: "card", pattern: re("\\b(?:[0-9][ -]?){12,18}[0-9]\\b"), valid: luhn },
  {
    kind: "phone",
    pattern: re(
      "\\+[0-9]{1,3}(?:[\\t\\n\\f\\r .-]?[0-9]{1,4}){2,5}\\b" +
        `|\\([0-9]{3}\\)${WS}?[0-9]{3}[\\t\\n\\f\\r .-][0-9]{4}\\b` +
        "|\\b[0-9]{3}[.-][0-9]{3}[.-][0-9]{4}\\b",
    ),
    valid: phoneDigits,
  },
];

const MARKER = /\[(?:REDACTED:[a-z_]+|HASH:[a-z_]+:[0-9a-f]{12})\]/g;
const MAX_ROUNDS = 8;

/** What the SDK redacts before any content leaves the process. */
export interface RedactionConfig {
  /** `mask` (default), `hash` or `drop`. */
  readonly strategy?: Strategy;
  /** Email, card and phone detection (secrets are always detected). Default true. */
  readonly pii?: boolean;
  /** Extra regular expressions (JavaScript syntax), reported as kind `custom`. */
  readonly customPatterns?: readonly string[];
  /**
   * Whole fields of structured values (tool arguments, results) to redact,
   * e.g. `$.customer.email` or `$.items[*].card_number`.
   */
  readonly jsonPaths?: readonly string[];
}

/** Custom patterns as rules (kind `custom`); throws SyntaxError on a bad pattern. */
export function customRules(patterns: readonly string[] = []): Rule[] {
  return patterns.map((p) => ({ kind: "custom", pattern: new RegExp(p, "gd") }));
}

/** The redactor a configuration describes. */
export function redactorFor(config: RedactionConfig = {}): Redactor {
  const rules: Rule[] = [...SECRET_RULES];
  if (config.pii ?? true) rules.push(...PII_RULES);
  rules.push(...customRules(config.customPatterns));
  return new Redactor(rules, config.strategy ?? "mask", config.jsonPaths ?? []);
}

type Segment = string | number;

/** Applies rules with a strategy. Instances are immutable. */
export class Redactor {
  readonly rules: readonly Rule[];
  readonly strategy: Strategy;
  private readonly paths: Segment[][];

  constructor(rules: readonly Rule[], strategy: Strategy = "mask", jsonPaths: readonly string[] = []) {
    if (!STRATEGIES.includes(strategy)) throw new RangeError(`unknown redaction strategy ${strategy}`);
    this.rules = rules.map((r) => ({ ...r, pattern: globalWithIndices(r.pattern) }));
    this.strategy = strategy;
    this.paths = jsonPaths.map(parsePath);
  }

  static secrets(strategy: Strategy = "mask"): Redactor {
    return new Redactor(SECRET_RULES, strategy);
  }

  static all(strategy: Strategy = "mask"): Redactor {
    return new Redactor([...SECRET_RULES, ...PII_RULES], strategy);
  }

  /**
   * Redacts free text. With the `drop` strategy the text is `null` when
   * anything sensitive was found.
   */
  text(s: string): { text: string | null; changed: boolean } {
    let changed = false;
    for (let round = 0; round < MAX_ROUNDS; round++) {
      const pass = this.pass(s);
      if (!pass.changed) break;
      s = pass.text;
      changed = true;
    }
    if (changed && this.strategy === "drop") return { text: null, changed };
    return { text: s, changed };
  }

  private pass(s: string): { text: string; changed: boolean } {
    let changed = false;
    for (const rule of this.rules) {
      const r = this.outsideMarkers(rule, s);
      s = r.text;
      changed ||= r.changed;
    }
    return { text: s, changed };
  }

  private outsideMarkers(rule: Rule, s: string): { text: string; changed: boolean } {
    const spans = [...s.matchAll(MARKER)].map((m) => [m.index, m.index + m[0].length] as const);
    if (spans.length === 0) return this.apply(rule, s);
    const parts: string[] = [];
    let changed = false;
    let prev = 0;
    for (const [start, end] of spans) {
      const seg = this.apply(rule, s.slice(prev, start));
      parts.push(seg.text, s.slice(start, end));
      changed ||= seg.changed;
      prev = end;
    }
    const seg = this.apply(rule, s.slice(prev));
    parts.push(seg.text);
    return { text: parts.join(""), changed: changed || seg.changed };
  }

  private apply(rule: Rule, s: string): { text: string; changed: boolean } {
    const parts: string[] = [];
    let prev = 0;
    let changed = false;
    for (const m of s.matchAll(rule.pattern)) {
      const span = m.indices?.[rule.group ?? 0];
      if (span === undefined) continue;
      const [start, end] = span;
      const target = s.slice(start, end);
      if (!target || (rule.valid !== undefined && !rule.valid(target))) continue;
      parts.push(s.slice(prev, start), this.replacement(rule.kind, target));
      prev = end;
      changed = true;
    }
    if (!changed) return { text: s, changed: false };
    parts.push(s.slice(prev));
    return { text: parts.join(""), changed: true };
  }

  private replacement(kind: string, value: string): string {
    if (this.strategy === "hash") return `[HASH:${kind}:${sha256Hex(value).slice(0, 12)}]`;
    return `[REDACTED:${kind}]`;
  }

  /**
   * Redacts a JSON value: configured JSON paths first (whole fields), then
   * every string leaf. Keys are never redacted. With the `drop` strategy
   * sensitive fields are removed. The input is not modified.
   */
  value(v: unknown): { value: unknown; changed: boolean } {
    let out = structuredClone(v);
    let changed = false;
    for (const path of this.paths) {
      const r = applyPath(out, path, this);
      out = r.value;
      changed ||= r.changed;
    }
    const r = this.leaves(out);
    return { value: r.value, changed: changed || r.changed };
  }

  private leaves(v: unknown): { value: unknown; changed: boolean } {
    if (typeof v === "string") {
      const r = this.text(v);
      return { value: r.text, changed: r.changed };
    }
    if (Array.isArray(v)) {
      const items: unknown[] = [];
      let changed = false;
      for (const item of v) {
        const r = this.leaves(item);
        changed ||= r.changed;
        if (r.value === null && r.changed && this.strategy === "drop") continue;
        items.push(r.value);
      }
      return { value: items, changed };
    }
    if (v !== null && typeof v === "object") {
      const result: Record<string, unknown> = {};
      let changed = false;
      for (const [k, item] of Object.entries(v)) {
        const r = this.leaves(item);
        changed ||= r.changed;
        if (r.value === null && r.changed && this.strategy === "drop") continue;
        result[k] = r.value;
      }
      return { value: result, changed };
    }
    return { value: v, changed: false };
  }

  /** What a whole field selected by a JSON path becomes (`undefined`: removed). */
  fieldReplacement(v: unknown): string | undefined {
    if (this.strategy === "drop") return undefined;
    if (this.strategy === "hash") {
      let encoded: string;
      try {
        encoded = canonicalJson(v);
      } catch {
        encoded = String(v);
      }
      return `[HASH:field:${sha256Hex(encoded).slice(0, 12)}]`;
    }
    return "[REDACTED:field]";
  }
}

function globalWithIndices(pattern: RegExp): RegExp {
  let flags = pattern.flags;
  if (!flags.includes("g")) flags += "g";
  if (!flags.includes("d")) flags += "d";
  return flags === pattern.flags ? pattern : new RegExp(pattern.source, flags);
}

// JSON paths: a deliberately small subset ($.a.b, $.a[*].b, $.a[0], $['k']).
const PATH_TOKEN = /\.([A-Za-z0-9_-]+)|\[(\*|[0-9]+)\]|\['([^']*)'\]/y;

export function parsePath(path: string): Segment[] {
  if (!path.startsWith("$")) throw new SyntaxError(`json path must start with '$': ${path}`);
  const segs: Segment[] = [];
  let pos = 1;
  while (pos < path.length) {
    PATH_TOKEN.lastIndex = pos;
    const m = PATH_TOKEN.exec(path);
    if (!m) throw new SyntaxError(`unsupported json path syntax at ${pos}: ${path}`);
    const [, name, index, quoted] = m;
    if (name !== undefined) segs.push(name);
    else if (quoted !== undefined) segs.push(quoted);
    else if (index === "*") segs.push("*");
    else segs.push(Number(index));
    pos = PATH_TOKEN.lastIndex;
  }
  if (segs.length === 0) throw new SyntaxError(`json path selects the whole document: ${path}`);
  return segs;
}

function applyPath(v: unknown, path: Segment[], red: Redactor): { value: unknown; changed: boolean } {
  if (path.length === 0) return { value: v, changed: false };
  const [head, ...rest] = path as [Segment, ...Segment[]];
  let changed = false;
  if (Array.isArray(v)) {
    const idxs =
      head === "*" ? v.map((_, i) => i) : typeof head === "number" && head < v.length ? [head] : [];
    const drop: number[] = [];
    for (const i of idxs) {
      if (rest.length > 0) {
        const r = applyPath(v[i], rest, red);
        v[i] = r.value;
        changed ||= r.changed;
      } else {
        const rep = red.fieldReplacement(v[i]);
        if (rep === undefined) drop.push(i);
        else v[i] = rep;
        changed = true;
      }
    }
    for (const i of drop.reverse()) v.splice(i, 1);
  } else if (v !== null && typeof v === "object") {
    const obj = v as Record<string, unknown>;
    const keys = head === "*" ? Object.keys(obj) : typeof head === "string" ? [head] : [];
    for (const k of keys) {
      if (!Object.hasOwn(obj, k)) continue;
      if (rest.length > 0) {
        const r = applyPath(obj[k], rest, red);
        obj[k] = r.value;
        changed ||= r.changed;
      } else {
        const rep = red.fieldReplacement(obj[k]);
        if (rep === undefined) Reflect.deleteProperty(obj, k);
        else obj[k] = rep;
        changed = true;
      }
    }
  }
  return { value: v, changed };
}

const TRUNCATED = "…[truncated]";
const TRUNCATED_BYTES = Buffer.byteLength(TRUNCATED);

/**
 * Shortens `s` to at most `maxBytes` UTF-8 bytes on a character boundary,
 * appending a marker (same contract as gokit/redact.Truncate).
 */
export function truncate(s: string, maxBytes: number): { text: string; truncated: boolean } {
  const raw = Buffer.from(s, "utf8");
  if (maxBytes <= 0 || raw.length <= maxBytes) return { text: s, truncated: false };
  let cut = Math.max(maxBytes - TRUNCATED_BYTES, 0);
  while (cut > 0 && (raw[cut]! & 0xc0) === 0x80) cut--;
  return { text: raw.subarray(0, cut).toString("utf8") + TRUNCATED, truncated: true };
}
