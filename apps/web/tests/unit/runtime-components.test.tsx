import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApprovalDetail } from "@/components/runtime/approval-detail";
import { ApprovalList } from "@/components/runtime/approval-list";
import { DecisionList } from "@/components/runtime/decision-list";
import { MeProvider } from "@/components/shell/me-context";
import type { ApprovalDetail as ApprovalDetailData } from "@/lib/api/runtime";
import type { Me } from "@/lib/types";
import {
  liveApproved,
  liveDecisions,
  livePendingApproval,
  livePendingApprovals,
  livePolicies,
  liveTraceDecisions,
  liveUsedApproval,
} from "./runtime-fixtures";

const replace = vi.fn();
let search = new URLSearchParams();
vi.mock("next/navigation", () => ({
  usePathname: () => "/approvals",
  useRouter: () => ({ push: vi.fn(), replace, refresh: vi.fn() }),
  useSearchParams: () => search,
}));

const PROJECT = livePendingApproval.project_id;
const PENDING = livePendingApproval.id;
const USED = liveUsedApproval.id;
/** A minute before the pending request expires. */
const BEFORE = new Date(new Date(livePendingApproval.expires_at).getTime() - 60_000);

type Handler = (url: string, init: RequestInit | undefined) => unknown;
let sent: { url: string; method: string; body: unknown; key: string | null }[] = [];
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
          method,
          body: init?.body ? JSON.parse(String(init.body)) : undefined,
          key: headers.get("Idempotency-Key"),
        });
      }
      const body = handler(url, init);
      if (body instanceof Response) return body;
      if (body === undefined)
        return new Response(JSON.stringify({ error: { code: "NOT_FOUND" } }), { status: 404 });
      return new Response(JSON.stringify(body), { status: 200 });
    }),
  );
}

beforeEach(() => {
  // Only the clock is faked: the pages compare expiries with `new Date()`.
  vi.useFakeTimers({ toFake: ["Date"], shouldAdvanceTime: true });
  vi.setSystemTime(BEFORE);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
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

function catalog(url: string): unknown {
  if (url === "/api/v1/projects") return projects;
  if (url.startsWith("/api/v1/policies?")) return livePolicies;
  return undefined;
}

function error(status: number, code: string, message: string) {
  return new Response(JSON.stringify({ error: { code, message, request_id: "r".repeat(32) } }), { status });
}

describe("ApprovalList", () => {
  it("opens on what waits for a person, with the exact action and its expiry", async () => {
    stub((url) => (url.startsWith("/api/v1/approvals?") ? livePendingApprovals : catalog(url)));
    renderAs(<ApprovalList />, ["read"]);
    const rows = await screen.findAllByTestId("approval-row");
    expect(rows).toHaveLength(livePendingApprovals.items.length);
    expect(fetched).toContain(`/api/v1/approvals?project_id=${PROJECT}&status=PENDING&limit=25`);
    expect(screen.getAllByRole("columnheader").map((h) => h.textContent)).toEqual([
      "Action",
      "Tool",
      "Agent",
      "Policy",
      "Status",
      "Expires",
      "Requested",
      "Trace",
    ]);
    const row = rows.find((r) => r.dataset.approvalId === PENDING)!;
    const cells = within(row)
      .getAllByRole("cell")
      .map((c) => c.textContent);
    expect(cells.slice(0, 6)).toEqual([
      `${livePendingApproval.summary}Refunds over 100 USD need a person's approval.`,
      "refund_paymentWrite irreversible",
      "support-refund-agentv1.3.1 · production",
      "refund-limitsover-automatic-limit",
      "Pending",
      "in 1 min",
    ]);
    expect(
      within(row).getByRole("link", { name: `Approval request: ${livePendingApproval.summary}` }),
    ).toHaveAttribute("href", `/approvals/${PENDING}?project_id=${PROJECT}`);
    expect(
      within(row).getByRole("link", { name: `Trace of ${livePendingApproval.summary}` }),
    ).toHaveAttribute("href", `/traces/${livePendingApproval.trace_id}`);
  });

  it("shows every status on request, and says when nothing waits", async () => {
    search = new URLSearchParams({ status: "all" });
    stub((url) => (url.startsWith("/api/v1/approvals?") ? { items: [] } : catalog(url)));
    renderAs(<ApprovalList />, ["read"]);
    expect(await screen.findByText("No approval requests")).toBeInTheDocument();
    expect(fetched).toContain(`/api/v1/approvals?project_id=${PROJECT}&limit=25`);
    await userEvent.selectOptions(screen.getByLabelText("Show"), "USED");
    expect(replace).toHaveBeenCalledWith("/approvals?status=USED", { scroll: false });
  });

  it("does not show a status it does not know as a filter", async () => {
    search = new URLSearchParams({ status: "BOGUS" });
    stub((url) => (url.startsWith("/api/v1/approvals?") ? { items: [] } : catalog(url)));
    renderAs(<ApprovalList />, ["read"]);
    expect(await screen.findByText("Nothing is waiting for a person")).toBeInTheDocument();
    expect(fetched).toContain(`/api/v1/approvals?project_id=${PROJECT}&status=PENDING&limit=25`);
  });
});

/** One approval's API: the request, and what approving or denying answers. */
function approvalAPI(detail: ApprovalDetailData, answers: Record<string, unknown> = {}): Handler {
  let current = detail;
  return (url, init) => {
    const method = init?.method ?? "GET";
    if (method !== "GET") {
      const answer = answers[`${method} ${url}`];
      if (answer && !(answer instanceof Response)) current = { ...current, ...(answer as object) };
      return answer;
    }
    if (url === `/api/v1/approvals/${detail.id}?project_id=${PROJECT}`) return current;
    return catalog(url);
  };
}

describe("ApprovalDetail", () => {
  it("shows the exact action, why it waits and until when", async () => {
    search = new URLSearchParams({ project_id: PROJECT });
    stub(approvalAPI(livePendingApproval));
    renderAs(<ApprovalDetail approvalId={PENDING} />, ["read"]);
    expect(await screen.findByTestId("approval-header")).toHaveTextContent(
      `${livePendingApproval.summary}Pending`,
    );
    expect(
      screen.getAllByTestId("argument-row").map((r) =>
        within(r)
          .getAllByRole("cell")
          .map((c) => c.textContent),
      ),
    ).toEqual([
      ["amount", "150"],
      ["idempotency_key", `"${livePendingApproval.arguments.idempotency_key}"`],
      ["order_id", `"${livePendingApproval.arguments.order_id}"`],
    ]);
    expect(screen.getByTestId("approval-reason")).toHaveTextContent(
      "Refunds over 100 USD need a person's approval.",
    );
    expect(screen.getByText(/\(in 1 min\)/)).toBeInTheDocument();
    // The policy links to its page (found by name among the tool's policies).
    const refund = livePolicies.items.find((p) => p.name === "refund-limits")!;
    expect(await screen.findByRole("link", { name: "refund-limits" })).toHaveAttribute(
      "href",
      `/policies/${refund.id}?project_id=${PROJECT}`,
    );
    expect(fetched).toContain(`/api/v1/policies?project_id=${PROJECT}&tool=refund_payment`);
    expect(screen.getByText("Not used yet")).toBeInTheDocument();
    // Without approval.decide there is nothing to click.
    expect(screen.queryByTestId("decide-form")).not.toBeInTheDocument();
    expect(screen.getByRole("note")).toHaveTextContent("Your role cannot decide approval requests.");
  });

  it("approves the exact action with a reason, once", async () => {
    search = new URLSearchParams({ project_id: PROJECT });
    stub(
      approvalAPI(livePendingApproval, {
        [`POST /api/v1/approvals/${PENDING}/approve`]: liveApproved,
      }),
    );
    renderAs(<ApprovalDetail approvalId={PENDING} />, ["read", "approval.decide"]);
    const form = await screen.findByTestId("decide-form");
    const approve = within(form).getByRole("button", { name: "Approve this action" });
    // A decision needs a reason.
    expect(approve).toBeDisabled();
    await userEvent.type(within(form).getByLabelText("Reason"), "  Damaged item confirmed.  ");
    await userEvent.click(approve);
    expect(await screen.findByTestId("approval-header")).toHaveTextContent("Approved");
    expect(sent).toHaveLength(1);
    expect(sent[0]).toMatchObject({
      url: `/api/v1/approvals/${PENDING}/approve`,
      method: "POST",
      body: { project_id: PROJECT, reason: "Damaged item confirmed." },
    });
    expect(sent[0]!.key).toMatch(/\S{8,}/);
    // Approved: the form is gone; the agent runs it with its token.
    expect(screen.queryByTestId("decide-form")).not.toBeInTheDocument();
  });

  it("denies it, and shows the service's refusal when it has one", async () => {
    search = new URLSearchParams({ project_id: PROJECT });
    stub(
      approvalAPI(livePendingApproval, {
        [`POST /api/v1/approvals/${PENDING}/deny`]: error(
          409,
          "APPROVAL_DECIDED",
          "Someone else already decided this request.",
        ),
      }),
    );
    renderAs(<ApprovalDetail approvalId={PENDING} />, ["read", "approval.decide"]);
    const form = await screen.findByTestId("decide-form");
    await userEvent.type(within(form).getByLabelText("Reason"), "A replacement was sent.");
    await userEvent.click(within(form).getByRole("button", { name: "Deny" }));
    expect(await within(form).findByText(/Someone else already decided this request/)).toBeInTheDocument();
    expect(sent.map((s) => s.url)).toEqual([`/api/v1/approvals/${PENDING}/deny`]);
  });

  it("offers nothing once the request expired, even while it still says pending", async () => {
    vi.setSystemTime(new Date(new Date(livePendingApproval.expires_at).getTime() + 3 * 60_000));
    search = new URLSearchParams({ project_id: PROJECT });
    stub(approvalAPI(livePendingApproval));
    renderAs(<ApprovalDetail approvalId={PENDING} />, ["read", "approval.decide"]);
    expect(await screen.findByRole("note")).toHaveTextContent(
      "This request expired 3 min ago; it can no longer be approved.",
    );
    expect(screen.queryByTestId("decide-form")).not.toBeInTheDocument();
  });

  it("shows that a changed request was refused and the exact one ran once", async () => {
    search = new URLSearchParams({ project_id: PROJECT });
    stub(approvalAPI(liveUsedApproval));
    renderAs(<ApprovalDetail approvalId={USED} />, ["read", "approval.decide"]);
    expect(await screen.findByTestId("approval-header")).toHaveTextContent("Used");
    expect(screen.queryByTestId("decide-form")).not.toBeInTheDocument();
    const [changed, ran] = screen.getAllByTestId("attempt-row");
    expect(changed).toHaveAttribute("data-result", "mismatch");
    expect(within(changed!).getByText("Different action")).toBeInTheDocument();
    expect(within(changed!).getByRole("list", { name: "Changes" })).toHaveTextContent("amount: 150 → 200");
    expect(ran).toHaveAttribute("data-result", "executed");
    expect(within(ran!).getByText("the same action")).toBeInTheDocument();
    // The decision that asked and the run it allowed.
    expect(screen.getByText("The decision that asked")).toBeInTheDocument();
    expect(screen.getByText("The approved run")).toBeInTheDocument();
    expect(screen.getByText("HTTP 200")).toBeInTheDocument();
    expect(screen.getByText(liveUsedApproval.decision_reason!)).toBeInTheDocument();
    // A used request no longer counts down to its expiry.
    expect(screen.queryByText(/\(in \d/)).not.toBeInTheDocument();
  });

  it("says a run faster than a millisecond took under one", async () => {
    search = new URLSearchParams({ project_id: PROJECT });
    const fast = {
      ...liveUsedApproval,
      used_decision: { ...liveUsedApproval.used_decision!, latency_ms: 0 },
    };
    stub(approvalAPI(fast));
    renderAs(<ApprovalDetail approvalId={USED} />, ["read"]);
    expect(await screen.findByText("< 1 ms")).toBeInTheDocument();
  });

  it("says when the request is not there", async () => {
    search = new URLSearchParams({ project_id: PROJECT });
    stub((url) =>
      url.startsWith(`/api/v1/approvals/${PENDING}`)
        ? error(404, "NOT_FOUND", "approval request not found")
        : catalog(url),
    );
    renderAs(<ApprovalDetail approvalId={PENDING} />, ["read"]);
    expect(await screen.findByText(/approval request not found/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "← Approvals" })).toHaveAttribute("href", "/approvals");
  });
});

describe("DecisionList", () => {
  it("lists what the gateway decided, with links to the approval and the trace", async () => {
    stub((url) => (url.startsWith("/api/v1/policy-decisions?") ? liveDecisions : catalog(url)));
    renderAs(<DecisionList />, ["read"]);
    const rows = await screen.findAllByTestId("decision-row");
    expect(rows).toHaveLength(liveDecisions.items.length);
    expect(fetched).toContain(`/api/v1/policy-decisions?project_id=${PROJECT}&limit=50`);
    const refused = liveDecisions.items.find((d) => d.outcome === "approval_refused")!;
    const row = rows.find((r) => r.dataset.decisionId === refused.id)!;
    expect(within(row).getByText("Approval refused")).toBeInTheDocument();
    expect(within(row).getByText("Needs approval")).toBeInTheDocument();
    expect(within(row).getByRole("link", { name: `Approval request of ${refused.summary}` })).toHaveAttribute(
      "href",
      `/approvals/${refused.approval_id}?project_id=${PROJECT}`,
    );
  });

  it("filters by effect, outcome, tool and trace through the contract's query", async () => {
    const trace = liveTraceDecisions.items[0]!.trace_id!;
    search = new URLSearchParams({
      effect: "require_approval",
      outcome: "executed",
      tool: "refund_payment",
      trace_id: trace.toUpperCase(),
      project_id: PROJECT,
    });
    stub((url) => (url.startsWith("/api/v1/policy-decisions?") ? { items: [] } : catalog(url)));
    renderAs(<DecisionList />, ["read"]);
    expect(await screen.findByText("No decisions match")).toBeInTheDocument();
    expect(fetched).toContain(
      `/api/v1/policy-decisions?project_id=${PROJECT}&effect=require_approval&outcome=executed&tool=refund_payment&trace_id=${trace}&limit=50`,
    );
    // The trace the page is filtered by is shown.
    expect(screen.getByLabelText("Trace")).toHaveValue(trace);
    await userEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(replace).toHaveBeenCalledWith(`/approvals?project_id=${PROJECT}`, { scroll: false });
  });

  it("filters by a trace typed in", async () => {
    const trace = liveTraceDecisions.items[0]!.trace_id!;
    stub((url) => (url.startsWith("/api/v1/policy-decisions?") ? liveDecisions : catalog(url)));
    renderAs(<DecisionList />, ["read"]);
    await screen.findAllByTestId("decision-row");
    await userEvent.type(screen.getByLabelText("Trace"), `${trace.toUpperCase()}{Enter}`);
    expect(replace).toHaveBeenCalledWith(`/approvals?trace_id=${trace}`, { scroll: false });
  });

  it("ignores a malformed trace id instead of sending it", async () => {
    search = new URLSearchParams({ trace_id: "not-a-trace" });
    stub((url) => (url.startsWith("/api/v1/policy-decisions?") ? liveTraceDecisions : catalog(url)));
    renderAs(<DecisionList />, ["read"]);
    await screen.findAllByTestId("decision-row");
    expect(fetched).toContain(`/api/v1/policy-decisions?project_id=${PROJECT}&limit=50`);
  });
});
