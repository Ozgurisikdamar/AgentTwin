import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RegressionDetail } from "@/components/regressions/regression-detail";
import { RegressionList } from "@/components/regressions/regression-list";
import { MeProvider } from "@/components/shell/me-context";
import type { Regression, RegressionDetail as RegressionDetailData } from "@/lib/api/evaluation";
import type { Me } from "@/lib/types";
import {
  liveCandidate,
  liveDraft,
  liveFixed,
  liveIncompleteDraft,
  livePromoted,
  liveRegressionInbox,
} from "./regression-fixtures";

const push = vi.fn();
const replace = vi.fn();
let search = new URLSearchParams();
vi.mock("next/navigation", () => ({
  usePathname: () => "/regressions",
  useRouter: () => ({ push, replace, refresh: vi.fn() }),
  useSearchParams: () => search,
}));

const DUP = liveCandidate.regression;
const PROJECT = DUP.project_id;
const OPEN = "status=CANDIDATE%2CCONFIRMED%2CREOPENED";

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
      return new Response(JSON.stringify(body ?? { items: [] }), { status: 200 });
    }),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  push.mockReset();
  replace.mockReset();
  search = new URLSearchParams();
});

function renderAs(ui: ReactNode, permissions: string[], role = "engineer") {
  const me = {
    user: { id: "u-1", email: "e@demo.agenttwin.dev", display_name: "Eve" },
    principal: { org: "o", sub: "u-1", role },
    permissions,
  } as unknown as Me;
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MeProvider me={me}>{ui}</MeProvider>
    </QueryClientProvider>,
  );
}

const REVIEWER = ["read", "review.write", "regression.promote"];
const ENGINEER = ["read", "review.write"];

const projects = { items: [{ id: PROJECT, slug: "support", name: "Support" }] };
const agents = {
  items: [
    { id: "01a0d9b2-0000-7000-8000-00000000a001", project_id: PROJECT, name: DUP.agent, version_count: 3 },
  ],
};

function catalog(url: string): unknown {
  if (url === "/api/v1/projects") return projects;
  if (url === `/api/v1/projects/${PROJECT}/agents`) return agents;
  return undefined;
}

function error(status: number, code: string, message: string, details?: Record<string, unknown>) {
  return new Response(JSON.stringify({ error: { code, message, request_id: "r".repeat(32), details } }), {
    status,
  });
}

/**
 * One regression's API: its detail, its draft, the inbox, and what each
 * action answers. Like the service, the detail read after an action shows
 * the regression the action left (for a merge, the merged one).
 */
function regressionAPI(detail: RegressionDetailData, answers: Record<string, unknown> = {}): Handler {
  const id = detail.regression.id;
  let current = detail;
  return (url, init) => {
    const method = init?.method ?? "GET";
    if (method !== "GET") {
      const answer = answers[`${method} ${url}`] ?? { regression: current.regression };
      if (!(answer instanceof Response)) {
        const a = answer as { regression: Regression; merged?: Regression };
        current = { ...current, regression: a.merged ?? a.regression };
      }
      return answer;
    }
    if (url === `/api/v1/regressions/${id}`) return current;
    if (url === `/api/v1/regressions/${id}/draft`) return answers.draft ?? liveDraft;
    if (url.startsWith("/api/v1/regressions/candidates?")) return liveRegressionInbox;
    return catalog(url);
  };
}

describe("RegressionList", () => {
  it("lists the inbox with the columns of spec 41.3", async () => {
    stub((url) => (url.startsWith("/api/v1/regressions/candidates?") ? liveRegressionInbox : catalog(url)));
    renderAs(<RegressionList />, ["read"]);
    const rows = await screen.findAllByTestId("regression-row");
    expect(rows).toHaveLength(3);
    expect(screen.getAllByRole("columnheader").map((h) => h.textContent)).toEqual([
      "Failure",
      "Status",
      "Severity",
      "Label",
      "Failures",
      "Versions",
      "Component",
      "Representative trace",
      "First seen",
      "Last seen",
    ]);
    const row = rows.find((r) => r.dataset.regressionId === DUP.id)!;
    const cells = within(row)
      .getAllByRole("cell")
      .map((c) => c.textContent);
    expect(cells.slice(0, 8)).toEqual([
      `refund_payment took effect twicesupport-refund-agent · cluster ${DUP.fingerprint.slice(0, 8)}`,
      "Candidate",
      "critical",
      "Duplicate side effect",
      "1",
      "v1.3.0",
      "refund_payment",
      DUP.representative_trace_id.slice(0, 12),
    ]);
    expect(
      within(row).getByRole("link", { name: "Regression: refund_payment took effect twice" }),
    ).toHaveAttribute("href", `/regressions/${DUP.id}`);
    expect(within(row).getByRole("link", { name: /Representative trace/ })).toHaveAttribute(
      "href",
      `/traces/${DUP.representative_trace_id}`,
    );
    // The inbox opens on what still needs a decision.
    expect(fetched).toContain(`/api/v1/regressions/candidates?project_id=${PROJECT}&${OPEN}&limit=25`);
  });

  it("shows a person's label and severity next to the miner's suggestion", async () => {
    stub((url) => (url.startsWith("/api/v1/regressions/candidates?") ? liveRegressionInbox : catalog(url)));
    renderAs(<RegressionList />, ["read"]);
    const timeout = (await screen.findAllByTestId("regression-row")).find((r) =>
      r.textContent?.includes("refund_payment timed out"),
    )!;
    expect(within(timeout).getAllByRole("cell")[2]).toHaveTextContent("low");
  });

  it("filters through the contract's query", async () => {
    search = new URLSearchParams({
      project_id: PROJECT,
      view: "all",
      severity: "critical",
      taxonomy: "DUPLICATE_SIDE_EFFECT",
      agent: DUP.agent,
      merged: "1",
    });
    stub((url) =>
      url.startsWith("/api/v1/regressions/candidates?") ? { items: [], next_cursor: null } : catalog(url),
    );
    renderAs(<RegressionList />, ["read"]);
    expect(await screen.findByText("No regressions match")).toBeInTheDocument();
    expect(fetched).toContain(
      `/api/v1/regressions/candidates?project_id=${PROJECT}&severity=critical&taxonomy=DUPLICATE_SIDE_EFFECT&agent=${DUP.agent}&include_merged=true&limit=25`,
    );
    await userEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(replace).toHaveBeenCalledWith(`/regressions?project_id=${PROJECT}`, { scroll: false });
  });

  it("ignores a label the contract does not know", async () => {
    search = new URLSearchParams({ taxonomy: "NOT_A_LABEL" });
    stub((url) =>
      url.startsWith("/api/v1/regressions/candidates?") ? { items: [], next_cursor: null } : catalog(url),
    );
    renderAs(<RegressionList />, ["read"]);
    await screen.findByText("No regressions match");
    expect(fetched).toContain(`/api/v1/regressions/candidates?project_id=${PROJECT}&${OPEN}&limit=25`);
  });

  it("says why the inbox is empty, and when it could not be read", async () => {
    stub((url) =>
      url.startsWith("/api/v1/regressions/candidates?") ? { items: [], next_cursor: null } : catalog(url),
    );
    const { unmount } = renderAs(<RegressionList />, ["read"]);
    expect(await screen.findByText("No regressions need a decision")).toBeInTheDocument();
    unmount();
    stub((url) =>
      url.startsWith("/api/v1/regressions/candidates?")
        ? error(503, "SERVICE_UNAVAILABLE", "The evaluation service is not reachable.")
        : catalog(url),
    );
    renderAs(<RegressionList />, ["read"]);
    expect(await screen.findByRole("alert")).toHaveTextContent("The evaluation service is not reachable.");
  });
});

describe("RegressionDetail", () => {
  it("heads with the failure, its status and severity; a viewer only reads", async () => {
    stub(regressionAPI(liveCandidate));
    renderAs(<RegressionDetail regressionId={DUP.id} />, ["read"]);
    const header = await screen.findByTestId("regression-header");
    expect(header).toHaveTextContent("refund_payment took effect twice");
    expect(header).toHaveTextContent("Candidate");
    expect(header).toHaveTextContent("critical");
    expect(screen.getByRole("list", { name: "Evidence" })).toHaveTextContent(
      "Duplicate side effectrefund_payment took effect more than once",
    );
    expect(screen.getByTestId("occurrence-count")).toHaveTextContent("1");
    const [occurrence] = screen.getAllByTestId("occurrence-row");
    expect(occurrence).toHaveTextContent("representative");
    expect(occurrence).toHaveTextContent("it took an irreversible action twice");
    expect(within(occurrence!).getByRole("link", { name: /^Trace / })).toHaveAttribute(
      "href",
      `/traces/${DUP.representative_trace_id}`,
    );
    for (const name of [/Promote/, /Confirm/, /Triage/, /Merge/, /Dismiss/, /Reopen/]) {
      expect(screen.queryByRole("button", { name })).not.toBeInTheDocument();
    }
  });

  it("offers a reviewer every action the status allows, and confirms", async () => {
    stub(regressionAPI(liveCandidate));
    renderAs(<RegressionDetail regressionId={DUP.id} />, REVIEWER, "reviewer");
    await screen.findByTestId("regression-header");
    for (const name of [/Promote to test/, /Confirm/, /Triage/, /Merge/, /Dismiss/]) {
      expect(screen.getByRole("button", { name })).toBeInTheDocument();
    }
    expect(screen.queryByRole("button", { name: /Reopen/ })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Confirm/ }));
    await vi.waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0]).toEqual({
      url: `/api/v1/regressions/${DUP.id}/confirm`,
      method: "POST",
      body: {},
      key: expect.stringMatching(/^regression-confirm-/),
    });
    // The header's promote opens the test tab.
    await userEvent.click(screen.getByRole("button", { name: /Promote to test/ }));
    expect(replace).toHaveBeenCalledWith("/regressions?tab=test", { scroll: false });
  });

  it("dismisses only with a reason", async () => {
    stub(regressionAPI(liveCandidate));
    renderAs(<RegressionDetail regressionId={DUP.id} />, ENGINEER);
    await userEvent.click(await screen.findByRole("button", { name: /Dismiss/ }));
    const form = screen.getByRole("form", { name: "Dismiss the regression" });
    const submit = within(form).getByRole("button", { name: "Dismiss" });
    expect(submit).toBeDisabled();
    await userEvent.type(within(form).getByLabelText("Reason"), "   ");
    expect(submit).toBeDisabled();
    await userEvent.type(within(form).getByLabelText("Reason"), "a test tenant");
    await userEvent.click(submit);
    await vi.waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0]).toMatchObject({
      url: `/api/v1/regressions/${DUP.id}/dismiss`,
      body: { reason: "a test tenant" },
      key: expect.stringMatching(/^regression-dismiss-/),
    });
  });

  it("triages with only what changed, and refuses what the service would", async () => {
    stub(regressionAPI(liveCandidate));
    renderAs(<RegressionDetail regressionId={DUP.id} />, ENGINEER);
    await userEvent.click(await screen.findByRole("button", { name: /Triage/ }));
    const form = screen.getByRole("form", { name: "Triage" });
    const save = within(form).getByRole("button", { name: /Save/ });
    expect(save).toBeDisabled(); // nothing changed yet
    expect(within(form).getByLabelText("Label")).toHaveValue("DUPLICATE_SIDE_EFFECT");
    await userEvent.selectOptions(within(form).getByLabelText("Severity"), "high");
    await userEvent.type(within(form).getByLabelText("Tags"), "payments, Bad Tag");
    expect(within(form).getByRole("alert")).toHaveTextContent("Not a tag: Bad, Tag");
    expect(save).toBeDisabled();
    await userEvent.clear(within(form).getByLabelText("Tags"));
    await userEvent.type(within(form).getByLabelText("Tags"), "payments refunds payments");
    await userEvent.type(within(form).getByLabelText("Assignee"), "alex");
    expect(within(form).getByRole("alert")).toHaveTextContent("user:alex");
    await userEvent.type(within(form).getByLabelText("Assignee"), "{Home}user:");
    await userEvent.type(within(form).getByLabelText("Reason (optional)"), "retries are the root cause");
    await userEvent.click(save);
    await vi.waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0]).toEqual({
      url: `/api/v1/regressions/${DUP.id}`,
      method: "PATCH",
      body: {
        severity: "high",
        tags: ["payments", "refunds"],
        assignee: "user:alex",
        reason: "retries are the root cause",
      },
      key: expect.stringMatching(/^regression-triage-/),
    });
  });

  it("merges into another regression of the agent and opens it", async () => {
    const target = liveRegressionInbox.items.find((r) => r.title === "refund_payment timed out")!;
    stub(
      regressionAPI(liveCandidate, {
        [`POST /api/v1/regressions/${DUP.id}/merge`]: {
          regression: target,
          merged: { ...DUP, merged_into: target.id, occurrence_count: 0 },
        },
      }),
    );
    renderAs(<RegressionDetail regressionId={DUP.id} />, ENGINEER);
    await userEvent.click(await screen.findByRole("button", { name: /Merge/ }));
    const form = screen.getByRole("form", { name: "Merge the regression" });
    await within(form).findByRole("option", { name: /refund_payment timed out/ });
    // Not itself.
    expect(within(form).queryByRole("option", { name: /took effect twice/ })).not.toBeInTheDocument();
    expect(fetched).toContain(
      `/api/v1/regressions/candidates?project_id=${PROJECT}&agent=${DUP.agent}&limit=200`,
    );
    await userEvent.selectOptions(within(form).getByLabelText("Merge into"), target.id);
    await userEvent.click(within(form).getByRole("button", { name: /^Merge$/ }));
    await vi.waitFor(() => expect(push).toHaveBeenCalledWith(`/regressions/${target.id}`));
    expect(sent[0]).toMatchObject({
      url: `/api/v1/regressions/${DUP.id}/merge`,
      body: { into: target.id },
      key: expect.stringMatching(/^regression-merge-/),
    });
  });

  it("promotes the complete draft as it is and shows the test it became", async () => {
    search = new URLSearchParams({ tab: "test" });
    stub(regressionAPI(liveCandidate, { [`POST /api/v1/regressions/${DUP.id}/promote`]: livePromoted }));
    renderAs(<RegressionDetail regressionId={DUP.id} />, REVIEWER, "reviewer");
    expect(await screen.findByTestId("draft-state")).toHaveTextContent("Complete");
    expect(screen.getByRole("list", { name: "Draft notes" })).toHaveTextContent(
      "ORD-3036 is not in the twin",
    );
    expect(screen.getByTestId("mapping-row")).toHaveTextContent("orders/ORD-3036ORD-1001");
    const form = screen.getByRole("form", { name: "Promote to a regression test" });
    expect(within(form).getByLabelText("Scenario (YAML)")).toHaveValue(liveDraft.draft.yaml);
    await userEvent.click(within(form).getByRole("button", { name: /Promote to test/ }));
    const test = await screen.findByTestId("regression-test");
    expect(sent[0]).toEqual({
      url: `/api/v1/regressions/${DUP.id}/promote`,
      method: "POST",
      body: {},
      key: expect.stringMatching(/^regression-promote-/),
    });
    // What the promotion answered stays shown once the page reads the promoted regression.
    await vi.waitFor(() =>
      expect(fetched.filter((u) => u === `/api/v1/regressions/${DUP.id}`)).toHaveLength(2),
    );
    expect(screen.getByTestId("regression-header")).toHaveTextContent("Has a test");
    expect(test).toHaveTextContent("Promoted to a regression test");
    expect(test).toHaveTextContent(`${livePromoted.scenario.name} (version 1)`);
    expect(within(test).getByRole("link", { name: livePromoted.scenario.name })).toHaveAttribute(
      "href",
      `/scenarios/${livePromoted.scenario.id}`,
    );
    expect(within(test).getByRole("link", { name: "production-regressions" })).toHaveAttribute(
      "href",
      `/datasets/${livePromoted.dataset.id}`,
    );
    expect(test).toHaveTextContent("Every release of support-refund-agent runs it as a known regression");
  });

  it("promotes a person's edit, and not a YAML that does not parse", async () => {
    search = new URLSearchParams({ tab: "test" });
    stub(regressionAPI(liveCandidate, { [`POST /api/v1/regressions/${DUP.id}/promote`]: livePromoted }));
    renderAs(<RegressionDetail regressionId={DUP.id} />, REVIEWER, "reviewer");
    const form = await screen.findByRole("form", { name: "Promote to a regression test" });
    const yaml = within(form).getByLabelText("Scenario (YAML)");
    const button = within(form).getByRole("button", { name: /Promote to test/ });
    await userEvent.type(yaml, "\n  bad: [[");
    expect(button).toBeDisabled();
    expect(form).toHaveTextContent("YAML error");
    const edited = liveDraft.draft.yaml.replace("equals: 40.0", "equals: 40");
    await userEvent.clear(yaml);
    await userEvent.click(yaml);
    await userEvent.paste(edited);
    expect(button).toBeEnabled();
    await userEvent.click(button);
    await vi.waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0]!.body).toEqual({ yaml: edited });
  });

  it("does not promote a draft that needs a person until they write it, and shows what the service asks", async () => {
    search = new URLSearchParams({ tab: "test" });
    const problems = liveIncompleteDraft.draft.problems;
    stub(
      regressionAPI(liveCandidate, {
        draft: liveIncompleteDraft,
        [`POST /api/v1/regressions/${DUP.id}/promote`]: error(
          409,
          "REGRESSION_DRAFT_INCOMPLETE",
          "The draft needs a person.",
          { problems },
        ),
      }),
    );
    renderAs(<RegressionDetail regressionId={DUP.id} />, REVIEWER, "reviewer");
    expect(await screen.findByTestId("draft-state")).toHaveTextContent("Needs a person");
    expect(screen.getByRole("list", { name: "What the draft needs" })).toHaveTextContent(
      "write the input message",
    );
    const form = screen.getByRole("form", { name: "Promote to a regression test" });
    const button = within(form).getByRole("button", { name: /Promote to test/ });
    expect(button).toBeDisabled();
    await userEvent.type(within(form).getByLabelText("Scenario (YAML)"), "\n# reviewed");
    await userEvent.click(button);
    expect(await within(form).findByRole("list", { name: "What the service needs" })).toHaveTextContent(
      problems[0]!,
    );
  });

  it("shows an engineer the draft without the promotion", async () => {
    search = new URLSearchParams({ tab: "test" });
    stub(regressionAPI(liveCandidate));
    renderAs(<RegressionDetail regressionId={DUP.id} />, ENGINEER);
    expect(await screen.findByLabelText("Scenario YAML")).toHaveTextContent("kind: Scenario");
    expect(screen.queryByRole("form", { name: "Promote to a regression test" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Promote to test/ })).not.toBeInTheDocument();
  });

  it("shows a fixed regression's test, the run that fixed it and its history", async () => {
    search = new URLSearchParams({ tab: "test" });
    stub(regressionAPI(liveFixed));
    const { unmount } = renderAs(<RegressionDetail regressionId={DUP.id} />, REVIEWER, "reviewer");
    const test = await screen.findByTestId("regression-test");
    const run = liveFixed.regression.fixed_eval_run_id!;
    expect(test).toHaveTextContent("Fixed in v1.3.1");
    expect(within(test).getByRole("link", { name: /evaluation run/ })).toHaveAttribute(
      "href",
      `/evaluations/${run}`,
    );
    // It is never drafted again; only reopening is left.
    expect(fetched).not.toContain(`/api/v1/regressions/${DUP.id}/draft`);
    expect(screen.getByRole("button", { name: /Reopen/ })).toBeInTheDocument();
    for (const name of [/Promote/, /Confirm/, /Merge/, /Dismiss/]) {
      expect(screen.queryByRole("button", { name })).not.toBeInTheDocument();
    }
    unmount();
    search = new URLSearchParams({ tab: "history" });
    renderAs(<RegressionDetail regressionId={DUP.id} />, ["read"]);
    const events = await screen.findAllByTestId("regression-event");
    expect(events.map((e) => e.firstChild?.textContent)).toEqual([
      "Found by the regression minerCandidate",
      "Promoted to the regression test regression-refund-payment-took-effect-twice-edc1a0 in production-regressionsHas a test",
      "Fixed in 1.3.1Fixed",
    ]);
    expect(events[2]).toHaveTextContent(liveFixed.events[2]!.reason!);
  });

  it("points a merged regression to the one it joined and offers nothing", async () => {
    const into = liveRegressionInbox.items[2]!.id;
    const merged = { ...liveCandidate, regression: { ...DUP, merged_into: into }, occurrences: [] };
    stub(regressionAPI(merged));
    renderAs(<RegressionDetail regressionId={DUP.id} />, REVIEWER, "reviewer");
    expect(await screen.findByRole("note")).toHaveTextContent("Merged into regression");
    expect(within(screen.getByRole("note")).getByRole("link")).toHaveAttribute(
      "href",
      `/regressions/${into}`,
    );
    expect(
      screen.queryByRole("button", { name: /Promote|Confirm|Triage|Merge|Dismiss/ }),
    ).not.toBeInTheDocument();
    expect(screen.getByText("No failures left in this group")).toBeInTheDocument();
  });
});
