import { type Page, expect, test } from "@playwright/test";
import { signIn, watchConsole } from "./helpers";

/**
 * Phase 4 acceptance, against the running stack (`make dev` + `make seed`):
 * a prompt or tool change selects the refund scenarios and says why
 * (golden path step 5: prompt → support-refund-agent → refund_payment →
 * payments-api), the dependency graph shows what the change reaches with the
 * evidence for each relationship, and a tool's dependencies can be mapped
 * by hand.
 */

const SHOTS = "../../docs/screenshots";
const AGENT = "support-refund-agent";
/** The seeded scenarios the refund tools are tested by, and the always-run security ones. */
const REFUND = [
  "cross-tenant-order",
  "refund-happy-path",
  "refund-over-limit",
  "refund-prompt-injection",
  "refund-rate-limited",
  "refund-timeout-after-mutation",
  "refund-tool-success-lie",
];
const SECURITY = ["malicious-retrieved-content", "unauthorized-admin-tool"];

function suffix(): string {
  return Math.random().toString(36).slice(2, 8);
}

function scenario(page: Page, name: string) {
  return page.locator(`[data-testid="impact-scenario"][data-scenario="${name}"]`);
}

async function compare(page: Page, base: string, candidate: string, title: string): Promise<void> {
  await page.goto("/changes");
  await page.getByRole("button", { name: "Compare versions" }).click();
  const form = page.getByRole("form", { name: "Compare versions" });
  await form.getByLabel("Agent").selectOption(AGENT);
  await expect(form.getByLabel("Candidate version").locator("option", { hasText: candidate })).toHaveCount(1);
  await form.getByLabel("Candidate version").selectOption(candidate);
  await form.getByLabel("Base version").selectOption(base);
  await form.getByLabel("Title (optional)").fill(title);
  await form.getByRole("button", { name: "Compare" }).click();
  await expect(page).toHaveURL(/\/changes\/[0-9a-f-]{36}$/);
  await expect(page.getByRole("heading", { level: 1 })).toContainText(AGENT);
  await expect(page.getByText(title)).toBeVisible();
}

test.describe("Phase 4 acceptance: what a change reaches and the scenarios it requires", () => {
  test("a prompt change selects the refund scenarios and explains why", async ({ page }) => {
    const problems = watchConsole(page);
    await signIn(page, "engineer@demo.agenttwin.dev", "/changes");
    await compare(page, "1.2.4", "1.3.0", `e2e prompt change ${suffix()}`);

    // What changed: the prompt's masked diff and the tools it names.
    const item = page.getByTestId("change-item");
    await expect(item).toHaveAttribute("data-kind", "prompt");
    await expect(
      page.getByTestId("prompt-diff").locator('[data-op="-"]', {
        hasText: "Always call get_refund_policy before refund_payment.",
      }),
    ).toHaveCount(1);
    await expect(
      page.getByTestId("prompt-diff").locator('[data-op="+"]', {
        hasText: "simply retry the refund right away",
      }),
    ).toHaveCount(1);

    // The impact is whole, and each required scenario says why.
    await expect(page.getByTestId("impact-status")).toHaveAttribute("data-complete", "true");
    await expect(page.getByTestId("impact-headline")).toHaveText(
      /^\d+ scenarios required · \d+ linked to the change$/,
    );
    for (const name of REFUND) {
      await expect(scenario(page, name).locator('[data-reason="linked"]')).toHaveText("Linked to the change");
      await expect(scenario(page, name)).toContainText(
        "tests tool refund_payment, which the change to the prompt reaches in 2 steps",
      );
    }
    for (const name of SECURITY) {
      await expect(scenario(page, name).locator('[data-reason="always"]')).toHaveText(
        "Always runs (security)",
      );
      await expect(scenario(page, name).locator('[data-reason="weak"]')).toHaveText("Weakly linked");
    }
    // The local embedding model's similarity is named for what it is (ADR-0014).
    await expect(scenario(page, "refund-happy-path").locator('[data-reason="similar"]')).toHaveText(
      /^text similarity 0\.\d\d$/,
    );
    await expect(page.getByTestId("similarity-note")).toContainText("text similarity");

    // Golden path: prompt → support-refund-agent → refund_payment → payments-api.
    const service = page.locator(
      '[data-testid="affected-component"][data-kind="SERVICE"][data-key="payments-api"]',
    );
    await expect(service.locator("[data-kind]")).toHaveText([
      /prompt [0-9a-f]{12}/,
      /support-refund-agent@1\.3\.0/,
      /refund_payment/,
      /payments-api/,
    ]);
    await expect(service.getByTestId("path-chain")).toContainText("refund_payment calls payments-api");
    await expect(
      page.locator('[data-testid="irreversible-action"][data-key="refund_payment"]'),
    ).toContainText("Write irreversible");
    await page.screenshot({ path: `${SHOTS}/phase4-change-impact.png`, fullPage: true });

    // The required scenarios open as a simulation of the candidate.
    await page.getByTestId("simulate-required").click();
    await expect(page).toHaveURL(/\/simulations\/new\?/);
    await expect(page.getByTestId("preselected-note")).toContainText("the scenarios this change requires");
    await expect(page.getByLabel("Version")).toHaveValue("1.3.0");
    for (const name of [...REFUND, ...SECURITY]) {
      await expect(page.getByRole("checkbox", { name: new RegExp(`^${name}\\b`) })).toBeChecked();
    }
    expect(problems).toEqual([]);
  });

  test("a tool contract change selects the scenarios that test the tool", async ({ page }) => {
    const problems = watchConsole(page);
    await signIn(page, "engineer@demo.agenttwin.dev", "/changes");
    // The seeded change set, opened from the list.
    await page
      .getByRole("link", { name: `Change set ${AGENT} v1.3.1 to v1.3.2` })
      .first()
      .click();
    await expect(page).toHaveURL(/\/changes\/[0-9a-f-]{36}$/);

    const item = page.getByTestId("change-item");
    await expect(item).toHaveAttribute("data-subject", "refund_payment");
    await expect(item).toContainText("breaking");
    const schema = page.getByTestId("schema-changes");
    await expect(schema).toContainText("/properties/idempotency_key");
    await expect(schema).toContainText("required_added");

    await expect(page.getByTestId("impact-status")).toHaveAttribute("data-complete", "true");
    for (const name of REFUND) {
      await expect(scenario(page, name)).toContainText("tests tool refund_payment, which changed");
    }
    // The change reaches what the tool writes and calls, one step away.
    await expect(
      page.locator('[data-testid="affected-component"][data-kind="DATABASE"][data-key="payments-db"]'),
    ).toContainText("1 step");
    await page.screenshot({ path: `${SHOTS}/phase4-tool-change.png`, fullPage: true });
    expect(problems).toEqual([]);
  });

  test("the dependency graph shows a change's blast radius and each relationship's evidence", async ({
    page,
  }) => {
    const problems = watchConsole(page);
    await signIn(page, "engineer@demo.agenttwin.dev", "/changes");
    await page
      .getByRole("link", { name: `Change set ${AGENT} v1.2.4 to v1.3.0` })
      .first()
      .click();
    await page.getByTestId("show-on-graph").click();
    await expect(page).toHaveURL(/\/graph\?/);

    const banner = page.getByTestId("blast-radius-banner");
    await expect(banner).toContainText(`Blast radius of ${AGENT} v1.2.4 → v1.3.0`);
    // The graph opens on the changed prompt, the blast radius outlined by severity.
    await expect(page.getByTestId("graph-summary")).toContainText("4 steps from prompt");
    const node = (kind: string, key: string) =>
      page.locator(`[data-testid="graph-node"][data-kind="${kind}"][data-key="${key}"]`);
    await expect(page.locator('[data-testid="graph-node"][data-focus]')).toHaveAttribute(
      "data-kind",
      "PROMPT",
    );
    await expect(node("TOOL", "refund_payment")).toHaveAttribute("data-impact", "critical");
    await expect(node("SERVICE", "payments-api")).toHaveAttribute("data-impact", /critical|high/);
    // Other agent versions are outside the blast radius.
    await expect(node("AGENT_VERSION", `${AGENT}@1.2.3`)).toHaveCount(0);

    // A component's relationships, with their evidence.
    await page.getByRole("button", { name: "Tool refund_payment" }).click();
    const panel = page.locator('[data-testid="component-panel"][data-key="refund_payment"]');
    await expect(panel).toContainText("Risk");
    const writes = panel.locator('[data-testid="relation"][data-type="WRITES"]', { hasText: "payments-db" });
    await expect(writes).toContainText("refund_payment writes payments-db (database)");
    await expect(
      panel.locator('[data-testid="relation"][data-direction="in"]', { hasText: "observed" }).first(),
    ).toContainText("uses refund_payment");
    await page.screenshot({ path: `${SHOTS}/phase4-graph.png`, fullPage: true });

    // The whole neighbourhood, filtered by evidence: prompts are never observed in traffic.
    await banner.getByRole("checkbox", { name: "Only the blast radius" }).uncheck();
    await expect(node("AGENT_VERSION", `${AGENT}@1.2.3`)).toHaveCount(1);
    await page.getByLabel("Evidence").selectOption("observed");
    await expect(page.getByTestId("graph-summary")).toContainText("Showing 1 of");

    // Back to the agents; find a component and centre on it.
    await page.getByRole("button", { name: "Back to the agents" }).click();
    await expect(page.getByTestId("graph-summary")).toContainText(`from ${AGENT}@`);
    await page.getByLabel("Find a component").fill("payments-db");
    await page.getByRole("button", { name: "Search the graph" }).click();
    await page
      .getByRole("list", { name: "Matches" })
      .getByRole("button", { name: /payments-db/ })
      .click();
    await expect(page.getByTestId("graph-summary")).toContainText("from payments-db");
    await expect(page.locator('[data-testid="component-panel"][data-key="payments-db"]')).toContainText(
      "refund_payment writes payments-db",
    );
    expect(problems).toEqual([]);
  });

  test("an engineer maps a tool's dependencies by hand; a viewer only reads", async ({ page }) => {
    const problems = watchConsole(page);
    // A fixed name: the graph keeps a component once seen, so one leftover at most.
    const system = "e2e-ledger-db";
    await signIn(page, "engineer@demo.agenttwin.dev", "/graph");
    // A link names the tool by kind and key; the page finds it and centres on it.
    await page.goto("/graph?kind=TOOL&key=refund_payment");
    await expect(page).toHaveURL(/[?&]focus=[0-9a-f-]{36}/);
    const panel = page.locator('[data-testid="component-panel"][data-key="refund_payment"]');
    const form = panel.getByRole("form", { name: "Manual mapping of refund_payment" });
    await form.getByRole("button", { name: "Add dependency" }).click();
    const row = form.getByTestId("mapping-row").last();
    await row.getByLabel("Kind").selectOption("DATABASE");
    await row.getByLabel("Name").fill(system);
    await row.getByLabel("Relation").selectOption("WRITES");
    await row.getByLabel("Criticality").selectOption("HIGH");
    await form.getByRole("button", { name: "Save mapping" }).click();
    await expect(form.getByRole("status")).toContainText("Saved:");
    const mapped = panel.locator('[data-testid="relation"][data-type="WRITES"]', { hasText: system });
    await expect(mapped).toContainText(`refund_payment writes ${system} (database)`);
    await expect(mapped).toContainText("manual");

    // Removed again: the manual mapping is replaced as a whole. Leftovers of
    // an earlier run (e2e-*) go too, so the demo graph stays as seeded.
    await page.reload();
    const again = page.locator('[data-testid="component-panel"][data-key="refund_payment"]');
    await expect(again.locator('[data-testid="relation"]', { hasText: system })).toBeVisible();
    const rows = again.getByTestId("mapping-row");
    await expect(rows.first()).toBeVisible();
    const names = await rows.evaluateAll((els) =>
      els.map((el) => (el.querySelector("input") as HTMLInputElement | null)?.value ?? ""),
    );
    expect(names).toContain(system);
    for (let i = names.length - 1; i >= 0; i--) {
      if (names[i]!.startsWith("e2e-"))
        await again.getByRole("button", { name: `Remove dependency ${i + 1}` }).click();
    }
    await again.getByRole("button", { name: "Save mapping" }).click();
    await expect(again.getByRole("status")).toBeVisible();
    await expect(
      again.locator('[data-testid="relation"][data-type="WRITES"]', { hasText: system }),
    ).toHaveCount(0);
    expect(problems).toEqual([]);

    // A viewer sees the change sets and the graph, but compares and maps nothing.
    await page.context().clearCookies();
    await signIn(page, "viewer@demo.agenttwin.dev", "/changes");
    await expect(page.getByTestId("change-set-row").first()).toBeVisible();
    await expect(page.getByRole("button", { name: "Compare versions" })).toHaveCount(0);
    await page.goto("/graph?kind=TOOL&key=refund_payment");
    await expect(page.locator('[data-testid="component-panel"][data-key="refund_payment"]')).toBeVisible();
    await expect(page.getByRole("form", { name: /Manual mapping/ })).toHaveCount(0);
  });
});
