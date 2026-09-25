import { type Page, expect, test } from "@playwright/test";
import {
  claimApprovalToken,
  createOrder,
  gatewayCall,
  newTraceparent,
  refundCount,
  signIn,
  startContainedRun,
  watchConsole,
} from "./helpers";

/**
 * Phase 7 acceptance (golden path 13–14), against the running stack
 * (`make dev` + `make seed`, which registers the demo tools with the runtime
 * gateway and activates refund-limits): a refund above the configured amount
 * is held for a person; the approvals page receives the request; a reviewer
 * approves that exact action; it succeeds once; a modified request cannot
 * reuse the approval.
 */

const SHOTS = "../../docs/screenshots";
const REVIEWER = "reviewer@demo.agenttwin.dev";

/** The approval request of an order, as the inbox lists it (it refreshes every 15 s). */
async function openApproval(page: Page, orderId: string): Promise<string> {
  const row = page.getByTestId("approval-row").filter({ hasText: orderId });
  await expect(async () => {
    await page.reload();
    await expect(row).toBeVisible({ timeout: 3_000 });
  }).toPass({ timeout: 60_000 });
  await expect(row.getByRole("cell").nth(1)).toContainText("refund_payment");
  await expect(row.getByRole("cell").nth(3)).toHaveText("refund-limitsover-automatic-limit");
  await expect(row.getByRole("cell").nth(4)).toHaveText("Pending");
  await row.getByRole("link", { name: /^Approval request:/ }).click();
  await expect(page).toHaveURL(/\/approvals\/[0-9a-f-]{36}\?project_id=/);
  return new URL(page.url()).pathname.split("/").pop()!;
}

async function approve(page: Page, reason: string) {
  const form = page.getByTestId("decide-form");
  await expect(form).toBeVisible();
  await form.getByLabel("Reason").fill(reason);
  await form.getByRole("button", { name: "Approve this action" }).click();
  await expect(page.getByTestId("approval-header")).not.toContainText("Pending");
}

test.describe.serial("Phase 7 acceptance: a risky action waits for a person and runs once", () => {
  test("an over-limit refund is held; a reviewer approves the exact action; the agent runs it once", async ({
    page,
  }) => {
    test.setTimeout(240_000);
    const problems = watchConsole(page);
    const orderId = await createOrder(180);
    // The agent's tools go through the gateway; it waits up to 3 minutes for a person.
    const run = startContainedRun(
      `Hi! One item in ${orderId} arrived broken. Can I get a refund of $150?`,
      "1.3.1",
      180,
    );

    await signIn(page, REVIEWER, "/approvals");
    await page.screenshot({ path: `${SHOTS}/phase7-approvals.png`, fullPage: true });
    await openApproval(page, orderId);
    await expect(page.getByTestId("approval-header")).toContainText("Pending");
    // The exact action a person approves.
    const args = page.getByTestId("argument-row");
    await expect(args.filter({ hasText: "amount" })).toContainText("150");
    await expect(args.filter({ hasText: "order_id" })).toContainText(orderId);
    await expect(page.getByTestId("approval-reason")).toHaveText(
      "Refunds over 100 USD need a person's approval.",
    );
    await expect(page.getByText("Not used yet")).toBeVisible();
    // Nothing moved while it waits.
    expect(await refundCount(orderId)).toBe(0);
    await page.screenshot({ path: `${SHOTS}/phase7-approval-pending.png`, fullPage: true });

    await approve(page, "Damaged item confirmed from the customer's photo.");
    const result = await run;
    expect(result.business_outcome).toBe("REFUND_COMPLETED");
    // One refund call: the agent's client waited for the approval and repeated it with the token.
    expect(result.tool_calls.filter((c) => c.name === "refund_payment")).toEqual([
      expect.objectContaining({ status: "ok" }),
    ]);
    expect(await refundCount(orderId)).toBe(1);

    // The approval was used once, by the exact action.
    await expect(async () => {
      await page.reload();
      await expect(page.getByTestId("approval-header")).toContainText("Used", { timeout: 3_000 });
    }).toPass({ timeout: 30_000 });
    const attempts = page.getByTestId("attempt-row");
    await expect(attempts).toHaveCount(1);
    await expect(attempts.first()).toHaveAttribute("data-result", "executed");
    await expect(attempts.first()).toContainText("the same action");
    await expect(page.getByText("The approved run")).toBeVisible();
    await expect(page.getByText("HTTP 200")).toBeVisible();

    // The conversation's decisions: held, then executed with the approval.
    await page.goto(`/decisions?trace_id=${result.trace_id}`);
    const rows = page.getByTestId("decision-row");
    await expect(rows.filter({ hasText: "Held for approval" })).toHaveCount(1);
    await expect(rows.filter({ hasText: "refund_payment" }).filter({ hasText: "Executed" })).toHaveCount(1);
    await page.screenshot({ path: `${SHOTS}/phase7-decisions.png`, fullPage: true });
    expect(problems).toEqual([]);
  });

  test("a modified request cannot reuse the approval; the exact one runs once", async ({ page }) => {
    test.setTimeout(180_000);
    const problems = watchConsole(page);
    const orderId = await createOrder(180);
    const { traceparent } = newTraceparent();
    const exact = { order_id: orderId, amount: 150, idempotency_key: `refund-${orderId}-150.00` };

    // The agent asks; the gateway holds it and names the approval request.
    const held = await gatewayCall("refund_payment", exact, {
      idempotencyKey: exact.idempotency_key,
      traceparent,
    });
    expect(held.status).toBe(403);
    expect(held.body.error?.code).toBe("APPROVAL_REQUIRED");
    const approvalId = String(held.body.error?.details?.approval_id);
    // Before a person decides there is no token.
    expect((await claimApprovalToken(approvalId)).body.error?.code).toBe("APPROVAL_PENDING");

    await signIn(page, REVIEWER, "/approvals");
    expect(await openApproval(page, orderId)).toBe(approvalId);
    await approve(page, "Refund 150 USD, exactly.");
    await expect(page.getByTestId("approval-header")).toContainText("Approved");

    const claimed = await claimApprovalToken(approvalId);
    expect(claimed.status).toBe(201);
    const token = String(claimed.body.token);

    // The same token for a larger refund: refused, and the tool is not called.
    const modified = { ...exact, amount: 200, idempotency_key: `refund-${orderId}-200.00` };
    const refused = await gatewayCall("refund_payment", modified, {
      idempotencyKey: modified.idempotency_key,
      traceparent,
      token,
    });
    expect(refused.status).toBe(403);
    expect(refused.body.error?.code).toBe("APPROVAL_MISMATCH");
    expect(refused.body.error?.details?.changes).toContainEqual({ path: "amount", before: 150, after: 200 });
    expect(await refundCount(orderId)).toBe(0);

    // The exact action runs, once.
    const ran = await gatewayCall("refund_payment", exact, {
      idempotencyKey: exact.idempotency_key,
      traceparent,
      token,
    });
    expect(ran.status).toBe(200);
    expect(ran.headers.get("X-AgentTwin-Decision")).toBe("require_approval");
    expect(await refundCount(orderId)).toBe(1);
    // Retrying it replays the stored answer; nothing runs again.
    const again = await gatewayCall("refund_payment", exact, {
      idempotencyKey: exact.idempotency_key,
      traceparent,
      token,
    });
    expect(again.status).toBe(200);
    expect(again.headers.get("Idempotent-Replayed")).toBe("true");
    expect(await refundCount(orderId)).toBe(1);
    // The approval is spent.
    expect((await claimApprovalToken(approvalId)).body.error?.code).toBe("APPROVAL_USED");

    await page.reload();
    await expect(page.getByTestId("approval-header")).toContainText("Used");
    const attempts = page.getByTestId("attempt-row");
    await expect(attempts).toHaveCount(2);
    await expect(attempts.nth(0)).toHaveAttribute("data-result", "mismatch");
    await expect(attempts.nth(0)).toContainText("Different action");
    await expect(attempts.nth(0).getByRole("list", { name: "Changes" })).toContainText("amount: 150 → 200");
    await expect(attempts.nth(1)).toHaveAttribute("data-result", "executed");
    await page.screenshot({ path: `${SHOTS}/phase7-approval-used.png`, fullPage: true });
    expect(problems).toEqual([]);
  });

  test("the refund policy that held it is active and its tests pass", async ({ page }) => {
    const problems = watchConsole(page);
    await signIn(page, "owner@demo.agenttwin.dev", "/policies");
    const row = page.getByTestId("policy-row").filter({ hasText: "refund-limits" });
    await expect(row).toContainText("v1 active");
    await page.screenshot({ path: `${SHOTS}/phase7-policies.png`, fullPage: true });
    await row.getByRole("link", { name: "Policy refund-limits" }).click();
    await expect(page.getByTestId("policy-header")).toContainText("v1 active");
    await expect(page.getByTestId("rule-row")).toHaveCount(3);
    await page.getByRole("button", { name: "Run tests" }).click();
    await expect(page.getByTestId("report-verdict")).toHaveText(
      "All 6 tests pass: this version can be activated.",
    );
    await expect(page.getByTestId("boundary").first()).toContainText("100 → Allow · 100.01 → Needs approval");
    await page.screenshot({ path: `${SHOTS}/phase7-policy.png`, fullPage: true });
    expect(problems).toEqual([]);
  });
});
