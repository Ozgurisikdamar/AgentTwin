import { readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test } from "@playwright/test";
import { parse } from "yaml";
import { controlPlaneURL, runDemoAgent, signIn, watchConsole } from "./helpers";

/**
 * Security acceptance against the running stack (spec §63): hostile text in
 * a trace is shown as text and never runs, and every read endpoint of every
 * API contract answers injection payloads without a 5xx, a leaked database
 * error or a delay a query could have caused.
 */

const here = path.dirname(fileURLToPath(import.meta.url));
const contractsDir = path.resolve(here, "../../../packages/contracts/openapi");

interface Parameter {
  name: string;
  in: "path" | "query" | "header" | "cookie";
  required?: boolean;
}

interface Operation {
  contract: string;
  method: string;
  path: string;
  params: Parameter[];
  hasBody: boolean;
}

type Doc = {
  paths: Record<string, Record<string, unknown>>;
  components?: { parameters?: Record<string, Parameter> };
};

function operations(): Operation[] {
  const out: Operation[] = [];
  for (const file of readdirSync(contractsDir)
    .filter((f) => f.endsWith(".openapi.yaml"))
    .sort()) {
    const doc = parse(readFileSync(path.join(contractsDir, file), "utf8")) as Doc;
    const resolve = (p: Parameter | { $ref: string }): Parameter =>
      "$ref" in p ? doc.components!.parameters![p.$ref.split("/").pop()!]! : p;
    for (const [route, item] of Object.entries(doc.paths)) {
      const shared = ((item.parameters as Parameter[] | undefined) ?? []).map(resolve);
      for (const method of ["get", "post", "put", "patch", "delete"]) {
        const op = item[method] as { parameters?: Parameter[]; requestBody?: unknown } | undefined;
        if (!op) continue;
        out.push({
          contract: file.replace(".openapi.yaml", ""),
          method: method.toUpperCase(),
          path: route,
          params: [...shared, ...(op.parameters ?? []).map(resolve)],
          hasBody: op.requestBody !== undefined,
        });
      }
    }
  }
  return out;
}

const enc = encodeURIComponent;

/** Payloads, already percent-encoded (some are not valid UTF-8 on purpose). */
const PAYLOADS: { name: string; raw: string; unstorable?: boolean }[] = [
  { name: "tautology", raw: enc("' OR '1'='1") },
  { name: "stacked statement", raw: enc("1'; DROP TABLE control.project; --") },
  { name: "union", raw: enc("%' UNION SELECT NULL, version() --") },
  { name: "time delay", raw: enc("1' AND pg_sleep(5) --") },
  { name: "LIKE wildcards", raw: enc("%%_%") },
  { name: "JSON operator", raw: enc('{"$ne": null}') },
  { name: "path traversal", raw: enc("../../../etc/passwd") },
  { name: "CRLF", raw: enc("a\r\nX-Injected: 1") },
  { name: "10 000 characters", raw: "x".repeat(10_000) },
  { name: "NUL", raw: "a%00b", unstorable: true },
  { name: "invalid UTF-8", raw: "%FF%FE", unstorable: true },
  { name: "overlong encoding", raw: "%C0%AF", unstorable: true },
  { name: "lone surrogate", raw: "%ED%A0%80", unstorable: true },
];

/** What a database or runtime error looks like when it escapes to a client. */
const LEAK =
  /SQLSTATE|syntax error at or near|pq: |pgx|psycopg|asyncpg|Traceback|panic:|goroutine \d|invalid byte sequence|unterminated quoted/i;

const ZERO_ID = "00000000-0000-0000-0000-000000000000";

interface Answer {
  status: number;
  text: string;
  ms: number;
}

async function call(url: string, init: RequestInit): Promise<Answer> {
  for (let attempt = 0; ; attempt++) {
    const started = performance.now();
    const res = await fetch(url, { ...init, redirect: "manual" });
    const text = await res.text();
    const ms = performance.now() - started;
    if (res.status === 429 && attempt < 10) {
      await new Promise((r) => setTimeout(r, 500 * (attempt + 1)));
      continue;
    }
    return { status: res.status, text, ms };
  }
}

async function ownerSession(): Promise<{ token: string; projectId: string }> {
  const login = await fetch(`${controlPlaneURL}/api/v1/auth/dev/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email: "owner@demo.agenttwin.dev" }),
  });
  expect(login.status, "dev login").toBe(200);
  const { token } = (await login.json()) as { token: string };
  const projects = await fetch(`${controlPlaneURL}/api/v1/projects`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  const { items } = (await projects.json()) as { items: { id: string; slug: string }[] };
  const project = items.find((p) => p.slug === "support");
  expect(project, "the demo project").toBeDefined();
  return { token, projectId: project!.id };
}

function errorCode(text: string): string | undefined {
  try {
    return (JSON.parse(text) as { error?: { code?: string } }).error?.code;
  } catch {
    return undefined;
  }
}

test.describe("Security acceptance (spec §63)", () => {
  test("hostile text in a trace is shown as text and never runs", async ({ page }) => {
    const problems = watchConsole(page);
    const dialogs: string[] = [];
    page.on("dialog", (d) => {
      dialogs.push(d.message());
      void d.dismiss();
    });
    const hostile =
      `<img src=x onerror="window.__xss=1"><script>window.__xss=2</script>` +
      ` <a href="javascript:window.__xss=3">link</a> [md](javascript:window.__xss=4)` +
      ` {{constructor.constructor('window.__xss=5')()}} please refund my order`;
    const run = await runDemoAgent(hostile);

    await signIn(page, "owner@demo.agenttwin.dev", "/traces");
    const link = page.locator(`a[href^="/traces/${run.trace_id}"]`);
    await expect(link).toBeVisible({ timeout: 30_000 });
    await link.click();
    await expect(page.getByTestId("trace-id")).toHaveText(run.trace_id);
    await page
      .getByTestId("waterfall")
      .getByRole("button", { name: /^Agent run / })
      .first()
      .click();

    // The input is displayed, character for character, as text.
    const details = page.getByTestId("span-details");
    const input = details.locator("pre", { hasText: "<script>window.__xss=2</script>" });
    await expect(input).toBeVisible();
    await expect(input).toContainText(`<img src=x onerror="window.__xss=1">`);
    await expect(input).toContainText("javascript:window.__xss=3");

    // And none of it became markup, ran, or opened a dialog.
    expect(await page.evaluate(() => (window as unknown as { __xss?: unknown }).__xss)).toBeUndefined();
    await expect(page.locator('a[href^="javascript:" i]')).toHaveCount(0);
    await expect(page.locator('img[src="x"]')).toHaveCount(0);
    expect(
      await page.locator("script").evaluateAll((s) => s.some((e) => e.textContent?.includes("__xss"))),
    ).toBe(false);
    expect(dialogs).toEqual([]);
    expect(problems).toEqual([]);
  });

  test("every read endpoint answers injection payloads without a 5xx, a leak or a delay", async () => {
    test.setTimeout(300_000);
    const { token, projectId } = await ownerSession();
    const headers = { Authorization: `Bearer ${token}` };
    const reads = operations().filter((o) => o.method === "GET" && o.path.startsWith("/api/v1/"));
    expect(reads.length, "GET operations in the contracts").toBeGreaterThanOrEqual(50);

    const failures: string[] = [];
    let requests = 0;
    for (const op of reads) {
      const pathParams = op.params.filter((p) => p.in === "path");
      const queryParams = op.params.filter((p) => p.in === "query");
      const base = queryParams.some((p) => p.name === "project_id") ? `project_id=${projectId}` : "";
      const targets: { url: string; where: string }[] = [];
      for (const payload of PAYLOADS) {
        for (const p of pathParams) {
          let route = op.path;
          for (const q of pathParams) route = route.replace(`{${q.name}}`, q === p ? payload.raw : ZERO_ID);
          targets.push({ url: route + (base ? `?${base}` : ""), where: `path ${p.name}` });
        }
        let route = op.path;
        for (const q of pathParams) route = route.replace(`{${q.name}}`, ZERO_ID);
        for (const p of queryParams) {
          const query = [base && p.name !== "project_id" ? base : "", `${p.name}=${payload.raw}`]
            .filter(Boolean)
            .join("&");
          targets.push({ url: `${route}?${query}`, where: `query ${p.name}` });
        }
        for (const t of targets.splice(0)) {
          const a = await call(controlPlaneURL + t.url, { headers });
          requests++;
          const at = `${op.contract} GET ${op.path} (${t.where}, ${payload.name})`;
          if (a.status >= 500) failures.push(`${at}: ${a.status} ${a.text.slice(0, 200)}`);
          if (LEAK.test(a.text)) failures.push(`${at}: leaked ${a.text.slice(0, 200)}`);
          if (a.ms > 3_000) failures.push(`${at}: took ${Math.round(a.ms)} ms`);
          // Text no column can store is refused as such, before anything else.
          if (payload.unstorable && errorCode(a.text) !== "INVALID_TEXT")
            failures.push(`${at}: ${a.status} ${errorCode(a.text)}, want 400 INVALID_TEXT`);
        }
      }
    }
    expect(failures, `${failures.length} of ${requests} requests`).toEqual([]);

    // The statements did nothing: the demo project and its data are intact.
    const after = await call(`${controlPlaneURL}/api/v1/projects`, { headers });
    expect(after.status).toBe(200);
    expect(after.text).toContain(projectId);
  });

  test("every write endpoint refuses a NUL in its body before acting on it", async () => {
    test.setTimeout(120_000);
    const { token, projectId } = await ownerSession();
    const writes = operations().filter(
      (o) =>
        o.hasBody && o.method !== "GET" && (o.path.startsWith("/api/v1/") || o.path.startsWith("/gateway/")),
    );
    expect(writes.length, "write operations with a body").toBeGreaterThanOrEqual(40);
    const failures: string[] = [];
    const others: string[] = [];
    let refused = 0;
    for (const op of writes) {
      // The demo project for a project id, a well-formed id that exists nowhere otherwise.
      const route = op.path.replace(/\{([^}]+)\}/g, (_, name: string) =>
        name === "project_id" || op.path.startsWith("/api/v1/projects/{") ? projectId : ZERO_ID,
      );
      const query = op.params.some((p) => p.in === "query" && p.name === "project_id")
        ? `?project_id=${projectId}`
        : "";
      const a = await call(`${controlPlaneURL}${route}${query}`, {
        method: op.method,
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
        body: '{"probe":"a\\u0000b"}',
      });
      const at = `${op.contract} ${op.method} ${op.path}`;
      if (a.status >= 500 || LEAK.test(a.text)) failures.push(`${at}: ${a.status} ${a.text.slice(0, 200)}`);
      // A body of one unknown field is never accepted.
      if (a.status < 400) failures.push(`${at}: accepted with ${a.status}`);
      if (errorCode(a.text) === "INVALID_TEXT") refused++;
      else others.push(`${at}: ${a.status} ${errorCode(a.text)}`);
    }
    expect(failures).toEqual([]);
    // Every endpoint that reads its body before looking anything up refuses it.
    // The others looked up a path id first (the id exists nowhere: 404, or is
    // not a trace id: 400), validate a manifest (400 INVALID_MANIFEST), or
    // take the agent's API key, not a person's session (403).
    expect(
      refused,
      `endpoints that refused the NUL; the others:\n${others.join("\n")}`,
    ).toBeGreaterThanOrEqual(30);
  });
});
