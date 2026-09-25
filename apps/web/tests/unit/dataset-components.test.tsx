import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DatasetDetailView } from "@/components/datasets/dataset-detail";
import { NewDataset } from "@/components/datasets/new-dataset";
import { filterScenarios } from "@/components/datasets/scenario-picker";
import { MeProvider } from "@/components/shell/me-context";
import type { DatasetDetail } from "@/lib/api/evaluation";
import type { SimulationSchemas } from "@/lib/api/simulation";
import { parseTags } from "@/lib/evaluations";
import type { Me } from "@/lib/types";
import { liveDataset } from "./eval-fixtures";

const push = vi.fn();
vi.mock("next/navigation", () => ({
  usePathname: () => "/datasets",
  useRouter: () => ({ push, replace: vi.fn(), refresh: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

const DS = liveDataset.dataset.id;
const PROJECT = liveDataset.dataset.project_id;

function scenario(
  name: string,
  over: Partial<SimulationSchemas["Scenario"]> = {},
): SimulationSchemas["Scenario"] {
  return {
    id: `01a0d778-5c50-7000-8000-${String(name.length).padStart(12, "0")}`,
    organization_id: liveDataset.dataset.organization_id,
    project_id: PROJECT,
    name,
    agent: "support-refund-agent",
    twin: "demo-co-support",
    severity: "critical",
    tags: ["refunds"],
    source: "manual",
    latest_version: 1,
    archived: false,
    created_by: "system:seed",
    created_at: "2026-09-25T07:29:40Z",
    updated_at: "2026-09-25T07:29:40Z",
    ...over,
  };
}

const scenarios = {
  items: [
    ...liveDataset.version.cases.map((c) => scenario(c.scenario)),
    scenario("refund-partial-shipment", { tags: ["shipping"], severity: "high" }),
  ],
  next_cursor: null,
} satisfies SimulationSchemas["ScenarioPage"];

let sent: { url: string; method: string; body: unknown; key: string | null }[] = [];

function stub(handler: (url: string, method: string) => unknown) {
  sent = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (method !== "GET") {
        sent.push({
          url,
          method,
          body: init?.body ? JSON.parse(String(init.body)) : undefined,
          key: new Headers(init?.headers).get("Idempotency-Key"),
        });
      }
      const body = handler(url, method);
      if (body instanceof Response) return body;
      return new Response(JSON.stringify(body ?? { items: [] }), { status: 200 });
    }),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  push.mockReset();
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

describe("dataset helpers", () => {
  it("reads tags typed as text and names the ones the API refuses", () => {
    expect(parseTags("release, refunds  release")).toEqual({ tags: ["release", "refunds"], invalid: [] });
    expect(parseTags("Release,ok,-x")).toEqual({ tags: ["Release", "ok", "-x"], invalid: ["Release", "-x"] });
    expect(parseTags("  ")).toEqual({ tags: [], invalid: [] });
  });

  it("filters scenarios by name, agent or tag", () => {
    const all = scenarios.items;
    expect(filterScenarios(all, "partial").map((s) => s.name)).toEqual(["refund-partial-shipment"]);
    // A tag the name does not contain.
    expect(filterScenarios(all, "shipping").map((s) => s.name)).toEqual(["refund-partial-shipment"]);
    expect(filterScenarios(all, "tenant").map((s) => s.name)).toEqual(["cross-tenant-order"]);
    expect(filterScenarios(all, "SUPPORT-refund")).toHaveLength(all.length);
    expect(filterScenarios(all, "")).toHaveLength(all.length);
  });
});

describe("DatasetDetailView", () => {
  const withV2 = {
    ...liveDataset,
    dataset: { ...liveDataset.dataset, latest_version: 2 },
    version: { ...liveDataset.version, version: 2, case_count: 10 },
  } satisfies DatasetDetail;

  function stubDataset(detail: DatasetDetail = liveDataset) {
    stub((url, method) => {
      if (url === `/api/v1/datasets/${DS}` && method === "GET") return detail;
      if (url.startsWith("/api/v1/scenarios?")) return scenarios;
      if (url === `/api/v1/datasets/${DS}/cases` && method === "POST") return withV2;
      if (url.startsWith(`/api/v1/datasets/${DS}/cases/`) && method === "DELETE") return withV2;
      return { items: [] };
    });
  }

  it("lists each case with its latest result, linking to the compared case", async () => {
    stubDataset();
    renderAs(<DatasetDetailView datasetId={DS} />, ["read"]);
    const rows = await screen.findAllByTestId("dataset-case");
    expect(rows).toHaveLength(9);
    const lie = rows.find((r) => r.getAttribute("data-scenario") === "refund-tool-success-lie")!;
    const link = within(lie).getByRole("link", { name: /Last result of refund-tool-success-lie/ });
    expect(link).toHaveAttribute(
      "href",
      `/evaluations/${liveDataset.version.cases[0]!.last_result!.eval_run_id}/cases/refund-tool-success-lie`,
    );
    expect(lie).toHaveTextContent("New critical failurev1.2.4 Passed → v1.3.0 Failed");
    // The scenario links to its page once the project's scenarios are known.
    expect(await within(lie).findByRole("link", { name: "refund-tool-success-lie" })).toHaveAttribute(
      "href",
      expect.stringMatching(/^\/scenarios\/01a0d778-/),
    );
    // Read-only: nothing to change.
    expect(screen.queryByRole("button", { name: /Add cases/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Remove/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Evaluate/ })).not.toBeInTheDocument();
  });

  it("adds only scenarios not yet in the dataset, as a new version", async () => {
    stubDataset();
    renderAs(<DatasetDetailView datasetId={DS} />, ["read", "scenario.write", "eval.run"]);
    expect(await screen.findByRole("link", { name: /Evaluate/ })).toHaveAttribute(
      "href",
      `/evaluations/new?project_id=${PROJECT}&dataset_id=${DS}`,
    );
    await userEvent.click(screen.getByRole("button", { name: /Add cases/ }));
    const panel = screen.getByTestId("add-cases");
    expect(panel).toHaveTextContent("makes version 2");
    // Already-included scenarios are shown but cannot be chosen again.
    const existing = await within(panel).findByRole("checkbox", { name: /cross-tenant-order/ });
    expect(existing).toBeDisabled();
    expect(existing).toBeChecked();
    await userEvent.click(within(panel).getByRole("button", { name: "Select shown" }));
    await userEvent.type(within(panel).getByLabelText(/Note on the new version/), "partial shipments");
    await userEvent.click(within(panel).getByRole("button", { name: "Add 1 case" }));
    expect(sent).toEqual([
      {
        url: `/api/v1/datasets/${DS}/cases`,
        method: "POST",
        body: { cases: [{ scenario: "refund-partial-shipment" }], note: "partial shipments" },
        key: expect.stringMatching(/^dataset-cases-[0-9a-f]{32}$/),
      },
    ]);
    expect(await screen.findByTestId("dataset-version")).toHaveTextContent("v2");
    expect(screen.queryByTestId("add-cases")).not.toBeInTheDocument();
  });

  it("removes a case only after it is confirmed", async () => {
    stubDataset();
    renderAs(<DatasetDetailView datasetId={DS} />, ["read", "scenario.write"]);
    await userEvent.click(await screen.findByRole("button", { name: "Remove refund-happy-path" }));
    expect(sent).toEqual([]);
    await userEvent.click(screen.getByRole("button", { name: "Keep" }));
    await userEvent.click(screen.getByRole("button", { name: "Remove refund-happy-path" }));
    await userEvent.click(screen.getByRole("button", { name: "Remove" }));
    expect(sent).toEqual([
      {
        url: `/api/v1/datasets/${DS}/cases/refund-happy-path`,
        method: "DELETE",
        body: undefined,
        key: expect.stringMatching(/^dataset-remove-/),
      },
    ]);
  });

  it("does not change an archived dataset or an older version", async () => {
    stubDataset({ ...liveDataset, dataset: { ...liveDataset.dataset, archived: true } });
    const { unmount } = renderAs(<DatasetDetailView datasetId={DS} />, [
      "read",
      "scenario.write",
      "eval.run",
    ]);
    expect(await screen.findByText("archived")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Add cases|Archive/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Evaluate/ })).not.toBeInTheDocument();
    unmount();
    // Version 1 shown while version 2 is the latest.
    stubDataset({ ...liveDataset, dataset: { ...liveDataset.dataset, latest_version: 2 } });
    renderAs(<DatasetDetailView datasetId={DS} />, ["read", "scenario.write"]);
    expect(await screen.findByText("not the latest (v2)")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Add cases/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Remove / })).not.toBeInTheDocument();
  });

  it("archives only after it is confirmed", async () => {
    stubDataset();
    renderAs(<DatasetDetailView datasetId={DS} />, ["read", "scenario.write"]);
    await userEvent.click(await screen.findByRole("button", { name: /Archive/ }));
    const dialog = screen.getByRole("alertdialog", { name: "Archive the dataset" });
    expect(sent).toEqual([]);
    await userEvent.click(within(dialog).getByRole("button", { name: "Archive" }));
    expect(sent.map((s) => [s.method, s.url])).toEqual([["POST", `/api/v1/datasets/${DS}/archive`]]);
  });
});

describe("NewDataset", () => {
  it("creates a dataset of the chosen scenarios with valid tags", async () => {
    stub((url, method) => {
      if (url === "/api/v1/projects") return { items: [{ id: PROJECT, slug: "support", name: "Support" }] };
      if (url.startsWith("/api/v1/scenarios?")) return scenarios;
      if (url === "/api/v1/datasets" && method === "POST") return liveDataset;
      return { items: [] };
    });
    renderAs(<NewDataset />, ["read", "scenario.write"]);
    const create = await screen.findByRole("button", { name: /Create with 0 cases/ });
    await userEvent.click(await screen.findByRole("checkbox", { name: /refund-tool-success-lie/ }));
    await userEvent.click(screen.getByRole("checkbox", { name: /refund-happy-path/ }));
    expect(create).toHaveTextContent("Create with 2 cases");
    // A name or a tag the API would refuse keeps the form from being sent.
    await userEvent.type(screen.getByLabelText("Name"), "Refund Gate");
    expect(screen.getByLabelText("Name")).toHaveAttribute("aria-invalid", "true");
    expect(create).toBeDisabled();
    await userEvent.clear(screen.getByLabelText("Name"));
    await userEvent.type(screen.getByLabelText("Name"), "refund-gate");
    expect(create).toBeEnabled();
    await userEvent.type(screen.getByLabelText("Tags (optional)"), "release, Gate");
    expect(screen.getByText("Not a tag: Gate")).toBeInTheDocument();
    expect(create).toBeDisabled();
    await userEvent.clear(screen.getByLabelText("Tags (optional)"));
    await userEvent.type(screen.getByLabelText("Tags (optional)"), "release, gate");
    expect(create).toBeEnabled();
    await userEvent.click(create);
    expect(sent).toEqual([
      {
        url: "/api/v1/datasets",
        method: "POST",
        body: {
          project_id: PROJECT,
          name: "refund-gate",
          tags: ["release", "gate"],
          cases: [{ scenario: "refund-happy-path" }, { scenario: "refund-tool-success-lie" }],
        },
        key: expect.stringMatching(/^dataset-[0-9a-f]{32}$/),
      },
    ]);
    await vi.waitFor(() => expect(push).toHaveBeenCalledWith(`/datasets/${DS}`));
  });
});
