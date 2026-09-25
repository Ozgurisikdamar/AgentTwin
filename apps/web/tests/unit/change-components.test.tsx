import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ChangeSetDetail } from "@/components/changes/change-set-detail";
import { ChangeSetList } from "@/components/changes/change-set-list";
import { PathChain } from "@/components/changes/path-chain";
import { MeProvider } from "@/components/shell/me-context";
import { NewSimulation } from "@/components/simulations/new-simulation";
import type { ChangeImpact } from "@/lib/api/control-plane";
import type { AgentVersion, Me } from "@/lib/types";
import {
  liveChangeSetPage,
  livePromptChange,
  livePromptImpact,
  liveToolChange,
  liveToolImpact,
} from "./change-fixtures";

const push = vi.fn();
let search = new URLSearchParams();
vi.mock("next/navigation", () => ({
  usePathname: () => "/changes",
  useRouter: () => ({ push, replace: vi.fn(), refresh: vi.fn() }),
  useSearchParams: () => search,
}));

const PROJECT = livePromptChange.project_id;
const AGENT = livePromptChange.agent_id;
const PROMPT_CS = livePromptChange.id;
const TOOL_CS = liveToolChange.id;

type Handler = (url: string, init: RequestInit | undefined) => unknown;
let sent: { url: string; method: string; body: unknown; key: string | null }[] = [];

function stub(handler: Handler) {
  sent = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (method !== "GET") {
        const headers = new Headers(init?.headers);
        sent.push({
          url,
          method,
          body: init?.body ? JSON.parse(String(init.body)) : undefined,
          key: headers.get("Idempotency-Key"),
        });
      }
      const body = handler(url, init);
      if (body instanceof Response) return body;
      return new Response(JSON.stringify(body ?? { items: [] }), { status: method === "POST" ? 201 : 200 });
    }),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  push.mockReset();
  search = new URLSearchParams();
});

function renderAs(ui: ReactNode, permissions: string[]) {
  const me = {
    user: { id: "u-1", email: "e@demo.agenttwin.dev", display_name: "Eve" },
    permissions,
  } as unknown as Me;
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MeProvider me={me}>{ui}</MeProvider>
    </QueryClientProvider>,
  );
}

const version = (v: string, id: string) =>
  ({
    id: `01a0d7b0-0000-7000-8000-00000000000${id}`,
    agent_id: AGENT,
    agent_name: "support-refund-agent",
    project_id: PROJECT,
    version: v,
    manifest: { tools: [] },
    manifest_sha256: "a".repeat(64),
    created_by: "system:seed",
    created_at: "2026-09-25T07:29:40Z",
  }) as unknown as AgentVersion;

const projects = { items: [{ id: PROJECT, slug: "support", name: "Support" }] };
const agents = {
  items: [{ id: AGENT, project_id: PROJECT, name: "support-refund-agent", version_count: 5 }],
};
const versions = {
  items: [version("1.2.4", "1"), version("1.3.2", "2"), version("1.3.0", "3"), version("1.3.1", "4")],
};

function catalog(url: string): unknown {
  if (url === "/api/v1/projects") return projects;
  if (url === `/api/v1/projects/${PROJECT}/agents`) return agents;
  if (url === `/api/v1/agents/${AGENT}/versions`) return versions;
  return undefined;
}

describe("ChangeSetList", () => {
  it("lists the change sets with their versions and counts; comparing needs release.write", async () => {
    stub((url) => {
      if (url.startsWith(`/api/v1/projects/${PROJECT}/change-sets`)) return liveChangeSetPage;
      return catalog(url);
    });
    const { unmount } = renderAs(<ChangeSetList />, ["read"]);
    const rows = await screen.findAllByTestId("change-set-row");
    expect(rows.map((r) => r.getAttribute("data-change-set-id"))).toEqual([TOOL_CS, PROMPT_CS]);
    expect(rows[0]).toHaveTextContent("v1.3.1");
    expect(rows[0]).toHaveTextContent("v1.3.2");
    expect(rows[0]).toHaveTextContent("1 tool change");
    expect(within(rows[0]!).getByText("1 breaking")).toBeInTheDocument();
    expect(rows[1]).toHaveTextContent("1 prompt change");
    expect(within(rows[1]!).queryByText(/breaking/)).not.toBeInTheDocument();
    expect(within(rows[1]!).getByRole("link", { name: /v1\.2\.4 to v1\.3\.0/ })).toHaveAttribute(
      "href",
      `/changes/${PROMPT_CS}`,
    );
    expect(screen.getByText("Showing 2 change sets")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Compare versions/ })).not.toBeInTheDocument();
    unmount();
    renderAs(<ChangeSetList />, ["read", "release.write"]);
    expect(await screen.findByRole("button", { name: /Compare versions/ })).toBeInTheDocument();
  });

  it("filters by agent through the contract's query", async () => {
    search = new URLSearchParams({ project_id: PROJECT, agent: "support-refund-agent" });
    const fetch = vi.fn();
    stub((url) => {
      fetch(url);
      if (url.startsWith(`/api/v1/projects/${PROJECT}/change-sets`)) return { items: [], next_cursor: null };
      return catalog(url);
    });
    renderAs(<ChangeSetList />, ["read"]);
    expect(await screen.findByText("No change sets for this agent")).toBeInTheDocument();
    expect(fetch).toHaveBeenCalledWith(
      `/api/v1/projects/${PROJECT}/change-sets?agent=support-refund-agent&limit=25`,
    );
  });

  it("compares the newest version with the one before it, or the pair the user picks", async () => {
    stub((url, init) => {
      if (init?.method === "POST") return { ...liveToolChange, created: true };
      if (url.startsWith(`/api/v1/projects/${PROJECT}/change-sets`)) return liveChangeSetPage;
      return catalog(url);
    });
    renderAs(<ChangeSetList />, ["read", "release.write"]);
    await userEvent.click(await screen.findByRole("button", { name: /Compare versions/ }));
    const form = screen.getByRole("form", { name: "Compare versions" });
    await within(within(form).getByLabelText("Base version")).findByRole("option", { name: "1.3.1" });
    expect(within(form).getByLabelText("Candidate version")).toHaveValue("1.3.2");
    expect(within(form).getByLabelText("Base version")).toHaveValue("1.3.1");
    // Picking a candidate moves the base to the version before it.
    await userEvent.selectOptions(within(form).getByLabelText("Candidate version"), "1.3.0");
    expect(within(form).getByLabelText("Base version")).toHaveValue("1.2.4");
    // The same version on both sides cannot be compared.
    await userEvent.selectOptions(within(form).getByLabelText("Base version"), "1.3.0");
    expect(within(form).getByRole("alert")).toHaveTextContent("Pick two different versions");
    expect(within(form).getByRole("button", { name: "Compare" })).toBeDisabled();
    await userEvent.selectOptions(within(form).getByLabelText("Base version"), "1.2.4");
    await userEvent.type(within(form).getByLabelText("Title (optional)"), " Faster refunds ");
    await userEvent.click(within(form).getByRole("button", { name: "Compare" }));
    expect(sent).toHaveLength(1);
    expect(sent[0]!.url).toBe(`/api/v1/projects/${PROJECT}/change-sets`);
    expect(sent[0]!.body).toEqual({
      agent: "support-refund-agent",
      base_version: "1.2.4",
      candidate_version: "1.3.0",
      title: "Faster refunds",
    });
    expect(sent[0]!.key).toMatch(/^change-set/);
    await vi.waitFor(() => expect(push).toHaveBeenCalledWith(`/changes/${TOOL_CS}`));
  });
});

function detailStub(cs: typeof livePromptChange | typeof liveToolChange, impact: unknown) {
  stub((url) => {
    if (url === `/api/v1/change-sets/${cs.id}`) return cs;
    if (url === `/api/v1/change-sets/${cs.id}/impact`) return impact;
    return undefined;
  });
}

describe("ChangeSetDetail: the prompt change", () => {
  it("leads with the required scenarios and shows the prompt diff", async () => {
    detailStub(livePromptChange, livePromptImpact);
    renderAs(<ChangeSetDetail changeSetId={PROMPT_CS} />, ["read"]);
    expect(await screen.findByTestId("impact-headline")).toHaveTextContent(
      "9 scenarios required · 7 linked to the change",
    );
    expect(screen.getByTestId("impact-status")).toHaveAttribute("data-complete", "true");
    expect(screen.getByTestId("impact-status")).toHaveTextContent("Complete");
    const item = screen.getByTestId("change-item");
    expect(item).toHaveAttribute("data-kind", "prompt");
    expect(item).toHaveTextContent("prompt 49482e32190e");
    const diff = within(item).getByTestId("prompt-diff");
    expect(diff.querySelectorAll('[data-op="+"]')).toHaveLength(3);
    expect(diff.querySelectorAll('[data-op="-"]')).toHaveLength(5);
    expect(diff).toHaveTextContent("removed: Always call get_refund_policy before refund_payment.");
    expect(within(item).getByText("refund_payment")).toBeInTheDocument(); // mentioned
    // Without simulation.run, no way to start the scenarios.
    expect(screen.queryByTestId("simulate-required")).not.toBeInTheDocument();
  });

  it("says why each scenario is required, with its paths", async () => {
    detailStub(livePromptChange, livePromptImpact);
    renderAs(<ChangeSetDetail changeSetId={PROMPT_CS} />, ["read"]);
    const rows = await screen.findAllByTestId("impact-scenario");
    expect(rows).toHaveLength(9);
    const tenant = rows.find((r) => r.getAttribute("data-scenario") === "cross-tenant-order")!;
    expect(within(tenant).getByText("Linked to the change")).toBeInTheDocument();
    expect(within(tenant).getByText("text similarity 0.28")).toBeInTheDocument();
    expect(within(tenant).getByText("Always runs (security)")).toBeInTheDocument();
    expect(
      within(tenant).getByRole("list", { name: "Why cross-tenant-order is required" }),
    ).toHaveTextContent("tests tool refund_payment, which the change to the prompt reaches in 2 steps");
    expect(within(tenant).getByRole("link", { name: "cross-tenant-order" })).toHaveAttribute(
      "href",
      "/scenarios/01a0d7b0-f3af-7015-b151-cbbf2981e6b1",
    );
    await userEvent.click(within(tenant).getByText("Show 2 paths"));
    const chain = within(tenant).getAllByTestId("path-chain")[0]!;
    expect(chain).toHaveTextContent("support-refund-agent@1.3.0 uses refund_payment");
    expect(chain).toHaveTextContent("refund_payment is tested by cross-tenant-order");
    const weak = rows.find((r) => r.getAttribute("data-scenario") === "malicious-retrieved-content")!;
    expect(within(weak).getByText("Weakly linked")).toBeInTheDocument();
    expect(screen.getByTestId("similarity-note")).toHaveTextContent(
      "Similarity is text similarity (the local model compares words and identifiers, not meaning)",
    );
    // Graph links to scenarios the library does not hold are named, not hidden.
    expect(screen.getByText(/The graph also links 2 scenarios/)).toBeInTheDocument();
  });

  it("shows how the change reaches the payments service and the irreversible action", async () => {
    detailStub(livePromptChange, livePromptImpact);
    renderAs(<ChangeSetDetail changeSetId={PROMPT_CS} />, ["read"]);
    const affected = await screen.findAllByTestId("affected-component");
    expect(affected).toHaveLength(16);
    expect(screen.getByText("16 components within 4 steps")).toBeInTheDocument();
    expect(within(affected[1]!).getByText("1 step")).toBeInTheDocument();
    const service = affected.find(
      (a) => a.getAttribute("data-kind") === "SERVICE" && a.getAttribute("data-key") === "payments-api",
    )!;
    const chain = within(service).getByTestId("path-chain");
    expect(
      [...chain.querySelectorAll("[data-kind]")].map(
        (n) => `${n.getAttribute("data-kind")}:${n.textContent}`,
      ),
    ).toEqual([
      "PROMPT:Promptprompt 49482e32190e",
      "AGENT_VERSION:Agent versionsupport-refund-agent@1.3.0",
      "TOOL:Toolrefund_payment",
      "SERVICE:Servicepayments-api",
    ]);
    expect(chain).toHaveTextContent("refund_payment calls payments-api");
    expect(
      within(service).getByRole("link", { name: "Service payments-api in the dependency graph" }),
    ).toHaveAttribute("href", `/graph?project_id=${PROJECT}&kind=SERVICE&key=payments-api`);
    const irreversible = screen.getAllByTestId("irreversible-action");
    expect(irreversible.map((a) => a.getAttribute("data-key"))).toEqual([
      "refund_payment",
      "export_customer_data",
    ]);
    expect(within(irreversible[0]!).getByText("Write irreversible")).toBeInTheDocument();
    expect(within(irreversible[1]!).getByText("indirect")).toBeInTheDocument();
  });

  it("links the required scenarios to a new simulation of the candidate", async () => {
    detailStub(livePromptChange, livePromptImpact);
    renderAs(<ChangeSetDetail changeSetId={PROMPT_CS} />, ["read", "simulation.run"]);
    const link = await screen.findByTestId("simulate-required");
    expect(link).toHaveTextContent("Simulate 9 required scenarios");
    const url = new URL(link.getAttribute("href")!, "http://x");
    expect(url.searchParams.get("version")).toBe("1.3.0");
    expect(url.searchParams.get("change_set")).toBe(PROMPT_CS);
    expect(url.searchParams.get("scenarios")?.split(",")).toHaveLength(9);
  });
});

describe("ChangeSetDetail: the tool contract change", () => {
  it("shows the breaking schema changes and scenarios that test the changed tool", async () => {
    detailStub(liveToolChange, liveToolImpact);
    renderAs(<ChangeSetDetail changeSetId={TOOL_CS} />, ["read"]);
    const item = await screen.findByTestId("change-item");
    expect(item).toHaveAttribute("data-subject", "refund_payment");
    expect(within(item).getAllByText("breaking")).toHaveLength(3); // the item and both schema changes
    const table = within(item).getByTestId("schema-changes");
    expect(table).toHaveTextContent("/properties/idempotency_key");
    expect(table).toHaveTextContent("required_added");
    expect(within(item).getByText("description, schema")).toBeInTheDocument();
    const rows = await screen.findAllByTestId("impact-scenario");
    const happy = rows.find((r) => r.getAttribute("data-scenario") === "refund-happy-path")!;
    expect(happy).toHaveTextContent("tests tool refund_payment, which changed");
    expect(screen.getAllByTestId("irreversible-action").map((a) => a.getAttribute("data-key"))).toEqual([
      "refund_payment",
    ]);
  });
});

describe("ChangeSetDetail: an impact that cannot be trusted as whole", () => {
  it("warns when a service did not answer, even with the whole graph known", async () => {
    const impact = structuredClone(livePromptImpact) as ChangeImpact;
    impact.complete = false;
    impact.problems = [{ service: "graph-service", code: "NOT_CONFIGURED", message: "no graph service" }];
    detailStub(livePromptChange, impact);
    renderAs(<ChangeSetDetail changeSetId={PROMPT_CS} />, ["read"]);
    const status = await screen.findByTestId("impact-status");
    expect(within(status).getByRole("alert")).toHaveTextContent(
      "The dependency graph did not answer (NOT_CONFIGURED): no graph service",
    );
    expect(status).not.toHaveTextContent("Complete: the dependency graph");
  });

  it("warns when a service did not answer and when the graph does not know a change", async () => {
    const impact = structuredClone(livePromptImpact) as ChangeImpact;
    impact.complete = false;
    impact.problems = [
      {
        service: "simulation-service",
        code: "UPSTREAM_UNAVAILABLE",
        message: "the scenario library timed out",
      },
    ];
    impact.graph!.unresolved = [
      { component: { kind: "TOOL", key: "refund_card" }, change: "added", summary: "tool added" },
    ];
    impact.notes = ["similarity was not computed"];
    detailStub(livePromptChange, impact);
    renderAs(<ChangeSetDetail changeSetId={PROMPT_CS} />, ["read"]);
    const status = await screen.findByTestId("impact-status");
    expect(status).toHaveAttribute("data-complete", "false");
    expect(within(status).getByRole("alert")).toHaveTextContent(
      "Incomplete: this impact may miss scenarios. Treat the change as unverified.",
    );
    expect(status).toHaveTextContent(
      "The scenario library did not answer (UPSTREAM_UNAVAILABLE): the scenario library timed out",
    );
    expect(status).toHaveTextContent("Tool refund_card (tool added)");
    expect(status).toHaveTextContent("similarity was not computed");
    expect(status).not.toHaveTextContent("Complete: the dependency graph");
  });

  it("keeps the change set readable when the impact fails, and can retry", async () => {
    let calls = 0;
    stub((url) => {
      if (url === `/api/v1/change-sets/${PROMPT_CS}`) return livePromptChange;
      if (url === `/api/v1/change-sets/${PROMPT_CS}/impact`) {
        calls += 1;
        return calls === 1
          ? new Response(JSON.stringify({ error: { code: "INTERNAL", message: "impact failed" } }), {
              status: 500,
            })
          : livePromptImpact;
      }
      return undefined;
    });
    renderAs(<ChangeSetDetail changeSetId={PROMPT_CS} />, ["read"]);
    expect(await screen.findByRole("alert")).toHaveTextContent("impact failed");
    expect(screen.getByTestId("change-item")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByTestId("impact-headline")).toHaveTextContent("9 scenarios required");
  });

  it("names a missing change set", async () => {
    stub(
      () =>
        new Response(JSON.stringify({ error: { code: "NOT_FOUND", message: "change set not found" } }), {
          status: 404,
        }),
    );
    renderAs(<ChangeSetDetail changeSetId={PROMPT_CS} />, ["read"]);
    expect(await screen.findByRole("alert")).toHaveTextContent("change set not found");
    expect(screen.getByRole("link", { name: "← Changes" })).toHaveAttribute("href", "/changes");
  });
});

describe("PathChain", () => {
  it("renders nothing for an empty path and a lone changed component as a focusable link", () => {
    const { container, unmount } = render(<PathChain path={[]} />);
    expect(container).toBeEmptyDOMElement();
    unmount();
    render(
      <PathChain
        projectId="p-1"
        path={[{ component: { kind: "TOOL", key: "refund_payment" }, label: "refund_payment" }]}
      />,
    );
    expect(screen.getByRole("link")).toHaveAttribute(
      "href",
      "/graph?project_id=p-1&kind=TOOL&key=refund_payment",
    );
  });
});

describe("NewSimulation preselected by a change", () => {
  const scenarios = ["refund-happy-path", "refund-rate-limited", "unauthorized-admin-tool"].map(
    (name, i) => ({
      id: `01a0d7b0-f3af-7015-b151-00000000000${i}`,
      name,
      severity: "high",
      description: "",
    }),
  );

  function simStub() {
    stub((url, init) => {
      if (init?.method === "POST") return { run: { id: "01a0d7b0-0000-7000-8000-0000000000aa" } };
      if (url === "/api/v1/simulations/capabilities") return { agents: ["support-refund-agent"] };
      if (url.startsWith("/api/v1/scenarios?")) return { items: scenarios, next_cursor: null };
      return catalog(url);
    });
  }

  it("runs only the scenarios the change requires, and names those the agent lacks", async () => {
    search = new URLSearchParams({
      project_id: PROJECT,
      agent: "support-refund-agent",
      version: "1.3.0",
      change_set: PROMPT_CS,
      scenarios: "refund-happy-path,unauthorized-admin-tool,archived-one",
    });
    simStub();
    renderAs(<NewSimulation />, ["read", "simulation.run"]);
    const submit = await screen.findByRole("button", { name: "Run 2 scenarios" });
    const note = screen.getByTestId("preselected-note");
    expect(within(note).getByRole("link", { name: "this change" })).toHaveAttribute(
      "href",
      `/changes/${PROMPT_CS}`,
    );
    expect(note).toHaveTextContent("Not in this agent's scenarios: archived-one.");
    expect(screen.getByRole("checkbox", { name: /refund-rate-limited/ })).not.toBeChecked();
    // A scenario the user adds joins the preselection.
    await userEvent.click(screen.getByRole("checkbox", { name: /refund-rate-limited/ }));
    await userEvent.click(screen.getByRole("checkbox", { name: /unauthorized-admin-tool/ }));
    await within(screen.getByLabelText("Version")).findByRole("option", { name: /^1\.3\.0/ });
    await userEvent.click(submit);
    expect(sent).toHaveLength(1);
    expect(sent[0]!.body).toEqual({
      project_id: PROJECT,
      agent: "support-refund-agent",
      agent_version: "1.3.0",
      scenarios: ["refund-happy-path", "refund-rate-limited"],
    });
  });

  it("drops the preselection when every scenario is picked", async () => {
    search = new URLSearchParams({ project_id: PROJECT, version: "1.3.0", scenarios: "refund-happy-path" });
    simStub();
    renderAs(<NewSimulation />, ["read", "simulation.run"]);
    await screen.findByRole("button", { name: "Run 1 scenario" });
    await userEvent.click(screen.getByRole("button", { name: "Select all" }));
    expect(screen.queryByTestId("preselected-note")).not.toBeInTheDocument();
    await within(screen.getByLabelText("Version")).findByRole("option", { name: /^1\.3\.0/ });
    await userEvent.click(screen.getByRole("button", { name: "Run 3 scenarios" }));
    // Every scenario: left to the service, which picks them all.
    expect(sent[0]!.body).toEqual({
      project_id: PROJECT,
      agent: "support-refund-agent",
      agent_version: "1.3.0",
    });
  });
});
