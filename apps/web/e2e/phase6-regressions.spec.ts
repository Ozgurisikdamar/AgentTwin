import { type Page, expect, test } from "@playwright/test";
import { signIn, watchConsole } from "./helpers";

/**
 * Phase 6 acceptance, against the running stack (`make dev` + `make seed`):
 * a production failure becomes a regression test and a later release runs
 * it on its own. The seed sends a canary incident on 1.3.0 — a refund whose
 * payment times out after the money moved, retried without an idempotency
 * key: the customer is refunded twice — and the miner groups it. A reviewer
 * inspects it and its representative trace and promotes it (spec §64
 * "Regression promotion"); the case appears in production-regressions; a
 * release of 1.3.1 then selects it as a known regression, 1.3.1 passes it
 * and the regression is fixed in 1.3.1.
 */

const SHOTS = "../../docs/screenshots";
const TITLE = "refund_payment took effect twice";

/** What the first test learned, for the second. */
const found: { regressionId?: string; scenario?: string } = {};

/** The most recently seen group of the incident (the seed just added to it). */
function incidentRow(page: Page) {
  return page
    .getByTestId("regression-row")
    .filter({ has: page.getByRole("link", { name: `Regression: ${TITLE}` }) })
    .filter({ hasText: "v1.3.0" })
    .first();
}

test.describe.serial("Phase 6 acceptance: production failures become regression tests", () => {
  test("a reviewer inspects the seeded incident and promotes it into production-regressions", async ({
    page,
  }) => {
    test.setTimeout(180_000);
    const problems = watchConsole(page);
    await signIn(page, "reviewer@demo.agenttwin.dev", "/regressions?view=all");
    const row = incidentRow(page);
    await expect(row).toBeVisible();
    await expect(row.getByRole("cell").nth(2)).toHaveText("critical");
    await expect(row.getByRole("cell").nth(3)).toHaveText("Duplicate side effect");
    await expect(row.getByRole("cell").nth(6)).toHaveText("refund_payment");
    await page.screenshot({ path: `${SHOTS}/phase6-regressions.png`, fullPage: true });

    // The failure, what shows it, and its representative trace.
    await row.getByRole("link", { name: `Regression: ${TITLE}` }).click();
    await expect(page).toHaveURL(/\/regressions\/[0-9a-f-]{36}$/);
    found.regressionId = page.url().split("/").pop()!;
    const header = page.getByTestId("regression-header");
    await expect(header).toContainText(TITLE);
    await expect(page.getByRole("list", { name: "Evidence" })).toContainText(
      "refund_payment took effect more than once",
    );
    await expect(page.getByRole("list", { name: "Evidence" })).toContainText(
      "a write was retried without an idempotency key",
    );
    const failure = page.getByTestId("occurrence-row").first();
    await expect(failure).toContainText("it took an irreversible action twice");
    await page.screenshot({ path: `${SHOTS}/phase6-regression.png`, fullPage: true });

    await page.getByRole("link", { name: "Open the representative trace" }).click();
    await expect(page).toHaveURL(/\/traces\/[0-9a-f]{32}$/);
    await expect(page.getByRole("heading", { level: 1 })).toContainText("support-refund-agent");
    await expect(page.getByRole("heading", { level: 1 })).toContainText("v1.3.0");
    await expect(page.getByTestId("waterfall").getByTestId("waterfall-row").first()).toBeVisible();
    await page.goBack();
    await expect(header).toContainText(TITLE);

    // Promote it (a rerun finds it promoted already, or fixed).
    await page.getByRole("tab", { name: "Regression test" }).click();
    const promote = page.getByRole("form", { name: "Promote to a regression test" });
    const test_ = page.getByTestId("regression-test");
    await expect(promote.or(test_)).toBeVisible();
    if (await promote.isVisible()) {
      await expect(page.getByTestId("draft-state")).toHaveText("Complete");
      await expect(promote.getByLabel("Scenario (YAML)")).toHaveValue(/type: noDuplicateSideEffect/);
      await expect(promote.getByLabel("Scenario (YAML)")).toHaveValue(/type: timeout_after_mutation/);
      await page.screenshot({ path: `${SHOTS}/phase6-regression-draft.png`, fullPage: true });
      await promote.getByRole("button", { name: /Promote to test/ }).click();
      await expect(test_).toContainText("Promoted to a regression test");
    }
    await expect(test_).toContainText("production-regressions");
    const scenarioLink = test_.getByRole("link", { name: /^regression-refund-payment-took-effect-twice-/ });
    found.scenario = (await scenarioLink.textContent())!.trim();
    await page.screenshot({ path: `${SHOTS}/phase6-regression-promoted.png`, fullPage: true });

    // The regression case appears in the dataset every release of the agent runs.
    await test_.getByRole("link", { name: "production-regressions" }).click();
    await expect(page).toHaveURL(/\/datasets\/[0-9a-f-]{36}$/);
    const kase = page.getByTestId("dataset-case").filter({ hasText: found.scenario });
    await expect(kase).toBeVisible();
    await expect(kase).toContainText("Production regression");
    await expect(kase).toContainText("redacted");
    await page.screenshot({ path: `${SHOTS}/phase6-regression-dataset.png`, fullPage: true });
    expect(problems).toEqual([]);
  });

  test("the next release runs it as a known regression, and the fix fixes it", async ({ page }) => {
    test.setTimeout(900_000);
    test.skip(!found.regressionId || !found.scenario, "needs the promoted regression");
    const problems = watchConsole(page);
    await signIn(page, "engineer@demo.agenttwin.dev", "/releases?new=1");
    const form = page.getByRole("form", { name: "New release" });
    await form.getByLabel("Agent").selectOption("support-refund-agent");
    await expect(form.getByRole("option", { name: "1.3.1" }).first()).toBeAttached();
    await form.getByLabel("Baseline (in use)").selectOption("1.2.4");
    await form.getByLabel("Candidate").selectOption("1.3.1");
    await form.getByLabel("Title (optional)").fill("1.3.1 with the production regression (e2e)");
    await form.getByRole("button", { name: /Create and evaluate/ }).click();
    await expect(page).toHaveURL(/\/releases\/[0-9a-f-]{36}$/);
    const header = page.getByTestId("release-header");
    // Both versions run the suite, the known regression with it.
    await expect(header.getByTestId("gate-outcome")).toHaveText(/^(PASS|WARN|BLOCK)$/, { timeout: 840_000 });
    await expect(header.getByTestId("gate-outcome")).toHaveText(/^(PASS|WARN)$/);
    await page.getByRole("tab", { name: "Simulations" }).click();
    const known = page.getByTestId("suite-row").filter({ hasText: found.scenario! });
    await expect(known).toContainText("known regression");
    await page.screenshot({ path: `${SHOTS}/phase6-release-known-regression.png`, fullPage: true });

    // 1.3.1 passed the test made from 1.3.0's failure: fixed in 1.3.1.
    await page.goto(`/regressions/${found.regressionId}?tab=test`);
    const regression = page.getByTestId("regression-header");
    await expect(regression).toContainText("Fixed");
    const test_ = page.getByTestId("regression-test");
    await expect(test_).toContainText("Fixed in v1.3.1");
    await expect(test_.getByRole("link", { name: /evaluation run/ })).toBeVisible();
    await page.getByRole("tab", { name: "History" }).click();
    const events = page.getByTestId("regression-event");
    await expect(events.filter({ hasText: "Promoted to the regression test" })).toHaveCount(1);
    await expect(events.filter({ hasText: "Fixed in 1.3.1" })).toHaveCount(1);
    await page.screenshot({ path: `${SHOTS}/phase6-regression-fixed.png`, fullPage: true });
    expect(problems).toEqual([]);
  });
});
