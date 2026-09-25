import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { GraphCanvasProps } from "@/components/graph/graph-canvas";
import { ComponentPanel } from "@/components/graph/component-panel";
import { GraphExplorer } from "@/components/graph/graph-explorer";
import { MeProvider } from "@/components/shell/me-context";
import type { Me } from "@/lib/types";
import { livePromptImpact } from "./change-fixtures";
import { livePaymentsSearch, livePromptNeighbourhood, liveRefundPayment } from "./graph-fixtures";

const replace = vi.fn();
let search = new URLSearchParams();
vi.mock("next/navigation", () => ({
  usePathname: () => "/graph",
  useRouter: () => ({ push: vi.fn(), replace, refresh: vi.fn() }),
  useSearchParams: () => search,
}));

// The canvas is React Flow (tested apart): here a list that shows what it was given.
vi.mock("@/components/graph/graph-canvas", () => ({
  default: ({ view, selectedId, onSelect, impact }: GraphCanvasProps) => (
    <ul aria-label="Canvas">
      {view.nodes.map((n) => (
        <li
          key={n.id}
          data-testid="canvas-node"
          data-key={n.key}
          data-impact={impact?.get(`${n.kind}:${n.key}`)}
        >
          <button type="button" onClick={() => onSelect(n.id)} aria-pressed={n.id === selectedId}>
            {n.label}
          </button>
        </li>
      ))}
    </ul>
  ),
}));

const PROJECT = "01a0d7b0-77fb-709b-b8ff-d8b711f57143";
const PROMPT = livePromptNeighbourhood.focus[0]!.id;
const REFUND = liveRefundPayment.component.id;

type Handler = (url: string, init: RequestInit | undefined) => unknown;
let calls: { url: string; method: string; body: unknown; key: string | null }[] = [];

function stub(handler: Handler) {
  calls = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      const headers = new Headers(init?.headers);
      calls.push({
        url,
        method,
        body: init?.body ? JSON.parse(String(init.body)) : undefined,
        key: headers.get("Idempotency-Key"),
      });
      const body = handler(url, init);
      if (body instanceof Response) return body;
      return new Response(JSON.stringify(body ?? { items: [] }), { status: 200 });
    }),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  replace.mockReset();
  search = new URLSearchParams();
});

function renderAs(ui: ReactNode, permissions: string[] = ["read"]) {
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

const projects = { items: [{ id: PROJECT, slug: "support", name: "Support" }] };

function graphStub(extra?: Handler) {
  stub((url, init) => {
    const more = extra?.(url, init);
    if (more !== undefined) return more;
    if (url === "/api/v1/projects") return projects;
    if (url.startsWith("/api/v1/graph?")) return livePromptNeighbourhood;
    if (url === `/api/v1/graph/components/${REFUND}`) return liveRefundPayment;
    if (url.startsWith("/api/v1/graph/components/")) return { ...liveRefundPayment, relations: [] };
    return undefined;
  });
}

/** The last URL the page moved to (its query), as an object. */
function lastQuery(): Record<string, string> {
  const url = replace.mock.calls.at(-1)?.[0] as string;
  return Object.fromEntries(new URL(url, "http://x").searchParams);
}

describe("GraphExplorer", () => {
  it("asks the graph service for the neighbourhood and says what it shows", async () => {
    search = new URLSearchParams({ project_id: PROJECT, focus: PROMPT });
    graphStub();
    renderAs(<GraphExplorer />);
    expect(await screen.findByTestId("graph-summary")).toHaveTextContent(
      "Showing 18 of the project's 42 components and 46 of 98 relationships, 2 steps from prompt 49482e32190e.",
    );
    expect(calls.map((c) => c.url)).toContain(
      `/api/v1/graph?project_id=${PROJECT}&focus=${PROMPT}&depth=2&limit=150`,
    );
    expect(screen.getAllByTestId("canvas-node")).toHaveLength(18);
    // The focus is selected at first: its panel is open.
    expect(await screen.findByTestId("component-panel")).toBeInTheDocument();
    expect(screen.getByTestId("relationship-table").querySelectorAll("tbody tr")).toHaveLength(46);
  });

  it("filters by evidence and risk tier without asking again", async () => {
    search = new URLSearchParams({ project_id: PROJECT, focus: PROMPT, risk: "irreversible" });
    graphStub();
    renderAs(<GraphExplorer />);
    await screen.findByTestId("graph-summary");
    const tools = screen
      .getAllByTestId("canvas-node")
      .map((n) => n.getAttribute("data-key"))
      .filter((k) => ["refund_payment", "export_customer_data", "lookup_order", "send_email"].includes(k!));
    expect(tools.sort()).toEqual(["export_customer_data", "refund_payment"]);
    await userEvent.selectOptions(screen.getByLabelText("Evidence"), "observed");
    expect(lastQuery()).toMatchObject({ evidence: "observed", risk: "irreversible" });
  });

  it("finds a component named by kind and key, then centres on it", async () => {
    search = new URLSearchParams({ project_id: PROJECT, kind: "TOOL", key: "refund_payment" });
    graphStub((url) =>
      url.startsWith("/api/v1/graph/components?")
        ? {
            items: [
              { ...liveRefundPayment.component, key: "refund_payment_v2" },
              liveRefundPayment.component,
            ],
            next_cursor: null,
          }
        : undefined,
    );
    renderAs(<GraphExplorer />);
    await vi.waitFor(() => expect(replace).toHaveBeenCalled());
    expect(calls.map((c) => c.url)).toContain(
      `/api/v1/graph/components?project_id=${PROJECT}&kind=TOOL&q=refund_payment&limit=200`,
    );
    // The exact key, not the first match; kind and key leave the URL.
    expect(lastQuery()).toEqual({ project_id: PROJECT, focus: REFUND, selected: REFUND });
    // The graph waits for the focus rather than loading the agents first.
    const graphs = calls.filter((c) => c.url.startsWith("/api/v1/graph?"));
    expect(graphs.every((c) => c.url.includes(`focus=${REFUND}`))).toBe(true);
  });

  it("says when the graph does not know the component", async () => {
    search = new URLSearchParams({ project_id: PROJECT, kind: "TOOL", key: "refund_card" });
    graphStub((url) => (url.startsWith("/api/v1/graph/components?") ? livePaymentsSearch : undefined));
    renderAs(<GraphExplorer />);
    expect(await screen.findByText("The graph does not know this component")).toBeInTheDocument();
    expect(replace).not.toHaveBeenCalled();
  });

  it("outlines a change set's blast radius and shows only it", async () => {
    search = new URLSearchParams({
      project_id: PROJECT,
      focus: PROMPT,
      change_set: livePromptImpact.change_set_id,
    });
    graphStub((url) =>
      url === `/api/v1/change-sets/${livePromptImpact.change_set_id}/impact` ? livePromptImpact : undefined,
    );
    renderAs(<GraphExplorer />);
    const banner = await screen.findByTestId("blast-radius-banner");
    await vi.waitFor(() =>
      expect(banner).toHaveTextContent("Blast radius of support-refund-agent v1.2.4 → v1.3.0: 16 components"),
    );
    // The change sets the default depth to the blast radius's.
    await vi.waitFor(() => expect(calls.some((c) => c.url.includes("&depth=4&"))).toBe(true));
    const shown = await screen.findAllByTestId("canvas-node");
    // Other agent versions are not in the blast radius.
    expect(shown.map((n) => n.getAttribute("data-key"))).not.toContain("support-refund-agent@1.2.3");
    expect(shown.find((n) => n.getAttribute("data-key") === "refund_payment")).toHaveAttribute(
      "data-impact",
      "critical",
    );
    await userEvent.click(within(banner).getByRole("checkbox", { name: "Only the blast radius" }));
    expect(lastQuery()).toMatchObject({ only: "all" });
  });

  it("opens the component a user picks, and centres on one from its panel", async () => {
    search = new URLSearchParams({ project_id: PROJECT, focus: PROMPT });
    graphStub();
    renderAs(<GraphExplorer />);
    await screen.findByTestId("graph-summary");
    await userEvent.click(screen.getByRole("button", { name: "refund_payment" }));
    expect(lastQuery()).toMatchObject({ selected: REFUND });
  });
});

describe("ComponentPanel", () => {
  function panel(permissions: string[] = ["read"], isFocus = false) {
    const onSelect = vi.fn();
    const onFocus = vi.fn();
    graphStub((url, init) =>
      init?.method === "PUT" ? { tool: liveRefundPayment.component, mapped: [] } : undefined,
    );
    renderAs(
      <ComponentPanel
        componentId={REFUND}
        projectId={PROJECT}
        isFocus={isFocus}
        onSelect={onSelect}
        onFocus={onFocus}
      />,
      permissions,
    );
    return { onSelect, onFocus };
  }

  it("says what the component acts on and what uses it, with the evidence", async () => {
    const { onSelect, onFocus } = panel();
    const p = await screen.findByTestId("component-panel");
    expect(p).toHaveTextContent("Acts on (10)");
    expect(p).toHaveTextContent("Used by (5)");
    expect(p).toHaveTextContent("Risk" + "WRITE_IRREVERSIBLE");
    const writes = within(p)
      .getAllByTestId("relation")
      .find((r) => r.getAttribute("data-type") === "WRITES")!;
    expect(writes).toHaveTextContent("refund_payment writes payments-db");
    expect(writes).toHaveTextContent("100%");
    await userEvent.click(within(writes).getByText(/Evidence/));
    expect(writes).toHaveTextContent("manifest:support-refund-agent@");
    const observed = within(p)
      .getAllByTestId("relation")
      .find((r) => r.getAttribute("data-direction") === "in" && r.textContent?.includes("observed"))!;
    expect(observed).toHaveTextContent(/^support-refund-agent@\S+ uses refund_payment \(agent version\)/);
    await userEvent.click(within(writes).getByRole("button", { name: "payments-db" }));
    expect(onSelect).toHaveBeenCalledWith(
      liveRefundPayment.relations.find((r) => r.type === "WRITES")!.component_id,
    );
    await userEvent.click(screen.getByRole("button", { name: /Center here/ }));
    expect(onFocus).toHaveBeenCalledWith(REFUND);
    // Mapping a tool needs graph.write.
    expect(screen.queryByRole("form", { name: /Manual mapping/ })).not.toBeInTheDocument();
  });

  it("replaces a tool's manual mapping with the rows the user keeps", async () => {
    panel(["read", "graph.write"], true);
    const form = await screen.findByRole("form", { name: "Manual mapping of refund_payment" });
    expect(within(form).getByText("No manual mapping.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Center here/ })).not.toBeInTheDocument();
    await userEvent.click(within(form).getByRole("button", { name: /Add dependency/ }));
    await userEvent.click(within(form).getByRole("button", { name: /Add dependency/ }));
    await userEvent.selectOptions(
      within(form).getByLabelText("Kind", { selector: "#map-kind-0" }),
      "DATABASE",
    );
    await userEvent.type(within(form).getByLabelText("Name", { selector: "#map-name-0" }), " ledger-db ");
    await userEvent.selectOptions(
      within(form).getByLabelText("Relation", { selector: "#map-relation-0" }),
      "WRITES",
    );
    await userEvent.selectOptions(
      within(form).getByLabelText("Criticality", { selector: "#map-criticality-0" }),
      "CRITICAL",
    );
    await userEvent.type(within(form).getByLabelText("Name", { selector: "#map-name-1" }), "fraud-check");
    await userEvent.click(within(form).getByRole("button", { name: "Remove dependency 2" }));
    await userEvent.click(within(form).getByRole("button", { name: "Save mapping" }));
    const put = calls.find((c) => c.method === "PUT")!;
    expect(put.url).toBe("/api/v1/graph/mappings/refund_payment");
    expect(put.body).toEqual({
      project_id: PROJECT,
      depends_on: [{ kind: "DATABASE", name: "ledger-db", relation: "WRITES", criticality: "CRITICAL" }],
    });
    expect(put.key).toMatch(/^tool-mapping/);
    expect(await within(form).findByRole("status")).toHaveTextContent("Saved: 1 dependency.");
  });
});
