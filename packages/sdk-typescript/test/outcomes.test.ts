import { readFileSync } from "node:fs";
import { createServer, type IncomingMessage, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import path from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { parse as parseYaml } from "yaml";

import { makeConfig } from "../src/config.js";
import { APIError, OutcomeReportError, reportOutcome } from "../src/outcomes.js";
import { OUTCOME_STATUSES, VERIFICATION_SOURCES } from "../src/tracing.js";
import { REPO } from "./helpers.js";

interface Schema {
  properties?: Record<string, { $ref?: string; enum?: string[] }>;
  required?: string[];
  enum?: string[];
  additionalProperties?: boolean;
}
const OPENAPI = parseYaml(
  readFileSync(path.join(REPO, "packages/contracts/openapi/trace-service.openapi.yaml"), "utf8"),
) as {
  components: { schemas: Record<string, Schema> };
};
const SCHEMAS = OPENAPI.components.schemas;

/** Checks a request body against RecordOutcomeRequest (keys, required, enums). */
function conforms(body: Record<string, unknown>): void {
  const schema = SCHEMAS.RecordOutcomeRequest!;
  expect(schema.additionalProperties).toBe(false);
  for (const key of schema.required ?? []) expect(body, key).toHaveProperty(key);
  for (const [key, value] of Object.entries(body)) {
    const prop = schema.properties?.[key];
    expect(prop, `${key} is not in RecordOutcomeRequest`).toBeDefined();
    const ref = prop?.$ref?.split("/").pop();
    if (ref !== undefined) expect(SCHEMAS[ref]!.enum, key).toContain(value);
  }
}

const servers: Server[] = [];
afterEach(async () => {
  await Promise.all(servers.splice(0).map((s) => new Promise((r) => s.close(r))));
});

interface Seen {
  method: string;
  path: string;
  headers: IncomingMessage["headers"];
  body: Record<string, unknown>;
}

async function stub(reply: (seen: Seen) => [number, unknown]): Promise<{ url: string; seen: Seen[] }> {
  const seen: Seen[] = [];
  const server = createServer((req, res) => {
    const chunks: Buffer[] = [];
    req.on("data", (c: Buffer) => chunks.push(c));
    req.on("end", () => {
      const s = {
        method: req.method ?? "",
        path: req.url ?? "",
        headers: req.headers,
        body: JSON.parse(Buffer.concat(chunks).toString() || "{}"),
      };
      seen.push(s);
      const [status, body] = reply(s);
      res.writeHead(status, { "content-type": "application/json" });
      res.end(body === undefined ? "" : JSON.stringify(body));
    });
  });
  servers.push(server);
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
  return { url: `http://127.0.0.1:${(server.address() as AddressInfo).port}/`, seen };
}

const KEY = "atk_demo0000_secret-value-123";
const TRACE = "4bf92f3577b34da6a3ce929d0e0e4736";

describe("reportOutcome", () => {
  it("the SDK's statuses and verification sources are the API's", () => {
    expect([...OUTCOME_STATUSES].sort()).toEqual([...SCHEMAS.OutcomeStatus!.enum!].sort());
    expect([...VERIFICATION_SOURCES].sort()).toEqual([...SCHEMAS.VerificationSource!.enum!].sort());
  });

  it("round trip: the request conforms to RecordOutcomeRequest; the stored outcome comes back", async () => {
    const { url, seen } = await stub((s) => [
      200,
      { ...s.body, contradiction: s.body.claimed_status !== s.body.status },
    ]);
    const config = makeConfig({ apiUrl: url, apiKey: KEY });
    const out = await reportOutcome(TRACE.toUpperCase(), "FAILURE", {
      config,
      verified: true,
      verificationSource: "state_assertion",
      claimedStatus: "SUCCESS",
      actualState: { refund_count: 2 },
      idempotencyKey: "outcome-1",
    });
    expect(out).toMatchObject({ status: "FAILURE", claimed_status: "SUCCESS", contradiction: true });
    const [req] = seen;
    expect(req!.method).toBe("POST");
    expect(req!.path).toBe(`/api/v1/traces/${TRACE}/outcome`);
    expect(req!.headers["x-agenttwin-api-key"]).toBe(KEY);
    expect(req!.headers["idempotency-key"]).toBe("outcome-1");
    expect(req!.headers["content-type"]).toBe("application/json");
    expect(req!.body).toEqual({
      status: "FAILURE",
      verified: true,
      verification_source: "state_assertion",
      claimed_status: "SUCCESS",
      actual_state: { refund_count: 2 },
    });
    conforms(req!.body);

    await reportOutcome(TRACE, "SUCCESS", {
      config,
      businessOutcome: "refund_issued",
      expectedState: { a: 1 },
      notes: "ok",
    });
    expect(seen[1]!.body).toEqual({
      status: "SUCCESS",
      verified: false,
      verification_source: "external_callback",
      business_outcome: "refund_issued",
      expected_state: { a: 1 },
      notes: "ok",
    });
    expect(seen[1]!.headers).not.toHaveProperty("idempotency-key");
    conforms(seen[1]!.body);
  });

  it("rejects invalid input before any request", async () => {
    const config = makeConfig({ apiUrl: "http://127.0.0.1:1", apiKey: KEY });
    await expect(reportOutcome("not-a-trace", "SUCCESS", { config })).rejects.toThrow(RangeError);
    await expect(reportOutcome(TRACE, "ESCALATED" as "SUCCESS", { config })).rejects.toThrow(RangeError);
    await expect(
      reportOutcome(TRACE, "SUCCESS", { config, claimedStatus: "YES" as "SUCCESS" }),
    ).rejects.toThrow(RangeError);
    await expect(
      reportOutcome(TRACE, "SUCCESS", { config, verificationSource: "self_report" as "unavailable" }),
    ).rejects.toThrow(RangeError);
    await expect(reportOutcome(TRACE, "SUCCESS", { config: makeConfig({ apiKey: KEY }) })).rejects.toThrow(
      /apiUrl/,
    );
    await expect(
      reportOutcome(TRACE, "SUCCESS", { config: makeConfig({ apiUrl: "ftp://x", apiKey: KEY }) }),
    ).rejects.toThrow(/http/);
  });

  it("API errors carry status and code, never the key", async () => {
    const { url } = await stub(() => [
      401,
      { error: { code: "UNAUTHENTICATED", message: "invalid API key", details: { hint: "x" } } },
    ]);
    const err = await reportOutcome(TRACE, "SUCCESS", {
      config: makeConfig({ apiUrl: url, apiKey: KEY }),
    }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(OutcomeReportError);
    expect(err).toBeInstanceOf(APIError);
    expect(err).toMatchObject({
      status: 401,
      code: "UNAUTHENTICATED",
      message: "invalid API key",
      details: { hint: "x" },
    });
    expect(String(err)).toBe("401 UNAUTHENTICATED: invalid API key");
    expect(JSON.stringify(err) + String(err)).not.toContain("secret-value");

    const { url: plain } = await stub(() => [502, undefined]);
    await expect(
      reportOutcome(TRACE, "SUCCESS", { config: makeConfig({ apiUrl: plain, apiKey: KEY }) }),
    ).rejects.toMatchObject({
      status: 502,
      code: "HTTP_ERROR",
    });

    const down = await reportOutcome(TRACE, "SUCCESS", {
      config: makeConfig({ apiUrl: "http://127.0.0.1:1", apiKey: KEY }),
      timeoutMs: 1000,
    }).catch((e: unknown) => e);
    expect(down).toMatchObject({ status: 0, code: "UNAVAILABLE" });
    expect(String(down)).not.toContain("secret-value");
  });
});
