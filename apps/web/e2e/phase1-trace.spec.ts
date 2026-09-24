import { expect, test } from "@playwright/test";
import { createOrder, runDemoAgent, signIn, watchConsole } from "./helpers";

test.describe("Phase 1 acceptance: a demo agent run is visible in the browser", () => {
  test("unauthenticated visitors are sent to sign-in", async ({ page }) => {
    await page.goto("/traces?agent=support-refund-agent");
    await expect(page).toHaveURL(/\/login\?next=%2Ftraces%3Fagent%3Dsupport-refund-agent/);
    await expect(page.getByRole("heading", { name: "Sign in to AgentTwin" })).toBeVisible();
  });

  test("demo agent trace: explorer → detail → waterfall → span details", async ({ page }) => {
    const problems = watchConsole(page);
    const order = await createOrder(140);
    const run = await runDemoAgent(`Hi! One item in ${order} arrived broken. Can I get a refund of $40?`);
    expect(run.trace_id).toMatch(/^[0-9a-f]{32}$/);
    // 1.2.4 re-reads the order after the refund to confirm it was recorded.
    expect(run.tool_calls.map((c) => c.name)).toEqual([
      "lookup_order",
      "get_refund_policy",
      "refund_payment",
      "lookup_order",
      "send_email",
    ]);

    await signIn(page, "owner@demo.agenttwin.dev", "/traces");
    await expect(page.getByTestId("current-user")).toHaveText("Olivia Owner");

    // The run appears in the explorer (live refresh) and links to the detail page.
    const link = page.locator(`a[href^="/traces/${run.trace_id}"]`);
    await expect(link).toBeVisible({ timeout: 30_000 });
    await link.click();
    await expect(page.getByTestId("trace-id")).toHaveText(run.trace_id);
    await expect(page.getByRole("heading", { level: 1 })).toContainText("support-refund-agent");
    await expect(page.getByRole("heading", { level: 1 })).toContainText("v1.2.4");

    // Waterfall: the agent run, model calls and the five tool calls.
    const waterfall = page.getByTestId("waterfall");
    await expect(waterfall.getByTestId("waterfall-row").first()).toBeVisible();
    // Accessible row labels carry the risk tier of mutating tools.
    const tools: [string, string, number][] = [
      ["lookup_order", "", 2],
      ["get_refund_policy", "", 1],
      ["refund_payment", ", write irreversible risk", 1],
      ["send_email", ", write reversible risk", 1],
    ];
    for (const [tool, risk, count] of tools) {
      const rows = waterfall.getByRole("button", {
        name: new RegExp(`^Tool call execute_tool ${tool}${risk}, ok`),
      });
      await expect(rows).toHaveCount(count);
      await expect(rows.first()).toBeVisible();
    }
    // Duration labels stay inside the timeline column (the root bar spans the trace).
    await expect(waterfall.getByTestId("waterfall-row").first().locator("[data-placement]")).toHaveAttribute(
      "data-placement",
      "inside",
    );
    await expect(waterfall.locator('[data-kind="model"]').first()).toBeVisible();
    await expect(waterfall.locator('[data-kind="agent"]')).toHaveCount(1);

    // Selecting the irreversible refund shows its risk and idempotency evidence.
    await waterfall.getByRole("button", { name: /^Tool call execute_tool refund_payment/ }).click();
    const details = page.getByTestId("span-details");
    await expect(details.getByRole("heading", { name: "execute_tool refund_payment" })).toBeVisible();
    await expect(details.getByText("WRITE IRREVERSIBLE")).toBeVisible();
    await expect(details.getByText("Idempotency key")).toBeVisible();

    // The agent's self-reported outcome is shown as a claim until evidence arrives.
    const outcome = page.getByTestId("outcome-card");
    await expect(outcome).toContainText("Success");
    const summary = page.getByTestId("summary-card");
    await expect(summary).toContainText(
      "lookup_order>get_refund_policy>refund_payment>lookup_order>send_email",
    );
    // Re-reading the order to confirm the refund is a new call, not a retry.
    await expect(summary.locator("dt", { hasText: /^Retries$/ }).locator("+ dd")).toHaveText("0");
    await expect(summary.getByText("retry", { exact: true })).toHaveCount(0);
    await expect(page.getByRole("tab", { name: "Retries (0)" })).toBeVisible();
    // The scripted planner reports deterministic token estimates for every call.
    await expect(summary).not.toContainText("0 in · 0 out");
    await expect(page.getByText("production environment")).toBeVisible();
    await expect(page.getByText("live traffic")).toBeVisible();

    await page.screenshot({ path: "../../docs/screenshots/phase1-trace-detail.png", fullPage: true });
    expect(problems, problems.join("\n")).toEqual([]);
  });

  test("explorer filters by version and outcome from the URL", async ({ page }) => {
    const problems = watchConsole(page);
    await signIn(page, "viewer@demo.agenttwin.dev", "/traces?agent_version=1.2.4&status=OK");
    await expect(page.getByLabel("Version")).toHaveValue("1.2.4");
    await expect(page.getByTestId("trace-row").first()).toBeVisible();
    const versions = await page.getByTestId("trace-row").locator("text=/^v\\d/").allTextContents();
    expect(versions.length).toBeGreaterThan(0);
    expect(new Set(versions)).toEqual(new Set(["v1.2.4"]));
    await page.getByRole("button", { name: /Clear 2 filters/ }).click();
    await expect(page).toHaveURL(/\/traces$/);
    expect(problems, problems.join("\n")).toEqual([]);
  });

  test("overview summarizes production health and signing out ends the session", async ({
    page,
    context,
  }) => {
    const problems = watchConsole(page);
    await signIn(page, "engineer@demo.agenttwin.dev", "/overview");
    await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
    await expect(page.getByText("Agent runs")).toBeVisible();
    await expect(page.getByRole("heading", { name: "Agent versions" })).toBeVisible();
    await page.screenshot({ path: "../../docs/screenshots/phase1-overview.png", fullPage: true });

    await page.getByRole("button", { name: "Sign out" }).click();
    await expect(page).toHaveURL(/\/login/);
    const cookies = await context.cookies();
    expect(cookies.find((c) => c.name === "agenttwin_session")).toBeUndefined();
    await page.goto("/traces");
    await expect(page).toHaveURL(/\/login/);
    expect(problems, problems.join("\n")).toEqual([]);
  });

  test("the session cookie is HttpOnly and the API rejects cross-site writes", async ({
    page,
    context,
    request,
  }) => {
    await signIn(page);
    const session = (await context.cookies()).find((c) => c.name === "agenttwin_session");
    expect(session?.httpOnly).toBe(true);
    expect(session?.sameSite).toBe("Lax");
    expect(await page.evaluate(() => document.cookie)).not.toContain("agenttwin_session");
    const res = await request.post("/api/v1/projects", {
      headers: {
        Cookie: `agenttwin_session=${session?.value}`,
        Origin: "http://evil.example",
        "Content-Type": "application/json",
      },
      data: { slug: "evil", name: "Evil" },
    });
    expect(res.status()).toBe(403);
  });
});
