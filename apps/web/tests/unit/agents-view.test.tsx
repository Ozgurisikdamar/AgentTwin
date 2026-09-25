import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AgentsView } from "@/components/agents/agents-view";
import type { ControlPlaneSchemas } from "@/lib/api/control-plane";

const PROJECT = "01a0d614-451c-7577-acb1-fea675542bae";
const AGENT = "01a0d614-b803-7de6-92ac-1eb7084ec07d";
const KEY = "apikey:01a0d614-451e-7920-932d-ab18fb179ec7";

// Verbatim from the demo stack (tool input schemas left out): the version list
// carries the normalized manifest, and the manifest carries the tools.
const agents = {
  items: [
    {
      id: AGENT,
      organization_id: "01a0d614-4514-78f3-9737-3e207d5e0f5e",
      project_id: PROJECT,
      name: "support-refund-agent",
      description: "Handles customer support questions and may issue eligible refunds.\n",
      created_by: KEY,
      created_at: "2026-09-25T01:01:17.187865Z",
      version_count: 1,
      latest_version: "1.3.1",
    },
  ],
} satisfies ControlPlaneSchemas["AgentList"];

const sha = (c: string) => c.repeat(64);
const versions = {
  items: [
    {
      id: "01a0d614-b832-759e-91a7-51dfb7d7011c",
      agent_id: AGENT,
      agent_name: "support-refund-agent",
      project_id: PROJECT,
      version: "1.3.1",
      manifest: {
        name: "support-refund-agent",
        version: "1.3.1",
        model: { name: "scripted-planner-v1", provider: "scripted", temperature: 0 },
        limits: { max_steps: 30, max_cost_usd: 1.5, max_tool_calls: 20, max_duration_seconds: 120 },
        content_mode: "redacted",
        tools: [
          { name: "lookup_order", risk: "READ", risk_declared: true, definition_sha256: sha("2") },
          {
            name: "refund_payment",
            risk: "WRITE_IRREVERSIBLE",
            dimensions: { privacy: "low", financial: "high", operational: "low" },
            risk_declared: true,
            definition_sha256: sha("2"),
            approval_required_when: "args.amount > 100",
          },
          { name: "send_email", risk: "WRITE_REVERSIBLE", risk_declared: true, definition_sha256: sha("1") },
        ],
      },
      manifest_sha256: sha("a"),
      prompt_version_id: "01a0d614-b830-7afa-a93a-cf6318171359",
      prompt_sha256: sha("c"),
      model_provider: "scripted",
      model_name: "scripted-planner-v1",
      model_params: { temperature: 0 },
      runtime_endpoint: "http://demo-agent:8090",
      created_by: KEY,
      created_at: "2026-09-25T01:01:17.231937Z",
    },
  ],
} satisfies ControlPlaneSchemas["AgentVersionList"];

const projects = {
  items: [
    {
      id: PROJECT,
      organization_id: "01a0d614-4514-78f3-9737-3e207d5e0f5e",
      slug: "support",
      name: "Customer Support",
      description: "Support automation agents",
      content_mode: "redacted",
      store_prompt_text: true,
      trace_retention_days: 30,
      content_retention_days: 7,
      artifact_retention_days: 30,
      gate_policy: {},
      created_by: "system:bootstrap",
      updated_by: "system:bootstrap",
      created_at: "2026-09-25T01:00:47.764504Z",
      updated_at: "2026-09-25T01:00:47.764504Z",
    },
  ],
} satisfies ControlPlaneSchemas["ProjectList"];

describe("AgentsView", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        const body = url.endsWith("/versions")
          ? versions
          : url.endsWith("/agents")
            ? agents
            : url === "/api/v1/projects"
              ? projects
              : { error: { code: "NOT_FOUND", message: "not found" } };
        return new Response(JSON.stringify(body), { status: "items" in body ? 200 : 404 });
      }),
    );
  });
  afterEach(() => vi.unstubAllGlobals());

  it("lists each version's tools from its manifest", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrap = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    );
    render(<AgentsView />, { wrapper: wrap });
    // The list answer has no `tools` of its own: the count used to read "0 tools".
    const toggle = await screen.findByRole("button", { name: "3 tools" });
    await userEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    const list = toggle.nextElementSibling as HTMLElement;
    expect(
      within(list)
        .getAllByRole("listitem")
        .map((li) => li.querySelector("code")?.textContent),
    ).toEqual(["lookup_order", "refund_payment", "send_email"]);
    expect(within(list).getByText("approval when args.amount > 100")).toBeInTheDocument();
  });
});
