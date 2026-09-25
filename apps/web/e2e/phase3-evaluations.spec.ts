import { type Page, expect, test } from "@playwright/test";
import { signIn, watchConsole } from "./helpers";

/**
 * Phase 3 acceptance, against the running stack (`make dev` + `make seed`):
 * a candidate agent version is compared with its baseline on the same pinned
 * suite - the seeded `refund-regression-suite` dataset - and the comparison
 * says what newly fails, where the candidate diverged and why; a person
 * reviews a judged result; datasets are versioned; the judge is calibrated
 * against human labels.
 */

const EVAL_TIMEOUT = 120_000;
const SHOTS = "../../docs/screenshots";
const SUITE = "refund-regression-suite";

/** Waits on the evaluation page until the run in the URL has completed; returns its id. */
async function waitForEvaluation(page: Page): Promise<string> {
  await expect(page).toHaveURL(/\/evaluations\/[0-9a-f-]{36}$/);
  const runId = new URL(page.url()).pathname.split("/").pop() ?? "";
  await expect(page.getByTestId("eval-run-id")).toHaveText(runId);
  await expect(page.locator("h1 [data-status]")).toHaveAttribute("data-status", "COMPLETED", {
    timeout: EVAL_TIMEOUT,
  });
  return runId;
}

async function startEvaluation(page: Page, baseline: string, candidate: string): Promise<string> {
  await page.goto("/evaluations/new");
  await expect(page.getByRole("heading", { name: "New evaluation" })).toBeVisible();
  await page.getByLabel("Baseline version").selectOption(baseline);
  await page.getByLabel("Candidate version").selectOption(candidate);
  // e2e datasets sort before the seeded suite: choose it by name.
  await page.getByLabel("Dataset", { exact: true }).selectOption({ label: `${SUITE} (9 cases)` });
  await page.getByLabel("Seed (optional)").fill("42");
  const start = page.getByRole("button", { name: /^Compare on \d+ scenarios$/ });
  await expect(start).toBeEnabled();
  await start.click();
  return waitForEvaluation(page);
}

function comparedCase(page: Page, scenario: string) {
  return page.locator(`[data-testid="compared-case"][data-scenario="${scenario}"]`);
}

function expectationRow(page: Page, id: string) {
  return page.locator(`[data-testid="expectation-change"][data-expectation="${id}"]`);
}

test.describe("Phase 3 acceptance: baseline against candidate, reviews, datasets and the judge", () => {
  test("1.3.0 against 1.2.4: what newly fails, where it diverged, and a reviewer's verdict", async ({
    page,
  }) => {
    const problems = watchConsole(page);
    await signIn(page, "engineer@demo.agenttwin.dev", "/evaluations");
    const runId = await startEvaluation(page, "1.2.4", "1.3.0");

    await expect(page.getByTestId("eval-verdict")).toHaveText(
      "v1.3.0 newly fails a critical expectation in 2 scenarios",
    );
    await expect(page.getByTestId("eval-summary")).toHaveText(
      `2 new critical failures · 1 regressed · 6 unchanged · ${SUITE} v1`,
    );
    const failures = page.getByTestId("new-critical-failures");
    await expect(failures).toContainText("refund-timeout-after-mutation");
    await expect(failures).toContainText("refunded-exactly-once");
    await expect(failures).toContainText("refund-tool-success-lie");
    await expect(failures).toContainText("no-unverified-success");
    await expect(comparedCase(page, "refund-happy-path")).toHaveAttribute("data-classification", "REGRESSED");
    await expect(comparedCase(page, "cross-tenant-order")).toHaveAttribute(
      "data-classification",
      "UNCHANGED",
    );
    await expect(page.getByTestId("eval-pinning")).toContainText("42");
    await expect(page.getByTestId("eval-pinning")).toContainText("not a language model");
    await page.screenshot({ path: `${SHOTS}/phase3-eval-run.png`, fullPage: true });

    // The counts filter the cases.
    await page.locator('[data-testid="count-tile"][data-classification="REGRESSED"]').click();
    await expect(page.getByTestId("compared-case")).toHaveCount(1);
    await expect(comparedCase(page, "refund-happy-path")).toBeVisible();
    await page.getByRole("button", { name: "show all" }).click();
    await expect(page.getByTestId("compared-case")).toHaveCount(9);

    // The golden path's critical case: a timeout after the refund was recorded,
    // and the candidate refunds again.
    await comparedCase(page, "refund-timeout-after-mutation").getByRole("link").click();
    await expect(page.getByRole("heading", { level: 1 })).toContainText("refund-timeout-after-mutation");
    await expect(page.getByTestId("case-reason")).toContainText("refunded-exactly-once");
    await expect(expectationRow(page, "refunded-exactly-once")).toHaveAttribute("data-change", "broken");
    await expect(page.getByTestId("divergence")).toBeVisible();
    await expect(page.getByTestId("impact")).toContainText(/refund_payment/);
    await expect(
      page.locator('[data-testid="metric"][data-metric="duplicate_side_effects"]'),
    ).toHaveAttribute("data-change", "worse");
    await page.screenshot({ path: `${SHOTS}/phase3-case-comparison.png`, fullPage: true });

    // A reviewer overrides the judge on one expectation; the case stays a new
    // critical failure because deterministic critical expectations still fail.
    await page.goto(`/evaluations/${runId}/cases/refund-tool-success-lie`);
    const semantic = expectationRow(page, "reply-admits-unconfirmed-refund");
    await expect(semantic.locator('[data-side="CANDIDATE"]')).toHaveAttribute("data-status", "FAIL");
    await page
      .getByRole("button", { name: "Review reply-admits-unconfirmed-refund on the candidate" })
      .click();
    const form = page.getByTestId("review-form");
    await expect(form.getByRole("radio", { name: "Pass" })).toBeChecked();
    await form.getByLabel(/Why/).fill("e2e: the reply does say the refund could not be confirmed.");
    await form.getByRole("button", { name: "Save review" }).click();
    await expect(page.getByTestId("review-outcome")).toHaveText(
      "Review saved. The case stays new critical failure.",
    );
    await expect(semantic.locator('[data-side="CANDIDATE"]')).toHaveAttribute("data-status", "PASS");
    await expect(semantic).toContainText("reviewed");
    await expect(page.getByTestId("review")).toContainText("FAIL → PASS");
    await page.goto(`/evaluations/${runId}`);
    await expect(comparedCase(page, "refund-tool-success-lie")).toContainText("reviewed");
    await expect(page.getByTestId("eval-summary")).toContainText("2 new critical failures");

    // Same pair, same pinned suite and seed: the comparison repeats.
    await page.getByRole("button", { name: "Run again" }).click();
    await expect(page).not.toHaveURL(new RegExp(`/evaluations/${runId}$`));
    await waitForEvaluation(page);
    await expect(page.getByTestId("eval-summary")).toHaveText(
      `2 new critical failures · 1 regressed · 6 unchanged · ${SUITE} v1`,
    );
    expect(problems, problems.join("\n")).toEqual([]);
  });

  test("the fixed version 1.3.1 improves on 1.3.0 and matches 1.2.4", async ({ page }) => {
    const problems = watchConsole(page);
    await signIn(page, "engineer@demo.agenttwin.dev", "/evaluations");
    await startEvaluation(page, "1.3.0", "1.3.1");
    await expect(page.getByTestId("eval-verdict")).toHaveText("No regressions; v1.3.1 fixes 3 scenarios");
    await expect(page.getByTestId("eval-summary")).toHaveText(`3 improved · 6 unchanged · ${SUITE} v1`);
    for (const name of ["refund-happy-path", "refund-timeout-after-mutation", "refund-tool-success-lie"]) {
      await expect(comparedCase(page, name)).toHaveAttribute("data-classification", "IMPROVED");
    }
    await startEvaluation(page, "1.2.4", "1.3.1");
    await expect(page.getByTestId("eval-verdict")).toHaveText("No regressions against v1.2.4");
    await expect(page.getByTestId("eval-summary")).toHaveText(`9 unchanged · ${SUITE} v1`);

    await page.goto("/evaluations");
    const rows = page.getByTestId("eval-run-row");
    await expect(rows.first()).toContainText("v1.2.4");
    await expect(rows.first()).toContainText("v1.3.1");
    await expect(rows.first()).toContainText("9 unchanged");
    await page.screenshot({ path: `${SHOTS}/phase3-evaluations.png`, fullPage: true });
    expect(problems, problems.join("\n")).toEqual([]);
  });

  test("a dataset is created, versioned, evaluated and archived", async ({ page }) => {
    const problems = watchConsole(page);
    const name = `e2e-refund-gate-${Date.now().toString(36)}`;
    await signIn(page, "engineer@demo.agenttwin.dev", "/datasets");
    await expect(page.locator(`[data-testid="dataset-row"][data-dataset="${SUITE}"]`)).toBeVisible();
    await page.getByRole("link", { name: "New dataset" }).click();
    await page.getByLabel("Name").fill(name);
    await page.getByLabel("Tags (optional)").fill("e2e, refunds");
    await page.getByRole("checkbox", { name: /refund-happy-path/ }).check();
    await page.getByRole("checkbox", { name: /refund-over-limit/ }).check();
    await page.getByRole("button", { name: "Create with 2 cases" }).click();
    await expect(page).toHaveURL(/\/datasets\/[0-9a-f-]{36}$/);
    await expect(page.getByRole("heading", { level: 1 })).toContainText(name);
    await expect(page.getByTestId("dataset-version")).toHaveText("v1");
    await expect(page.getByTestId("dataset-case")).toHaveCount(2);

    // Every change is a new version; the old ones stay readable.
    await page.getByRole("button", { name: "Add cases" }).click();
    const add = page.getByTestId("add-cases");
    await expect(add.getByRole("checkbox", { name: /refund-happy-path/ })).toBeDisabled();
    await add.getByRole("checkbox", { name: /refund-tool-success-lie/ }).check();
    await add.getByLabel(/Note on the new version/).fill("the success-lie fault");
    await add.getByRole("button", { name: "Add 1 case" }).click();
    await expect(page.getByTestId("dataset-version")).toHaveText("v2");
    await expect(page.getByTestId("dataset-case")).toHaveCount(3);
    await page.getByRole("button", { name: "Remove refund-over-limit" }).click();
    await page.getByRole("button", { name: "Remove", exact: true }).click();
    await expect(page.getByTestId("dataset-version")).toHaveText("v3");
    await expect(page.getByTestId("dataset-case")).toHaveCount(2);
    await expect(page.getByTestId("dataset-versions")).toContainText("the success-lie fault");
    await page.getByLabel("Show version").selectOption("1");
    await expect(page.getByTestId("dataset-version")).toHaveText("v1");
    await expect(
      page.locator('[data-testid="dataset-case"][data-scenario="refund-over-limit"]'),
    ).toBeVisible();
    await page.getByLabel("Show version").selectOption("3");

    // Evaluate the latest version: the form arrives with the dataset chosen.
    const datasetId = new URL(page.url()).pathname.split("/").pop() ?? "";
    await page.getByRole("link", { name: "Evaluate", exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`/evaluations/new\\?.*dataset_id=${datasetId}`));
    await expect(page.getByLabel("Dataset", { exact: true })).toHaveValue(datasetId);
    await page.getByLabel("Baseline version").selectOption("1.2.4");
    await page.getByLabel("Candidate version").selectOption("1.3.0");
    await page.getByRole("button", { name: "Compare on 2 scenarios" }).click();
    await waitForEvaluation(page);
    await expect(page.getByTestId("eval-summary")).toHaveText(
      `1 new critical failure · 1 regressed · ${name} v3`,
    );

    // The dataset shows each case's latest result; then it is archived.
    await page
      .getByTestId("eval-pinning")
      .getByRole("link", { name: `${name} v3` })
      .click();
    await expect(
      page.locator('[data-testid="dataset-case"][data-scenario="refund-tool-success-lie"]'),
    ).toContainText("New critical failure");
    await page.screenshot({ path: `${SHOTS}/phase3-dataset.png`, fullPage: true });
    await page.getByRole("button", { name: "Archive" }).click();
    await page.getByRole("alertdialog").getByRole("button", { name: "Archive" }).click();
    await expect(page.getByRole("heading", { level: 1 })).toContainText("archived");
    await expect(page.getByRole("button", { name: "Add cases" })).toHaveCount(0);
    expect(problems, problems.join("\n")).toEqual([]);
  });

  test("the judge is calibrated against human labels for one criterion", async ({ page }) => {
    const problems = watchConsole(page);
    await signIn(page, "reviewer@demo.agenttwin.dev", "/judges");
    await expect(page.getByTestId("judge-identity")).toContainText("keyword-overlap-v1");
    // Twenty labeled examples the keyword judge agrees with: a pass restates the rubric.
    const rubric = "The reply confirms the refund was issued.";
    const examples = Array.from({ length: 20 }, (_, i) =>
      JSON.stringify({
        id: `e2e-${i + 1}`,
        rubric,
        customer_message: "Where is my refund?",
        answer: i % 2 === 0 ? "Your refund was issued; I confirm it." : "Please contact the store.",
        human_label: i % 2 === 0 ? "pass" : "fail",
      }),
    ).join("\n");
    const form = page.getByTestId("calibrate-form");
    await form.getByLabel("Criterion").selectOption("rubric");
    await form.getByLabel(/Labeled examples/).fill(examples);
    await expect(form.getByTestId("example-count")).toHaveText("20 examples.");
    await form.getByRole("button", { name: "Calibrate" }).click();
    const latest = page.getByTestId("calibration").first();
    await expect(latest).toHaveAttribute("data-criterion", "rubric");
    await expect(latest).toHaveAttribute("data-status", "COMPLETED", { timeout: 60_000 });
    await expect(latest).toContainText("agreed 20 of 20 (100%)");
    await expect(page.locator('[data-testid="criterion"][data-criterion="rubric"]')).toHaveAttribute(
      "data-calibrated",
      "true",
    );
    await page.screenshot({ path: `${SHOTS}/phase3-judges.png`, fullPage: true });

    // The review queue: the demo's semantic expectations are not critical, so it may be empty.
    await page.goto("/reviews");
    await expect(page.getByRole("heading", { name: "Reviews" })).toBeVisible();
    expect(problems, problems.join("\n")).toEqual([]);
  });
});
