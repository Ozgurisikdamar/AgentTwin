import { type Page, expect, test } from "@playwright/test";
import { signIn, watchConsole } from "./helpers";

/**
 * Phase 5 acceptance, against the running stack (`make dev` + `make seed`):
 * a bad candidate is BLOCKED (golden path steps 6-8), the release page says
 * why with the evidence (the rule, the scenario, the candidate against the
 * baseline, the first divergence and the trace), the decision is hashed and
 * verifies, and an override never turns it into a pass (spec §92).
 */

const SHOTS = "../../docs/screenshots";
const AGENT = "support-refund-agent";

function suffix(): string {
  return Math.random().toString(36).slice(2, 8);
}

/**
 * The row of the seed's release of a candidate version (`make seed` titles it
 * "1.2.4 -> <candidate> (demo)"). Not simply the newest release of that
 * version: another test, or the CLI, creates and overrides releases of the
 * same versions, and they sort first.
 */
function releaseRow(page: Page, candidate: string) {
  return page
    .getByTestId("release-row")
    .filter({ has: page.getByRole("link", { name: new RegExp(`to ${candidate.replace(/\./g, "\\.")}$`) }) })
    .filter({ hasText: `1.2.4 -> ${candidate} (demo)` })
    .first();
}

test.describe("Phase 5 acceptance: the release gate", () => {
  test("the seeded bad candidate is BLOCKED and its fix is not", async ({ page }) => {
    const problems = watchConsole(page);
    await signIn(page, "engineer@demo.agenttwin.dev", "/releases");
    const bad = releaseRow(page, "1.3.0");
    const fixed = releaseRow(page, "1.3.1");
    await expect(bad.getByTestId("gate-outcome")).toHaveText("BLOCK");
    await expect(fixed.getByTestId("gate-outcome")).toHaveText(/^(PASS|WARN)$/);
    // Critical failures and impacted scenarios come from the decision.
    const cells = bad.getByRole("cell");
    await expect(cells.nth(4)).toHaveText(/^[1-9]\d*$/);
    await expect(cells.nth(5)).toHaveText(/^[1-9]\d*$/);
    await expect(fixed.getByRole("cell").nth(4)).toHaveText("0");
    await page.screenshot({ path: `${SHOTS}/phase5-releases.png`, fullPage: true });

    // Why it is blocked, and every rule's exact evidence.
    await bad.getByRole("link", { name: /to 1\.3\.0$/ }).click();
    await expect(page).toHaveURL(/\/releases\/[0-9a-f-]{36}$/);
    const header = page.getByTestId("release-header");
    await expect(header).toContainText(AGENT);
    await expect(header.getByTestId("gate-outcome")).toHaveText("BLOCK");
    const why = page.getByTestId("why");
    await expect(why.getByRole("heading", { name: "Why it is blocked" })).toBeVisible();
    await expect(why.getByRole("list", { name: "What the evaluation found" })).toContainText(
      /new critical failure/,
    );
    const rules = why.getByTestId("why-rule");
    await expect(rules.filter({ hasText: "duplicate_side_effect" })).toContainText(
      "refund-timeout-after-mutation",
    );
    await expect(rules.filter({ hasText: "unverified_success" })).toContainText("refund-tool-success-lie");
    await expect(page.getByText(/^exit code 3 \(fails the job\)$/)).toBeVisible();
    await page.screenshot({ path: `${SHOTS}/phase5-release-blocked.png`, fullPage: true });

    await page.getByRole("tab", { name: "Evals" }).click();
    const twice = page.getByTestId("rule-evidence").filter({ hasText: "duplicate_side_effect" });
    await expect(twice).toContainText("Rule: An irreversible action must not take effect more than once.");
    await expect(twice).toContainText(/The side effect refund:ORD-\d+ was applied 2 times\./);
    await expect(twice).toContainText("First divergence");
    await expect(twice.getByRole("link", { name: /Candidate trace/ })).toBeVisible();
    await page.screenshot({ path: `${SHOTS}/phase5-release-evidence.png`, fullPage: true });

    // The decision is hashed and still verifies.
    await page.getByRole("tab", { name: "Evidence" }).click();
    await expect(page.getByTestId("evidence-hash")).toHaveText(/^[0-9a-f]{64}$/);
    await expect(page.getByTestId("evidence-verified")).toContainText("Verified");

    // The comparison behind the evidence opens from it.
    await page.getByRole("tab", { name: "Evals" }).click();
    await twice.getByRole("link", { name: "refund-timeout-after-mutation" }).click();
    await expect(page).toHaveURL(/\/evaluations\/[0-9a-f-]{36}\/cases\/refund-timeout-after-mutation$/);
    await expect(page.getByRole("heading", { level: 1 })).toContainText("refund-timeout-after-mutation");
    expect(problems).toEqual([]);
  });

  test("a release created in the UI is gated, then overridden without becoming a pass", async ({ page }) => {
    test.setTimeout(600_000);
    const problems = watchConsole(page);
    const title = `e2e release ${suffix()}`;
    await signIn(page, "engineer@demo.agenttwin.dev", "/releases");
    await page.getByRole("button", { name: "New release" }).click();
    const form = page.getByRole("form", { name: "New release" });
    await form.getByLabel("Agent").selectOption(AGENT);
    await expect(form.getByLabel("Candidate").locator("option", { hasText: "1.3.0" })).toHaveCount(1);
    await form.getByLabel("Candidate").selectOption("1.3.0");
    await form.getByLabel("Baseline (in use)").selectOption("1.2.4");
    await form.getByLabel("Title (optional)").fill(title);
    await form.getByRole("button", { name: "Create and evaluate" }).click();
    await expect(page).toHaveURL(/\/releases\/[0-9a-f-]{36}$/);
    const url = page.url();
    await expect(page.getByText(title)).toBeVisible();

    // It evaluates, then blocks: the page follows the gate on its own.
    const outcome = page.getByTestId("release-header").getByTestId("gate-outcome");
    await expect(outcome).toHaveText("BLOCK", { timeout: 540_000 });
    await expect(page.getByRole("heading", { name: "Why it is blocked" })).toBeVisible();
    // An engineer cannot override.
    await expect(page.getByRole("button", { name: /^Override$/ })).toHaveCount(0);

    // A reviewer can, with a reason; the decision stays BLOCK.
    await page.context().clearCookies();
    await signIn(page, "reviewer@demo.agenttwin.dev", new URL(url).pathname);
    await page.getByRole("button", { name: /^Override$/ }).click();
    const override = page.getByRole("form", { name: "Override the gate" });
    await override.getByLabel("Reason").fill("too short");
    await override.getByRole("button", { name: /Override BLOCK/ }).click();
    await expect(override.getByText(/at least 10 characters/)).toBeVisible();
    await override
      .getByLabel("Reason")
      .fill("Hotfix for the refund outage; reviewed with the payments team.");
    await override.getByLabel("Ticket URL (optional)").fill("https://tickets.example.com/OPS-12");
    await override.getByRole("button", { name: /Override BLOCK/ }).click();
    const banner = page.getByTestId("override-banner");
    await expect(banner).toContainText("Originally BLOCKED · overridden");
    await expect(banner).toContainText("Hotfix for the refund outage; reviewed with the payments team.");
    await expect(page.getByTestId("release-header")).toContainText("originally BLOCK");
    await expect(page.getByTestId("release-header").getByTestId("gate-outcome")).toHaveText("Overridden");
    await expect(page.getByRole("heading", { name: "Why it is blocked" })).toBeVisible();
    await expect(page.getByText(/^exit code 0 \(the job passes\)$/)).toBeVisible();
    await expect(page.getByRole("button", { name: /^Override$/ })).toHaveCount(0);
    await page.screenshot({ path: `${SHOTS}/phase5-release-overridden.png`, fullPage: true });

    // The list says so too, never PASS.
    await page.goto("/releases");
    const row = page.getByTestId("release-row").filter({ hasText: title });
    await expect(row.getByTestId("gate-outcome")).toHaveText("Overridden");
    await expect(row).toContainText("originally BLOCK");

    // The audit log keeps who did what (owners read it).
    await page.context().clearCookies();
    await signIn(page, "owner@demo.agenttwin.dev", `${new URL(url).pathname}?tab=audit`);
    const entries = page.getByTestId("audit-entry");
    await expect(entries.first()).toContainText("Gate overridden");
    await expect(entries.first()).toContainText("Hotfix for the refund outage");
    await expect(entries.filter({ hasText: "Gate decided" })).toHaveCount(1);
    await expect(entries.filter({ hasText: "Release created" })).toHaveCount(1);
    expect(problems).toEqual([]);
  });

  test("a viewer reads releases and their evidence, and changes nothing", async ({ page }) => {
    await signIn(page, "viewer@demo.agenttwin.dev", "/releases");
    await expect(page.getByTestId("release-row").first()).toBeVisible();
    await expect(page.getByRole("button", { name: "New release" })).toHaveCount(0);
    await releaseRow(page, "1.3.0")
      .getByRole("link", { name: /to 1\.3\.0$/ })
      .click();
    await expect(page.getByTestId("why")).toBeVisible();
    await expect(page.getByRole("button", { name: /^Override$/ })).toHaveCount(0);
    await expect(page.getByRole("button", { name: /Evaluate/ })).toHaveCount(0);
    await page.getByRole("tab", { name: "Audit" }).click();
    await expect(page.getByText("The audit log is for administrators")).toBeVisible();
  });
});
