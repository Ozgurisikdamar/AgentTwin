/**
 * Canonical JSON and SHA-256 content identities.
 *
 * The canonical form is shared with the Go services (`gokit/hashx`) and the
 * Python SDK, and all of them are verified against
 * `packages/contracts/fixtures/canonical-json.json`:
 *
 * - object keys sorted by code point (identical to UTF-8 byte order);
 * - no insignificant whitespace, no HTML escaping; U+2028 and U+2029 escaped;
 *   a lone surrogate is written as U+FFFD, which is what it becomes whenever
 *   JavaScript encodes the string as UTF-8 (TextEncoder, Buffer, fetch), so
 *   the canonical form is that of the text the server receives;
 * - integers without fraction or exponent; integral numbers below 1e21 as
 *   their shortest round-trip digits padded with zeros (Go's `'f', -1`);
 * - other numbers in the shortest round-trip form printed like Go's
 *   `strconv.FormatFloat(f, 'g', -1, 64)` (exponent below 1e-4 and at or
 *   above 1e6).
 *
 * JavaScript numbers are IEEE doubles, like Go's float64; a `bigint` is
 * written exactly when it fits in int64 (as Go and Python write integers).
 * Object properties whose value is `undefined` are left out, as
 * `JSON.stringify` does; any other value JSON cannot represent is an error.
 */

import { createHash } from "node:crypto";

const INT64_MIN = -(2n ** 63n);
const INT64_MAX = 2n ** 63n - 1n;

/** Hex SHA-256 of bytes (strings are UTF-8 encoded). */
export function sha256Hex(data: string | Uint8Array): string {
  return createHash("sha256").update(data).digest("hex");
}

/** SHA-256 of the canonical JSON form of `value`. */
export function contentHash(value: unknown): string {
  return sha256Hex(canonicalJson(value));
}

/**
 * The canonical JSON encoding of `value`. Throws TypeError for values JSON
 * cannot represent (and for cycles) and RangeError for non-finite numbers.
 */
export function canonicalJson(value: unknown): string {
  const out: string[] = [];
  write(value, out, new Set());
  return out.join("");
}

function write(value: unknown, out: string[], seen: Set<object>): void {
  if (value === null) {
    out.push("null");
    return;
  }
  switch (typeof value) {
    case "boolean":
      out.push(value ? "true" : "false");
      return;
    case "number":
      out.push(formatNumber(value));
      return;
    case "bigint":
      out.push(value >= INT64_MIN && value <= INT64_MAX ? value.toString() : formatNumber(Number(value)));
      return;
    case "string":
      out.push(quote(value));
      return;
    case "object":
      break;
    default:
      throw new TypeError(`canonical json: unsupported type ${typeof value}`);
  }
  const obj = value as object;
  if (seen.has(obj)) throw new TypeError("canonical json: cyclic value");
  seen.add(obj);
  if (Array.isArray(obj)) {
    out.push("[");
    obj.forEach((item: unknown, i) => {
      if (i) out.push(",");
      if (item === undefined) throw new TypeError("canonical json: undefined array element");
      write(item, out, seen);
    });
    out.push("]");
  } else {
    const proto: unknown = Object.getPrototypeOf(obj);
    if (proto !== Object.prototype && proto !== null) {
      throw new TypeError(
        `canonical json: unsupported object ${obj.constructor?.name ?? "without a prototype"}`,
      );
    }
    const entries = Object.entries(obj).filter(([, v]) => v !== undefined);
    entries.sort(([a], [b]) => compareCodePoints(a, b));
    out.push("{");
    entries.forEach(([key, item], i) => {
      if (i) out.push(",");
      out.push(quote(key), ":");
      write(item, out, seen);
    });
    out.push("}");
  }
  seen.delete(obj);
}

/**
 * Orders strings by Unicode code point, not by UTF-16 code unit (the
 * default sort): characters above U+FFFF are surrogate pairs whose code
 * units sort below U+E000..U+FFFF although their code points are above.
 */
export function compareCodePoints(a: string, b: string): number {
  const n = Math.min(a.length, b.length);
  for (let i = 0; i < n; i++) {
    let x = a.charCodeAt(i);
    let y = b.charCodeAt(i);
    if (x === y) continue;
    if (x >= 0xd800 && y >= 0xd800) {
      x = x >= 0xe000 ? x - 0x800 : x + 0x2000;
      y = y >= 0xe000 ? y - 0x800 : y + 0x2000;
    }
    return x - y;
  }
  return a.length - b.length;
}

// eslint-disable-next-line no-control-regex -- the control characters are the point
const NEEDS_ESCAPE = /["\\\u0000-\u001f\u2028\u2029\ud800-\udfff]/;

function quote(s: string): string {
  if (!NEEDS_ESCAPE.test(s)) return `"${s}"`;
  let out = '"';
  for (let i = 0; i < s.length; i++) {
    const c = s.charCodeAt(i);
    if (c === 0x22) out += '\\"';
    else if (c === 0x5c) out += "\\\\";
    else if (c < 0x20) out += CONTROL[c];
    else if (c === 0x2028 || c === 0x2029) out += `\\u${c.toString(16)}`;
    else if (c >= 0xd800 && c <= 0xdbff) {
      const next = s.charCodeAt(i + 1);
      if (next >= 0xdc00 && next <= 0xdfff) {
        out += s[i]! + s[i + 1]!;
        i++;
      } else out += "\ufffd";
    } else if (c >= 0xdc00 && c <= 0xdfff) out += "\ufffd";
    else out += s[i];
  }
  return out + '"';
}

const CONTROL: string[] = Array.from({ length: 0x20 }, (_, c) => `\\u${c.toString(16).padStart(4, "0")}`);
CONTROL[0x08] = "\\b";
CONTROL[0x09] = "\\t";
CONTROL[0x0a] = "\\n";
CONTROL[0x0c] = "\\f";
CONTROL[0x0d] = "\\r";

function formatNumber(f: number): string {
  if (!Number.isFinite(f)) throw new RangeError(`canonical json: non-finite number ${f}`);
  if (Number.isInteger(f) && Math.abs(f) < 1e21) {
    // Go's 'f', -1: shortest round-trip digits padded with zeros, which is
    // what JavaScript prints for integral values below 1e21 ("-0" is "0").
    return f === 0 ? "0" : String(f);
  }
  return goShortestG(f);
}

/** Formats like Go's strconv.FormatFloat(f, 'g', -1, 64). */
function goShortestG(f: number): string {
  // toExponential() without an argument gives the shortest round-trip digits.
  const [mantissa, exp] = f.toExponential().split("e") as [string, string];
  const neg = mantissa.startsWith("-") ? "-" : "";
  const digits = mantissa.replace("-", "").replace(".", "").replace(/0+$/, "") || "0";
  const exp10 = Number(exp);
  const nd = digits.length;
  if (exp10 < -4 || exp10 >= 6) {
    const m = digits[0]! + (nd > 1 ? `.${digits.slice(1)}` : "");
    return `${neg}${m}e${exp10 < 0 ? "-" : "+"}${String(Math.abs(exp10)).padStart(2, "0")}`;
  }
  const dp = exp10 + 1; // value = 0.<digits> * 10**dp
  if (dp <= 0) return `${neg}0.${"0".repeat(-dp)}${digits}`;
  if (dp >= nd) return `${neg}${digits}${"0".repeat(dp - nd)}`;
  return `${neg}${digits.slice(0, dp)}.${digits.slice(dp)}`;
}
