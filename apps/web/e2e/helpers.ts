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

/** The control plane (the runtime gateway is behind it, ADR-0033). */
export const controlPlaneURL =
  process.env.E2E_CONTROL_PLANE_URL ?? `http://127.0.0.1:${process.env.CONTROL_PLANE_HOST_PORT ?? "8080"}`;

/** Starts a contained conversation: the agent's tools go through the runtime gateway. */
export function startContainedRun(
  input: string,
  version: string,
  approvalWaitS: number,
  customerId = "CUS-100",
): Promise<DemoRun & { error?: string }> {
  return fetch(`${env.agentURL}/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${env.agentToken}` },
    body: JSON.stringify({
      input,
      customer_id: customerId,
      agent_version: version,
      contained: true,
      approval_wait_s: approvalWaitS,
      run_context: { source: "production", environment: "production" },
    }),
  }).then(async (res) => {
    expect(res.status, "demo agent adapter (set DEMO_AGENT_TOKEN)").toBe(200);
    return (await res.json()) as DemoRun;
  });
}

export interface GatewayAnswer {
  status: number;
  body: { error?: { code: string; message: string; details?: Record<string, unknown> } } & Record<
    string,
    unknown
  >;
  headers: Headers;
}

/** Calls a tool through the runtime gateway as the demo agent would (its API key). */
export async function gatewayCall(
  tool: string,
  args: Record<string, unknown>,
  opts: { idempotencyKey?: string; traceparent?: string; token?: string } = {},
): Promise<GatewayAnswer> {
  const key = process.env.AGENTTWIN_DEMO_API_KEY ?? "";
  expect(key, "set AGENTTWIN_DEMO_API_KEY").not.toBe("");
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    "X-AgentTwin-Api-Key": key,
    "X-AgentTwin-Agent": "support-refund-agent",
    "X-AgentTwin-Agent-Version": "1.3.1",
    "X-AgentTwin-Environment": "production",
  };
  if (opts.idempotencyKey) headers["Idempotency-Key"] = opts.idempotencyKey;
  if (opts.traceparent) headers.traceparent = opts.traceparent;
  if (opts.token) headers["X-AgentTwin-Approval-Token"] = opts.token;
  const res = await fetch(`${controlPlaneURL}/gateway/v1/tools/${tool}`, {
    method: "POST",
    headers,
    body: JSON.stringify(args),
  });
  return { status: res.status, body: await res.json(), headers: res.headers };
}

/** Claims the token of an approved request (only its requester may). */
export async function claimApprovalToken(approvalId: string): Promise<GatewayAnswer> {
  const res = await fetch(`${controlPlaneURL}/gateway/v1/approvals/${approvalId}/token`, {
    method: "POST",
    headers: { "X-AgentTwin-Api-Key": process.env.AGENTTWIN_DEMO_API_KEY ?? "" },
  });
  return { status: res.status, body: await res.json(), headers: res.headers };
}

/** How many refunds the demo tools issued for an order (admin API). */
export async function refundCount(orderId: string): Promise<number> {
  const res = await fetch(`${env.toolsURL}/state`, {
    headers: { Authorization: `Bearer ${env.toolsAdminToken}` },
  });
  expect(res.status).toBe(200);
  const state = (await res.json()) as { orders: Record<string, { refund_count: number }> };
  return state.orders[orderId]?.refund_count ?? 0;
}

/** A W3C traceparent with a fresh trace id. */
export function newTraceparent(): { traceId: string; traceparent: string } {
  const hex = (n: number) =>
    Array.from(crypto.getRandomValues(new Uint8Array(n)), (b) => b.toString(16).padStart(2, "0")).join("");
  const traceId = hex(16);
  return { traceId, traceparent: `00-${traceId}-${hex(8)}-01` };
}
