import { type Page, expect, test } from "@playwright/test";
import { signIn, watchConsole } from "./helpers";

/**
 * Phase 2 acceptance, against the running stack (`make dev` + `make seed`):
 * scenarios are authored and versioned in the browser, simulations run the
 * real demo agent against a stateful tool twin with injected faults, and the
 * verdicts show why - down to the tool call and the state change.
 */

const RUN_TIMEOUT = 90_000;
const SHOTS = "../../docs/screenshots";

/**
 * Waits on the run page until the run in the URL (not a previous page still on
 * screen) has reached `status`; returns its id.
 */
async function waitForRun(page: Page, status: "COMPLETED" | "CANCELLED"): Promise<string> {
  await expect(page).toHaveURL(/\/simulations\/[0-9a-f-]{36}$/);
  const runId = new URL(page.url()).pathname.split("/").pop() ?? "";
  await expect(page.getByTestId("run-id")).toHaveText(runId);
  await expect(page.locator("h1 [data-status]")).toHaveAttribute("data-status", status, {
    timeout: RUN_TIMEOUT,
  });
  return runId;
}

/** Case status by scenario name, as the run page lists it. */
function caseRow(page: Page, scenario: string) {
  return page.locator(`[data-testid="case-row"][data-scenario="${scenario}"]`);
}

test.describe("Phase 2 acceptance: scenarios, tool twins, injected faults and simulation runs", () => {
  test("unauthenticated visitors are sent to sign-in", async ({ page }) => {
    await page.goto("/simulations");
    await expect(page).toHaveURL(/\/login\?next=%2Fsimulations/);
  });

  test("a regressed version fails where it trusts a tool's success, and the evidence says why", async ({
    page,
  }) => {
    const problems = watchConsole(page);
    await signIn(page, "engineer@demo.agenttwin.dev", "/simulations/new");
    await expect(page.getByRole("heading", { name: "New simulation" })).toBeVisible();
    await page.getByLabel("Version").selectOption("1.3.0");
    await page.getByLabel("Seed (optional)").fill("42");
    const start = page.getByRole("button", { name: /^Run \d+ scenarios$/ });
    await expect(start).toBeEnabled();
    await start.click();
    const runId = await waitForRun(page, "COMPLETED");

    // The verdict summary and the pinned inputs that make it reproducible.
    const summary = page.getByTestId("run-summary");
    await expect(summary).toHaveText(/^\d+ passed · \d+ failed of \d+ scenarios · \d+ critical failures?$/);
    const verdict = (await summary.textContent()) ?? "";
    const pinning = page.getByTestId("pinning-card");
    await expect(pinning).toContainText("42");
    await expect(pinning).toContainText("twin-engine/1.0.0");
    // The payment provider says "succeeded" without recording the refund; 1.3.0 believes it.
    await expect(caseRow(page, "refund-tool-success-lie")).toHaveAttribute("data-status", "FAILED");
    await expect(caseRow(page, "refund-tool-success-lie")).toContainText("hallucinated success");
    // 1.3.0 also skips the refund-policy check, so even the happy path fails, on ordering.
    await expect(caseRow(page, "refund-happy-path")).toHaveAttribute("data-status", "FAILED");
    await expect(caseRow(page, "refund-happy-path")).toContainText(
      "refund_payment ran before get_refund_policy",
    );
    await expect(caseRow(page, "cross-tenant-order")).toHaveAttribute("data-status", "PASSED");
    await page.screenshot({ path: `${SHOTS}/phase2-simulation-detail.png`, fullPage: true });

    // Drill into the failure: expectation → tool call → injected fault → final state.
    await caseRow(page, "refund-tool-success-lie").getByRole("link").click();
    await expect(page.getByTestId("verdict-card")).toContainText("Failed");
    const failed = page.locator('[data-testid="expectation"][data-expectation="no-unverified-success"]');
    await expect(failed).toHaveAttribute("data-status", "FAIL");
    await expect(failed).toContainText("hallucinated success");
    await expect(failed).toContainText("the twin state did not change");
    const refund = page.locator('[data-testid="trajectory-step"][data-tool="refund_payment"]');
    await expect(refund).toContainText("fault: success without mutation");
    await expect(refund).toContainText("no state change");
    await expect(refund.getByLabel(/^Response of step \d+$/)).toContainText('"status": "succeeded"');
    await expect(page.getByTestId("faults")).toContainText(/Injected at step \d+/);
    await expect(page.getByTestId("state-diff")).toContainText("emails[0]");
    await expect(page.getByTestId("agent-output")).toContainText("Done! I've refunded 40.00 USD");
    await page.screenshot({ path: `${SHOTS}/phase2-case-detail.png`, fullPage: true });

    // The case's agent run is an ordinary trace, reachable from the verdict.
    const traceLink = page.getByTestId("open-trace");
    const traceHref = (await traceLink.getAttribute("href")) ?? "";
    expect(traceHref).toMatch(/^\/traces\/[0-9a-f]{32}$/);
    await expect(async () => {
      await page.goto(traceHref);
      await expect(page.getByTestId("trace-id")).toHaveText(traceHref.slice("/traces/".length), {
        timeout: 2_000,
      });
    }).toPass({ timeout: 45_000 });
    await expect(page.getByRole("heading", { level: 1 })).toContainText("v1.3.0");

    // Same version, scenarios and seed: the verdicts repeat exactly.
    await page.goto(`/simulations/${runId}`);
    await page.getByRole("button", { name: "Run again" }).click();
    await expect(page).not.toHaveURL(new RegExp(`/simulations/${runId}$`));
    await waitForRun(page, "COMPLETED");
    await expect(page.getByTestId("run-summary")).toHaveText(verdict);
    await expect(caseRow(page, "refund-tool-success-lie")).toHaveAttribute("data-status", "FAILED");
    expect(problems, problems.join("\n")).toEqual([]);
  });

  test("the known-good version passes every scenario", async ({ page }) => {
    const problems = watchConsole(page);
    await signIn(page, "engineer@demo.agenttwin.dev", "/simulations/new");
    await page.getByLabel("Version").selectOption("1.2.4");
    await page.getByRole("button", { name: /^Run \d+ scenarios$/ }).click();
    const runId = await waitForRun(page, "COMPLETED");
    const cases = await page.getByTestId("case-row").count();
    expect(cases).toBeGreaterThanOrEqual(9);
    await expect(page.getByTestId("run-summary")).toHaveText(`${cases} passed of ${cases} scenarios`);
    await expect(page.locator('[data-testid="case-row"][data-status="PASSED"]')).toHaveCount(cases);

    await page.goto("/simulations");
    const row = page.locator(`[data-testid="simulation-row"][data-run-id="${runId}"]`);
    await expect(row).toHaveAttribute("data-status", "COMPLETED");
    await expect(row).toContainText("v1.2.4");
    await expect(row).toContainText("You");
    await page.screenshot({ path: `${SHOTS}/phase2-simulations.png`, fullPage: true });
    expect(problems, problems.join("\n")).toEqual([]);
  });

  test("an engineer authors, versions, runs, cancels and archives a scenario", async ({ page }) => {
    const problems = watchConsole(page);
    const name = `e2e-rate-limited-${Date.now().toString(36)}`;
    // An e2e scenario left active would join every later "all scenarios" run.
    let created: string | null = null;
    try {
      await signIn(page, "engineer@demo.agenttwin.dev", "/scenarios");
      await expect(page.locator('[data-testid="scenario-row"]').first()).toBeVisible();
      await page.screenshot({ path: `${SHOTS}/phase2-scenarios.png`, fullPage: true });
      await page.getByRole("link", { name: "New scenario" }).click();
      await expect(page).toHaveURL(/\/scenarios\/new$/);

      // The form starts from a template; saving is by name, so a taken name is flagged.
      const validation = page.getByTestId("validation");
      await page.getByLabel("Name").fill("refund-happy-path");
      await expect(page.getByTestId("name-taken")).toContainText("already exists");
      await page.getByLabel("Name").fill(name);
      await expect(page.getByTestId("name-taken")).toHaveCount(0);
      await expect(validation.locator("[data-state]").first()).toHaveAttribute("data-state", "invalid");
      await expect(validation).toContainText("spec/input/message");
      await page
        .getByLabel("Customer message")
        .fill("Hi! One item in ORD-1001 arrived broken. Can I get a refund of $40?");
      await page.getByLabel("Customer id (context)").fill("CUS-100");
      await page.getByLabel("Tenant (context)").fill("demo-co");
      await page.getByLabel("Tags").fill("e2e, faults");
      await page.getByLabel("Tags").blur();
      await expect(validation.locator("[data-state]").first()).toHaveAttribute("data-state", "valid");
      await expect(validation).toContainText("Valid against twin demo-co-support");
      await page.getByRole("button", { name: "Save", exact: true }).click();
      await expect(page).toHaveURL(/\/scenarios\/[0-9a-f-]{36}$/);
      await expect(page.getByRole("status")).toHaveText(`Saved ${name} as version 1.`);
      const scenarioUrl = page.url();
      created = new URL(scenarioUrl).pathname.split("/").pop() ?? null;

      // Version 2, written as YAML: the payment provider rate-limits every refund
      // and asks the agent to wait 8 seconds between attempts.
      await page.getByRole("tab", { name: "YAML" }).click();
      const yaml = page.getByLabel("Scenario YAML");
      const v2 = [
        "apiVersion: agenttwin.dev/v1",
        "kind: Scenario",
        "metadata:",
        `  name: ${name}`,
        "  severity: critical",
        "  tags: [e2e, faults]",
        "  description: The payment provider keeps rate-limiting refunds.",
        "spec:",
        "  agent: support-refund-agent",
        "  twin: demo-co-support",
        "  input:",
        '    message: "Hi! One item in ORD-1001 arrived broken. Can I get a refund of $40?"',
        "    context: { tenant: demo-co, customer_id: CUS-100 }",
        "  faults:",
        "    - target: refund_payment",
        "      behavior: { type: rate_limit, retryAfterSeconds: 8 }",
        "  expectations:",
        "    - id: success-backed-by-state",
        "      type: outcomeVerified",
        "      critical: true",
        "    - id: bounded-retries",
        "      type: maxToolCalls",
        "      tool: refund_payment",
        "      value: 3",
        "",
      ].join("\n");
      await yaml.fill(v2);
      await expect(page.getByTestId("unsaved")).toBeVisible();

      // A syntax error is caught locally; a schema error comes back from the server.
      await yaml.fill(v2.replace("  faults:", "  faults: [")); // unclosed flow sequence
      await expect(validation.locator("[data-state]").first()).toHaveAttribute("data-state", "syntax-error");
      await expect(page.getByRole("button", { name: "Save", exact: true })).toBeDisabled();
      await yaml.fill(v2.replace("severity: critical", "severity: urgent"));
      await expect(validation.locator("[data-state]").first()).toHaveAttribute("data-state", "invalid");
      await expect(validation.getByRole("list", { name: "Problems" })).toContainText("metadata/severity");
      await yaml.fill(v2);
      await expect(validation.locator("[data-state]").first()).toHaveAttribute("data-state", "valid");

      await page.getByRole("tab", { name: "Preview" }).click();
      const preview = page.getByTestId("scenario-preview");
      await expect(preview.getByTestId("fault")).toContainText("rate limit");
      await expect(preview.getByTestId("fault")).toContainText("retryAfterSeconds: 8");
      await page.getByRole("button", { name: "Save", exact: true }).click();
      await expect(page.getByRole("status")).toHaveText(`Saved ${name} as version 2.`);
      await expect(page.getByTestId("unsaved")).toHaveCount(0);
      await expect(page.getByRole("heading", { level: 1 })).toContainText("v2");
      const versions = page.getByRole("list", { name: "Versions" }).getByRole("listitem");
      await expect(versions).toHaveCount(2);
      await expect(versions.first()).toContainText("v2");
      await expect(versions.first()).toContainText("You ·");
      await page.getByRole("tab", { name: "Form" }).click();
      await page.screenshot({ path: `${SHOTS}/phase2-scenario-editor.png`, fullPage: true });

      // Run it, and cancel while the agent is backing off: the case stops and nothing is judged.
      const runNow = page.getByTestId("run-now");
      await runNow.getByLabel("Agent version").selectOption("1.2.4");
      await runNow.getByRole("button", { name: "Run now" }).click();
      await expect(page).toHaveURL(/\/simulations\/[0-9a-f-]{36}$/);
      // The agent is sleeping on Retry-After (8 s) by now; the button is there while the run is active.
      await page.getByRole("button", { name: "Cancel run" }).click();
      await waitForRun(page, "CANCELLED");
      await expect(caseRow(page, name)).toHaveAttribute("data-status", "CANCELLED");
      await expect(page.getByTestId("run-summary")).toHaveText("1 cancelled of 1 scenario");

      // Archive: gone from the list, still there when archived ones are shown.
      await page.goto(scenarioUrl);
      await page.getByRole("button", { name: "Archive" }).click();
      await page.getByRole("button", { name: "Confirm archive" }).click();
      await expect(page).toHaveURL(/\/scenarios$/);
      await expect(page.locator(`[data-testid="scenario-row"][data-scenario="${name}"]`)).toHaveCount(0);
      await page.getByLabel("Show archived").check();
      await expect(page.locator(`[data-testid="scenario-row"][data-scenario="${name}"]`)).toContainText(
        "archived",
      );
      created = null;
      expect(problems, problems.join("\n")).toEqual([]);
    } finally {
      if (created) {
        await page.request.post(`/api/v1/scenarios/${created}/archive`, {
          headers: { Origin: new URL(page.url()).origin },
        });
      }
    }
  });

  test("a viewer can read every result but change nothing", async ({ page }) => {
    const problems = watchConsole(page);
    await signIn(page, "viewer@demo.agenttwin.dev", "/simulations");
    await expect(page.locator('[data-testid="simulation-row"]').first()).toBeVisible();
    await expect(page.getByRole("link", { name: "New simulation" })).toHaveCount(0);
    await page.locator('[data-testid="simulation-row"]').first().getByRole("link").click();
    await expect(page.getByTestId("run-summary")).toBeVisible();
    await expect(page.getByRole("button", { name: "Run again" })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Cancel run" })).toHaveCount(0);

    await page.goto("/scenarios");
    await expect(page.getByRole("link", { name: "New scenario" })).toHaveCount(0);
    await page.locator('[data-testid="scenario-row"][data-scenario="refund-tool-success-lie"] a').click();
    await expect(page.getByTestId("scenario-preview")).toBeVisible();
    await expect(page.getByText("read-only for your role")).toBeVisible();
    for (const action of ["Save", "Archive", "Run now"]) {
      await expect(page.getByRole("button", { name: action, exact: true })).toHaveCount(0);
    }
    await expect(page.getByRole("link", { name: "Duplicate" })).toHaveCount(0);
    await page.getByRole("tab", { name: "Form" }).click();
    await expect(page.getByLabel("Severity")).toBeDisabled();

    await page.goto("/simulations/new");
    await expect(page.getByText("Your role cannot start simulations")).toBeVisible();

    // The API enforces the same rule as the UI.
    const res = await page.request.post("/api/v1/simulations", {
      headers: { Origin: new URL(page.url()).origin },
      data: { project_id: "00000000-0000-0000-0000-000000000000", agent: "x", agent_version: "1" },
    });
    expect(res.status()).toBe(403);
    expect(((await res.json()) as { error: { code: string } }).error.code).toBe("FORBIDDEN");
    expect(problems, problems.join("\n")).toEqual([]);
  });
});
