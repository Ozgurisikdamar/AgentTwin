import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { NewPolicy } from "@/components/runtime/new-policy";
import { PolicyDetail, selectedVersion } from "@/components/runtime/policy-detail";
import { PolicyList } from "@/components/runtime/policy-list";
import { MeProvider } from "@/components/shell/me-context";
import type { PolicyDetail as PolicyDetailData, PolicyVersionDetail } from "@/lib/api/runtime";
import type { Me } from "@/lib/types";
import {
  liveActivatedPolicy,
  liveAddedVersion,
  liveCreatedPolicy,
  liveDeactivatedPolicy,
  liveFailingTestReport,
  liveInactivePolicy,
  liveInvalidPolicy,
  liveNotActivatable,
  livePolicies,
  livePolicy,
  livePolicyExists,
  livePolicyVersion,
  liveTestReport,
  liveUnchangedVersion,
} from "./runtime-fixtures";

const push = vi.fn();
const replace = vi.fn();
let search = new URLSearchParams();
vi.mock("next/navigation", () => ({
  usePathname: () => "/policies",
  useRouter: () => ({ push, replace, refresh: vi.fn() }),
  useSearchParams: () => search,
}));

const PROJECT = "01a0d9c7-d08c-7d27-b938-bcae1f1b7894";
const REFUND = livePolicy.id;
const WRITER = ["read", "policy.write", "policy.test", "policy.activate"];

type Handler = (url: string, init: RequestInit | undefined) => unknown;
let sent: { url: string; body: unknown; key: string | null }[] = [];
let fetched: string[] = [];

function stub(handler: Handler) {
  sent = [];
  fetched = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (method === "GET") fetched.push(url);
      else {
        const headers = new Headers(init?.headers);
        sent.push({
          url,
          body: init?.body ? JSON.parse(String(init.body)) : undefined,
          key: headers.get("Idempotency-Key"),
        });
      }
      const body = handler(url, init);
      if (body instanceof Response) return body;
      if (body === undefined) {
        return new Response(JSON.stringify({ error: { code: "NOT_FOUND", message: "not found" } }), {
          status: 404,
        });
      }
      return new Response(JSON.stringify(body), { status: 200 });
    }),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  push.mockReset();
  replace.mockReset();
  search = new URLSearchParams();
});

function renderAs(ui: ReactNode, permissions: string[]) {
  const me = {
    user: { id: "u-1", email: "o@demo.agenttwin.dev", display_name: "Olivia" },
    principal: { org: "o", sub: "u-1", role: "owner" },
    permissions,
  } as unknown as Me;
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MeProvider me={me}>{ui}</MeProvider>
    </QueryClientProvider>,
  );
}

const projects = { items: [{ id: PROJECT, slug: "support", name: "Customer Support" }] };

function answer(e: { status: number; body: unknown }) {
  return new Response(JSON.stringify(e.body), { status: e.status });
}

/** The refund-limits policy's API; `answers` are what each POST answers. */
function policyAPI(
  detail: PolicyDetailData = livePolicy,
  answers: Record<string, unknown> = {},
  version: PolicyVersionDetail = livePolicyVersion,
): Handler {
  let current = detail;
  return (url, init) => {
    const method = init?.method ?? "GET";
    if (method !== "GET") {
      const a = answers[`${method} ${url}`];
      if (a && !(a instanceof Response) && "latest_version" in (a as object))
        current = { ...current, ...(a as object) };
      return a;
    }
    if (url === "/api/v1/projects") return projects;
    if (url === `/api/v1/policies/${detail.id}?project_id=${PROJECT}`) return current;
    if (url === `/api/v1/policies/${detail.id}/versions/${version.version}?project_id=${PROJECT}`)
      return version;
    return undefined;
  };
}

describe("PolicyList", () => {
  it("lists the policies, which version guards each tool and since when", async () => {
    stub((url) =>
      url === "/api/v1/projects" ? projects : url.startsWith("/api/v1/policies?") ? livePolicies : undefined,
    );
    renderAs(<PolicyList />, ["read"]);
    const rows = await screen.findAllByTestId("policy-row");
    expect(rows).toHaveLength(2);
    expect(fetched).toContain(`/api/v1/policies?project_id=${PROJECT}`);
    const refund = rows.find((r) => r.dataset.policyId === REFUND)!;
    const cells = within(refund)
      .getAllByRole("cell")
      .map((c) => c.textContent);
    expect(cells[0]).toMatch(/^refund-limitsA refund is the support agent's only irreversible action/);
    expect(cells.slice(1, 4)).toEqual(["refund_payment", "v1 active", "v1"]);
    expect(within(refund).getByRole("link", { name: "Policy refund-limits" })).toHaveAttribute(
      "href",
      `/policies/${REFUND}?project_id=${PROJECT}`,
    );
    // Reading policies is not writing them.
    expect(screen.queryByRole("link", { name: "New policy" })).not.toBeInTheDocument();
  });

  it("offers a new policy to a writer, and filters by tool", async () => {
    search = new URLSearchParams({ tool: "send_email" });
    stub((url) =>
      url === "/api/v1/projects" ? projects : url.startsWith("/api/v1/policies?") ? { items: [] } : undefined,
    );
    renderAs(<PolicyList />, WRITER);
    expect(await screen.findByText("No policy guards send_email")).toBeInTheDocument();
    expect(fetched).toContain(`/api/v1/policies?project_id=${PROJECT}&tool=send_email`);
    expect(screen.getByRole("link", { name: "New policy" })).toHaveAttribute(
      "href",
      `/policies/new?project_id=${PROJECT}`,
    );
  });

  it("does not send a tool name the gateway could not have", async () => {
    search = new URLSearchParams({ tool: "a b/c" });
    stub((url) =>
      url === "/api/v1/projects" ? projects : url.startsWith("/api/v1/policies?") ? livePolicies : undefined,
    );
    renderAs(<PolicyList />, ["read"]);
    await screen.findAllByTestId("policy-row");
    expect(fetched).toContain(`/api/v1/policies?project_id=${PROJECT}`);
  });

  it("flags a policy whose newest version is not the active one", async () => {
    const newer = {
      items: [
        { ...livePolicies.items[0]!, latest_version: 3, active: { id: livePolicy.active!.id, version: 1 } },
      ],
    };
    stub((url) =>
      url === "/api/v1/projects" ? projects : url.startsWith("/api/v1/policies?") ? newer : undefined,
    );
    renderAs(<PolicyList />, ["read"]);
    const [row] = await screen.findAllByTestId("policy-row");
    expect(row).toHaveTextContent("v3 not active yet");
  });
});

describe("selectedVersion", () => {
  it("shows the asked version when the policy has it, else the active one, else the latest", () => {
    expect(selectedVersion(livePolicy, "1")).toBe(1);
    expect(selectedVersion(livePolicy, "7")).toBe(1);
    expect(selectedVersion(livePolicy, "x")).toBe(1);
    expect(selectedVersion(liveInactivePolicy, null)).toBe(2);
    expect(selectedVersion(liveInactivePolicy, "1")).toBe(1);
    expect(selectedVersion({ ...liveInactivePolicy, active: { id: "v", version: 1 } }, null)).toBe(1);
  });
});

describe("PolicyDetail", () => {
  it("shows what the active version decides: settings, rules in order, thresholds, tests", async () => {
    search = new URLSearchParams({ project_id: PROJECT });
    stub(policyAPI());
    renderAs(<PolicyDetail policyId={REFUND} />, ["read"]);
    expect(await screen.findByTestId("policy-header")).toHaveTextContent("refund-limitsv1 active");
    expect(screen.getAllByTestId("version-row")).toHaveLength(1);
    expect(screen.getAllByTestId("version-row")[0]).toHaveAttribute("aria-current", "true");
    const view = await screen.findByTestId("version-view");
    expect(
      within(view)
        .getAllByTestId("rule-row")
        .map((r) =>
          within(r)
            .getAllByRole("cell")
            .map((c) => c.textContent)
            .slice(0, 4),
        ),
    ).toEqual([
      ["1", "over-automatic-limit", "args.amount > 100", "Needs approval"],
      ["2", "over-finance-limit", "args.amount > 1000", "Deny"],
      ["3", "one-refund-per-conversation", "trace.calls >= 1", "Deny"],
    ]);
    expect(within(view).getByRole("list", { name: "Thresholds" })).toHaveTextContent(
      "args.amount > 100args.amount > 1000trace.calls >= 1",
    );
    expect(within(view).getAllByTestId("test-row")).toHaveLength(6);
    expect(within(view).getByText("1 h after the request")).toBeInTheDocument();
    expect(within(view).getByText(/A rule that cannot be evaluated denies the call/)).toBeInTheDocument();
    // A reader gets no actions.
    expect(screen.queryByTestId("version-actions")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit as a new version" })).not.toBeInTheDocument();
  });

  it("runs the saved version's tests and shows the report with its boundaries", async () => {
    search = new URLSearchParams({ project_id: PROJECT });
    stub(policyAPI(livePolicy, { "POST /api/v1/policies/test": liveTestReport }));
    renderAs(<PolicyDetail policyId={REFUND} />, ["read", "policy.test"]);
    await userEvent.click(await screen.findByRole("button", { name: "Run tests" }));
    const report = await screen.findByTestId("test-report");
    expect(sent).toEqual([
      {
        url: "/api/v1/policies/test",
        body: { project_id: PROJECT, policy_id: REFUND, version: 1 },
        key: null,
      },
    ]);
    expect(within(report).getByTestId("report-verdict")).toHaveTextContent(
      "All 6 tests pass: this version can be activated.",
    );
    expect(within(report).getAllByTestId("result-row")).toHaveLength(6);
    const boundaries = within(report).getAllByTestId("boundary");
    expect(boundaries).toHaveLength(3);
    expect(boundaries[0]).toHaveTextContent("100 → Allow · 100.01 → Needs approval (over-automatic-limit)");
    // Only a person who may activate sees activation.
    expect(screen.queryByRole("button", { name: /Activate/ })).not.toBeInTheDocument();
  });

  it("deactivates the active policy only with a reason", async () => {
    search = new URLSearchParams({ project_id: PROJECT });
    stub(
      policyAPI(livePolicy, {
        [`POST /api/v1/policies/${REFUND}/deactivate`]: {
          ...livePolicy,
          ...liveDeactivatedPolicy,
          active: null,
        },
      }),
    );
    renderAs(<PolicyDetail policyId={REFUND} />, WRITER);
    const off = await screen.findByRole("button", { name: "Deactivate the policy" });
    // The active version cannot be activated again.
    expect(screen.queryByRole("button", { name: "Activate v1" })).not.toBeInTheDocument();
    expect(off).toBeDisabled();
    await userEvent.type(screen.getByLabelText(/Reason/), "Finance takes refunds over.");
    await userEvent.click(off);
    expect(await screen.findByRole("note")).toHaveTextContent("No version is active");
    expect(sent).toHaveLength(1);
    expect(sent[0]).toMatchObject({
      url: `/api/v1/policies/${REFUND}/deactivate`,
      body: { project_id: PROJECT, reason: "Finance takes refunds over." },
    });
    expect(sent[0]!.key).toMatch(/\S{8,}/);
  });

  it("activates an inactive version, and says why the gateway refused one", async () => {
    const email = liveInactivePolicy;
    const v2 = { ...liveAddedVersion.version, variables: livePolicyVersion.variables, thresholds: [] };
    search = new URLSearchParams({ project_id: PROJECT });
    stub(
      policyAPI(
        email,
        {
          "POST /api/v1/policies/test": liveFailingTestReport,
          [`POST /api/v1/policies/${email.id}/activate`]: answer(liveNotActivatable),
        },
        v2,
      ),
    );
    renderAs(<PolicyDetail policyId={email.id} />, WRITER);
    expect(await screen.findByTestId("policy-header")).toHaveTextContent("email-recipientsNot active");
    await screen.findByTestId("version-view");
    await userEvent.click(screen.getByRole("button", { name: "Run tests" }));
    expect(await screen.findByTestId("report-verdict")).toHaveTextContent("1 of 6 tests fail");
    expect(sent.shift()).toMatchObject({
      url: "/api/v1/policies/test",
      body: { project_id: PROJECT, policy_id: email.id, version: 2 },
    });
    await userEvent.click(screen.getByRole("button", { name: "Activate v2" }));
    const problems = await screen.findByRole("list", { name: "Why the gateway refused" });
    expect(problems).toHaveTextContent(
      'spec.tests[1]"someone else" fails: expected allow, the policy decided deny',
    );
    expect(sent[0]).toMatchObject({
      url: `/api/v1/policies/${email.id}/activate`,
      body: { project_id: PROJECT, version: 2 },
    });
  });

  it("sends the reason with an activation when one is given", async () => {
    search = new URLSearchParams({ project_id: PROJECT, version: "1" });
    const v1 = { ...livePolicyVersion, id: liveInactivePolicy.versions[1]!.id, active: false };
    stub(
      policyAPI(
        liveInactivePolicy,
        { [`POST /api/v1/policies/${liveInactivePolicy.id}/activate`]: liveActivatedPolicy },
        v1,
      ),
    );
    renderAs(<PolicyDetail policyId={liveInactivePolicy.id} />, WRITER);
    await screen.findByTestId("version-view");
    await userEvent.type(screen.getByLabelText(/Reason/), "  Emails only reach customers.  ");
    await userEvent.click(screen.getByRole("button", { name: "Activate v1" }));
    expect(await screen.findByTestId("policy-header")).toHaveTextContent("v1 active");
    expect(sent[0]!.body).toEqual({
      project_id: PROJECT,
      version: 1,
      reason: "Emails only reach customers.",
    });
    expect(sent[0]!.key).toMatch(/\S{8,}/);
  });

  it("switches versions through the URL", async () => {
    search = new URLSearchParams({ project_id: PROJECT });
    stub(policyAPI(liveInactivePolicy, {}, { ...livePolicyVersion, version: 2 }));
    renderAs(<PolicyDetail policyId={liveInactivePolicy.id} />, ["read"]);
    await screen.findByTestId("version-view");
    await userEvent.click(screen.getByRole("button", { name: "Show version 1" }));
    expect(replace).toHaveBeenCalledWith(`/policies?project_id=${PROJECT}&version=1`, { scroll: false });
  });

  it("edits a version into a draft, tests it, and stores nothing when nothing changed", async () => {
    search = new URLSearchParams({ project_id: PROJECT });
    stub(
      policyAPI(livePolicy, {
        "POST /api/v1/policies/test": liveFailingTestReport,
        [`POST /api/v1/policies/${REFUND}/versions`]: liveUnchangedVersion,
      }),
    );
    renderAs(<PolicyDetail policyId={REFUND} />, WRITER);
    await userEvent.click(await screen.findByRole("button", { name: "Edit as a new version" }));
    const editor = screen.getByTestId("new-version");
    const doc = within(editor).getByLabelText("Policy document");
    expect(doc).toHaveValue(livePolicyVersion.document);
    const draft = livePolicyVersion.document.replace('"args.amount > 100"', '"args.amount > 500"');
    fireEvent.change(doc, { target: { value: draft } });
    await userEvent.click(within(editor).getByRole("button", { name: "Test the draft" }));
    expect(await within(editor).findByTestId("report-verdict")).toHaveTextContent(
      "1 of 6 tests fail: this version cannot be activated.",
    );
    expect(within(editor).getAllByTestId("result-row")[2]).toHaveAttribute("data-passed", "false");
    expect(sent[0]).toEqual({
      url: "/api/v1/policies/test",
      body: { project_id: PROJECT, document: draft },
      key: null,
    });
    await userEvent.click(within(editor).getByRole("button", { name: "Save as a new version" }));
    expect(await within(editor).findByRole("status")).toHaveTextContent(
      "It decides exactly what v1 decides; nothing new was stored.",
    );
    expect(sent[1]).toMatchObject({
      url: `/api/v1/policies/${REFUND}/versions`,
      body: { project_id: PROJECT, document: draft },
    });
    expect(replace).not.toHaveBeenCalled();
  });

  it("opens the saved version", async () => {
    search = new URLSearchParams({ project_id: PROJECT });
    stub(
      policyAPI(livePolicy, {
        [`POST /api/v1/policies/${REFUND}/versions`]: { ...liveAddedVersion, created: true },
      }),
    );
    renderAs(<PolicyDetail policyId={REFUND} />, WRITER);
    await userEvent.click(await screen.findByRole("button", { name: "Edit as a new version" }));
    await userEvent.click(screen.getByRole("button", { name: "Save as a new version" }));
    await vi.waitFor(() =>
      expect(replace).toHaveBeenCalledWith(`/policies?project_id=${PROJECT}&version=2`, { scroll: false }),
    );
  });

  it("says when the policy is not there", async () => {
    search = new URLSearchParams({ project_id: PROJECT });
    stub((url) => (url === "/api/v1/projects" ? projects : undefined));
    renderAs(<PolicyDetail policyId={REFUND} />, ["read"]);
    expect(await screen.findByRole("link", { name: "← Policies" })).toHaveAttribute("href", "/policies");
    expect(screen.getByRole("alert")).toHaveTextContent("not found");
  });
});

describe("NewPolicy", () => {
  it("starts from a document for the tool asked for, tests it and saves it as version 1", async () => {
    search = new URLSearchParams({ project_id: PROJECT, tool: "send_email" });
    stub((url, init) => {
      if (url === "/api/v1/projects") return projects;
      if (init?.method === "POST" && url === "/api/v1/policies/test") return liveTestReport;
      if (init?.method === "POST" && url === "/api/v1/policies") return liveCreatedPolicy;
      return undefined;
    });
    renderAs(<NewPolicy />, WRITER);
    const doc = await screen.findByLabelText("Policy document");
    expect((doc as HTMLTextAreaElement).value).toContain("tool: send_email");
    expect((doc as HTMLTextAreaElement).value).toContain("name: send-email-limits");
    await userEvent.click(screen.getByRole("button", { name: "Test the draft" }));
    expect(await screen.findByTestId("report-verdict")).toHaveTextContent("All 6 tests pass");
    await userEvent.click(screen.getByRole("button", { name: "Save the policy" }));
    await vi.waitFor(() =>
      expect(push).toHaveBeenCalledWith(`/policies/${liveCreatedPolicy.policy.id}?project_id=${PROJECT}`),
    );
    expect(sent.map((s) => s.url)).toEqual(["/api/v1/policies/test", "/api/v1/policies"]);
    expect(sent[1]!.body).toEqual({ project_id: PROJECT, document: (doc as HTMLTextAreaElement).value });
    expect(sent[1]!.key).toMatch(/\S{8,}/);
  });

  it("shows why a document is not valid, and a taken name", async () => {
    search = new URLSearchParams({ project_id: PROJECT });
    stub((url, init) => {
      if (url === "/api/v1/projects") return projects;
      if (init?.method === "POST" && url === "/api/v1/policies/test") return answer(liveInvalidPolicy);
      if (init?.method === "POST" && url === "/api/v1/policies") return answer(livePolicyExists);
      return undefined;
    });
    renderAs(<NewPolicy />, WRITER);
    await userEvent.click(await screen.findByRole("button", { name: "Test the draft" }));
    expect(await screen.findByRole("list", { name: "Problems in the document" })).toHaveTextContent(
      "spec.rules[0].when",
    );
    await userEvent.click(screen.getByRole("button", { name: "Save the policy" }));
    expect(
      await screen.findByText(/A policy named "email-recipients" exists in this project/),
    ).toBeInTheDocument();
    expect(push).not.toHaveBeenCalled();
  });

  it("lets a person who may only test do just that", async () => {
    search = new URLSearchParams({ project_id: PROJECT });
    stub((url) => (url === "/api/v1/projects" ? projects : undefined));
    renderAs(<NewPolicy />, ["read", "policy.test"]);
    expect(await screen.findByRole("note")).toHaveTextContent(
      "Your role cannot write policies; you can still test a draft.",
    );
    expect(screen.getByRole("button", { name: "Test the draft" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Save the policy" })).not.toBeInTheDocument();
  });
});
