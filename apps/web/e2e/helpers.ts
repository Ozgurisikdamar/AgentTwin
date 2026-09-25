import { type Page, expect } from "@playwright/test";

export const env = {
  agentURL:
    process.env.E2E_DEMO_AGENT_URL ?? `http://127.0.0.1:${process.env.DEMO_AGENT_HOST_PORT ?? "8090"}`,
  toolsURL:
    process.env.E2E_DEMO_TOOLS_URL ?? `http://127.0.0.1:${process.env.DEMO_TOOLS_HOST_PORT ?? "8091"}`,
  agentToken: process.env.DEMO_AGENT_TOKEN ?? "",
  toolsAdminToken: process.env.DEMO_TOOLS_ADMIN_TOKEN ?? "",
};

export interface DemoRun {
  trace_id: string;
  agent_version: string;
  claimed_outcome: string;
  business_outcome: string | null;
  tool_calls: { name: string; status: string }[];
}

/**
 * Creates a fresh delivered order in the demo tools (admin API). The order is
 * exempt from the stack's probabilistic fault injection so the agent's path
 * is deterministic.
 */
export async function createOrder(total: number, customerId = "CUS-100"): Promise<string> {
  const res = await fetch(`${env.toolsURL}/admin/orders`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${env.toolsAdminToken}` },
    body: JSON.stringify({
      customer_id: customerId,
      total,
      status: "delivered",
      age_days: 3,
      inject_faults: false,
    }),
  });
  expect(res.status, "demo tools admin API (set DEMO_TOOLS_ADMIN_TOKEN)").toBe(201);
  return ((await res.json()) as { order_id: string }).order_id;
}

/** Runs one conversation through the real demo agent adapter. */
export async function runDemoAgent(
  input: string,
  version = "1.2.4",
  customerId = "CUS-100",
): Promise<DemoRun> {
  const res = await fetch(`${env.agentURL}/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${env.agentToken}` },
    body: JSON.stringify({
      input,
      customer_id: customerId,
      agent_version: version,
      run_context: { source: "production", environment: "production" },
    }),
  });
  expect(res.status, "demo agent adapter (set DEMO_AGENT_TOKEN)").toBe(200);
  return (await res.json()) as DemoRun;
}

/** Fails the test on uncaught page errors and CSP violations. */
export function watchConsole(page: Page): string[] {
  const problems: string[] = [];
  page.on("pageerror", (err) => problems.push(`pageerror: ${err.message}`));
  page.on("console", (msg) => {
    const text = msg.text();
    if (msg.type() === "error" || /Content Security Policy|Refused to/i.test(text))
      problems.push(`${msg.type()}: ${text}`);
  });
  return problems;
}

export async function signIn(
  page: Page,
  email = "owner@demo.agenttwin.dev",
  next = "/overview",
): Promise<void> {
  await page.goto(`/login?next=${encodeURIComponent(next)}`);
  await page.getByLabel("Email").fill(email);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`${next.replace(/[?/]/g, "\\$&")}`));
}
