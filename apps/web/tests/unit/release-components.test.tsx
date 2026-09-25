import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ReleaseDetail } from "@/components/releases/release-detail";
import { ReleaseList } from "@/components/releases/release-list";
import { MeProvider } from "@/components/shell/me-context";
import type { ReleaseDetail as ReleaseDetailData, ReleaseGate } from "@/lib/api/control-plane";
import type { Me } from "@/lib/types";
import {
  liveBlockedDetail,
  liveBlockedGate,
  liveOverriddenDetail,
  liveOverriddenGate,
  livePassedGate,
  liveReleaseAudit,
  liveReleasePage,
} from "./release-fixtures";
import { livePromptChange } from "./change-fixtures";

const push = vi.fn();
const replace = vi.fn();
let search = new URLSearchParams();
vi.mock("next/navigation", () => ({
  usePathname: () => "/releases",
  useRouter: () => ({ push, replace, refresh: vi.fn() }),
  useSearchParams: () => search,
}));

const RELEASE = liveBlockedDetail.release;
const PROJECT = RELEASE.project_id;
const AGENT = RELEASE.agent.id;

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
      return new Response(JSON.stringify(body ?? { items: [] }), { status: method === "POST" ? 201 : 200 });
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

const projects = { items: [{ id: PROJECT, slug: "support", name: "Support" }] };
const agents = {
  items: [{ id: AGENT, project_id: PROJECT, name: "support-refund-agent", version_count: 2 }],
};
const version = (v: string, n: string) => ({
  id: `01a0d7b0-0000-7000-8000-00000000000${n}`,
  agent_id: AGENT,
  agent_name: "support-refund-agent",
  project_id: PROJECT,
  version: v,
  manifest: { tools: [] },
  manifest_sha256: "a".repeat(64),
  created_by: "system:seed",
  created_at: "2026-09-25T07:29:40Z",
});

function catalog(url: string): unknown {
  if (url === "/api/v1/projects") return projects;
  if (url === `/api/v1/projects/${PROJECT}/agents`) return agents;
  if (url === `/api/v1/agents/${AGENT}/versions`)
    return { items: [version("1.2.4", "1"), version("1.3.0", "2")] };
  return undefined;
}

/** The control plane of one release: its detail, its gate (per revision) and its audit. */
function releaseAPI(detail: ReleaseDetailData, gates: Record<number, ReleaseGate>) {
  const id = detail.release.id;
  return (url: string): unknown => {
    if (url === `/api/v1/releases/${id}`) return detail;
    const m = /^\/api\/v1\/releases\/[^/]+\/gate(?:\?revision=(\d+))?$/.exec(url);
    if (m) return gates[Number(m[1] ?? 1)] ?? new Response("{}", { status: 404 });
    if (url.startsWith("/api/v1/audit?")) return liveReleaseAudit;
    if (url.startsWith("/api/v1/eval-runs/")) return new Response("{}", { status: 404 });
    return catalog(url);
  };
}

describe("ReleaseList", () => {
  it("lists releases with the columns of spec 41.2", async () => {
    stub((url) => (url.startsWith("/api/v1/releases?") ? liveReleasePage : catalog(url)));
    renderAs(<ReleaseList />, ["read"]);
    const rows = await screen.findAllByTestId("release-row");
    expect(rows).toHaveLength(1);
    const row = rows[0]!;
    const cells = within(row)
      .getAllByRole("cell")
      .map((c) => c.textContent);
    expect(cells).toEqual([
      "support-refund-agentv1.3.0Refund flow rewrite",
      "v1.2.4",
      "1 code change, 1 prompt change",
      "BLOCK",
      "2",
      "9",
      "+$0.04 (worse)",
      "−16 ms (better)",
      expect.stringMatching(/^User /),
      expect.stringMatching(/ago|just now/),
    ]);
    expect(within(row).getByRole("link", { name: /1\.2\.4 to 1\.3\.0/ })).toHaveAttribute(
      "href",
      `/releases/${RELEASE.id}`,
    );
    expect(screen.getAllByRole("columnheader").map((h) => h.textContent)).toEqual([
      "Candidate",
      "Baseline",
      "Changed components",
      "Gate",
      "Critical failures",
      "Impacted scenarios",
      "Cost delta",
      "Latency delta (p95)",
      "Created by",
      "Date",
    ]);
    expect(fetched).toContain(`/api/v1/releases?project_id=${PROJECT}&limit=25`);
    expect(screen.queryByRole("button", { name: /New release/ })).not.toBeInTheDocument();
  });

  it("counts the scenarios the change required, not only those evaluated", async () => {
    const r = liveReleasePage.items[0]!;
    const partly = {
      items: [{ ...r, gate: { ...r.gate, summary: { ...r.gate.summary, evaluated: 7 } } }],
      next_cursor: null,
    };
    stub((url) => (url.startsWith("/api/v1/releases?") ? partly : catalog(url)));
    renderAs(<ReleaseList />, ["read"]);
    const row = (await screen.findAllByTestId("release-row"))[0]!;
    expect(within(row).getAllByRole("cell")[5]).toHaveTextContent(/^9$/);
  });

  it("shows an overridden release as overridden, with the outcome it did not change", async () => {
    const overridden = {
      items: [liveOverriddenDetail.release],
      next_cursor: null,
    };
    stub((url) => (url.startsWith("/api/v1/releases?") ? overridden : catalog(url)));
    renderAs(<ReleaseList />, ["read"]);
    const row = (await screen.findAllByTestId("release-row"))[0]!;
    expect(within(row).getByTestId("gate-outcome")).toHaveTextContent("Overridden");
    expect(row).toHaveTextContent("originally BLOCK");
    expect(row).not.toHaveTextContent("PASS");
  });

  it("filters by agent through the contract's query", async () => {
    search = new URLSearchParams({ project_id: PROJECT, agent: "support-refund-agent" });
    stub((url) => (url.startsWith("/api/v1/releases?") ? { items: [], next_cursor: null } : catalog(url)));
    renderAs(<ReleaseList />, ["read"]);
    expect(await screen.findByText("No releases of this agent")).toBeInTheDocument();
    expect(fetched).toContain(`/api/v1/releases?project_id=${PROJECT}&agent=support-refund-agent&limit=25`);
  });

  it("creates and evaluates a release (release.write), then opens it", async () => {
    stub((url, init) => {
      if (init?.method === "POST") return { release: RELEASE, gate: null };
      return url.startsWith("/api/v1/releases?") ? { items: [], next_cursor: null } : catalog(url);
    });
    renderAs(<ReleaseList />, ["read", "release.write"]);
    await userEvent.click(await screen.findByRole("button", { name: /New release/ }));
    const form = await screen.findByRole("form", { name: "New release" });
    await screen.findAllByRole("option", { name: "1.3.0" });
    expect(within(form).getByLabelText("Candidate")).toHaveValue("1.3.0");
    expect(within(form).getByLabelText("Baseline (in use)")).toHaveValue("1.2.4");
    await userEvent.type(within(form).getByLabelText("Title (optional)"), "Refund flow rewrite");
    await userEvent.click(within(form).getByRole("button", { name: /Create and evaluate/ }));
    await vi.waitFor(() => expect(push).toHaveBeenCalledWith(`/releases/${RELEASE.id}`));
    expect(sent).toEqual([
      {
        url: "/api/v1/releases",
        method: "POST",
        body: {
          project_id: PROJECT,
          agent: "support-refund-agent",
          baseline_version: "1.2.4",
          candidate_version: "1.3.0",
          title: "Refund flow rewrite",
        },
        key: expect.stringMatching(/^release-/),
      },
    ]);
  });
});

describe("ReleaseDetail", () => {
  it("heads with the versions and the outcome, and says why it is blocked", async () => {
    stub(releaseAPI(liveBlockedDetail, { 1: liveBlockedGate }));
    renderAs(<ReleaseDetail releaseId={RELEASE.id} />, ["read"]);
    const header = await screen.findByTestId("release-header");
    expect(header).toHaveTextContent("support-refund-agent");
    expect(header).toHaveTextContent("v1.2.4");
    expect(header).toHaveTextContent("v1.3.0");
    expect(within(header).getByTestId("gate-outcome")).toHaveTextContent("BLOCK");
    const why = await screen.findByTestId("why");
    expect(within(why).getByRole("heading", { name: "Why it is blocked" })).toBeInTheDocument();
    expect(why).toHaveTextContent(liveBlockedGate.decision.summary);
    expect(within(why).getByRole("list", { name: "What the evaluation found" })).toHaveTextContent(
      "2 new critical failures",
    );
    const rules = within(why).getAllByTestId("why-rule");
    expect(rules.map((r) => within(r).getByText(/^(BLOCK|WARN)$/).textContent)).toEqual(
      liveBlockedGate.decision.rules.map((r) => r.outcome),
    );
    expect(rules[0]).toHaveTextContent("An action took effect twice");
    expect(rules[0]).toHaveTextContent("refund-timeout-after-mutation");
    // What CI does with it, and the summary numbers.
    expect(screen.getByText("exit code 3 (fails the job)")).toBeInTheDocument();
    expect(screen.getAllByTestId("coverage-row")[0]).toHaveTextContent(
      "Required scenarios passed6/9 67%refund-happy-path, refund-timeout-after-mutation, refund-tool-success-lie",
    );
    expect(screen.getByRole("heading", { name: "Risk index 90/100" })).toBeInTheDocument();
    // Nobody here may override or evaluate.
    expect(screen.queryByRole("button", { name: /Override/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Evaluate/ })).not.toBeInTheDocument();
  });

  it("shows every rule's exact evidence with links to the eval case and trace", async () => {
    search = new URLSearchParams({ tab: "evals" });
    stub(releaseAPI(liveBlockedDetail, { 1: liveBlockedGate }));
    renderAs(<ReleaseDetail releaseId={RELEASE.id} />, ["read"]);
    const cards = await screen.findAllByTestId("rule-evidence");
    expect(cards).toHaveLength(liveBlockedGate.decision.rules.length);
    const first = cards[0]!;
    expect(first).toHaveTextContent("Rule: An irreversible action must not take effect more than once.");
    expect(first).toHaveTextContent("The side effect refund:ORD-1001 was applied 2 times.");
    expect(first).toHaveTextContent("#3 refund_payment → HTTP 200. Applied side effect refund:ORD-1001.");
    expect(first).toHaveTextContent("At step 2 the baseline called get_refund_policy");
    const run = liveBlockedGate.eval_run_id;
    expect(within(first).getByRole("link", { name: "refund-timeout-after-mutation" })).toHaveAttribute(
      "href",
      `/evaluations/${run}/cases/refund-timeout-after-mutation`,
    );
    expect(within(first).getByRole("link", { name: /Candidate trace/ })).toHaveAttribute(
      "href",
      `/traces/${(liveBlockedGate as ReleaseGate).decision!.rules[0]!.evidence[0]!.trace_id}?project_id=${PROJECT}`,
    );
  });

  it("shows the pinned suite and why each scenario runs", async () => {
    search = new URLSearchParams({ tab: "simulations" });
    stub(releaseAPI(liveBlockedDetail, { 1: liveBlockedGate }));
    renderAs(<ReleaseDetail releaseId={RELEASE.id} />, ["read"]);
    const rows = await screen.findAllByTestId("suite-row");
    expect(rows).toHaveLength(liveBlockedGate.suite.length);
    const first = liveBlockedGate.suite[0]!;
    expect(rows[0]).toHaveTextContent(first.scenario_name);
    expect(rows[0]).toHaveTextContent(first.why[0]!);
    expect(rows[0]).toHaveTextContent("mandatory");
  });

  it("shows the evidence hash, verified, and says when it is not", async () => {
    search = new URLSearchParams({ tab: "evidence" });
    stub(releaseAPI(liveBlockedDetail, { 1: liveBlockedGate }));
    const { unmount } = renderAs(<ReleaseDetail releaseId={RELEASE.id} />, ["read"]);
    expect(await screen.findByTestId("evidence-hash")).toHaveTextContent(liveBlockedGate.evidence_sha256);
    expect(screen.getByTestId("evidence-verified")).toHaveTextContent("Verified");
    expect(screen.getByRole("button", { name: /Download JSON/ })).toBeInTheDocument();
    unmount();
    stub(releaseAPI(liveBlockedDetail, { 1: { ...liveBlockedGate, evidence_verified: false } }));
    renderAs(<ReleaseDetail releaseId={RELEASE.id} />, ["read"]);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "NOT VERIFIED: the stored decision no longer matches its hash.",
    );
  });

  it("shows what changed, and what the change reached when the revision was requested", async () => {
    search = new URLSearchParams({ tab: "changes" });
    const api = releaseAPI(liveBlockedDetail, { 1: liveBlockedGate });
    stub((url) => (url === `/api/v1/change-sets/${RELEASE.change_set_id}` ? livePromptChange : api(url)));
    const { unmount } = renderAs(<ReleaseDetail releaseId={RELEASE.id} />, ["read"]);
    expect(await screen.findByRole("link", { name: "Open the change set" })).toHaveAttribute(
      "href",
      `/changes/${RELEASE.change_set_id}`,
    );
    expect(await screen.findAllByTestId("change-item")).toHaveLength(livePromptChange.items.length);
    expect(screen.getByTestId("prompt-diff")).toBeInTheDocument();
    expect(fetched).toContain(`/api/v1/change-sets/${RELEASE.change_set_id}`);
    unmount();
    search = new URLSearchParams({ tab: "blast-radius" });
    fetched = [];
    renderAs(<ReleaseDetail releaseId={RELEASE.id} />, ["read"]);
    expect(await screen.findByText(/As computed when revision 1 was requested/)).toBeInTheDocument();
    // The impact comes from the gate: nothing is recomputed.
    expect(fetched.some((u) => u.includes("/impact"))).toBe(false);
  });

  it("lists the release's audit entries for administrators only", async () => {
    search = new URLSearchParams({ tab: "audit" });
    stub(releaseAPI(liveOverriddenDetail, { 1: liveOverriddenGate }));
    const { unmount } = renderAs(<ReleaseDetail releaseId={liveOverriddenDetail.release.id} />, ["read"]);
    expect(await screen.findByText("The audit log is for administrators")).toBeInTheDocument();
    expect(fetched.some((u) => u.startsWith("/api/v1/audit"))).toBe(false);
    unmount();
    renderAs(<ReleaseDetail releaseId={liveOverriddenDetail.release.id} />, ["read", "settings.read"]);
    const entries = await screen.findAllByTestId("audit-entry");
    expect(entries.map((e) => e.querySelector("span")?.textContent)).toEqual([
      "Gate overridden",
      "Gate decided",
      "Evaluation requested",
      "Release created",
    ]);
    expect(entries[0]).toHaveTextContent("Hotfix for the outage, reviewed by the refunds team.");
    expect(fetched).toContain(
      `/api/v1/audit?resource_type=release&resource_id=${liveOverriddenDetail.release.id}&limit=100`,
    );
  });

  it("overrides a blocked gate with a reason, and still shows it was blocked", async () => {
    let gate: ReleaseGate = liveBlockedGate;
    stub((url, init) => {
      if (init?.method === "POST" && url.endsWith("/override")) {
        gate = { ...liveOverriddenGate, release_id: RELEASE.id };
        return gate;
      }
      return releaseAPI(liveBlockedDetail, { 1: gate })(url);
    });
    renderAs(<ReleaseDetail releaseId={RELEASE.id} />, ["read", "release.override"]);
    await userEvent.click(await screen.findByRole("button", { name: /^Override$/ }));
    const form = screen.getByRole("form", { name: "Override the gate" });
    // A reason too short is refused before anything is sent.
    await userEvent.type(within(form).getByLabelText("Reason"), "because");
    await userEvent.click(within(form).getByRole("button", { name: /Override BLOCK/ }));
    expect(within(form).getByText(/at least 10 characters/)).toBeInTheDocument();
    expect(sent).toEqual([]);
    await userEvent.type(within(form).getByLabelText("Reason"), " the outage needs this fix now");
    await userEvent.type(
      within(form).getByLabelText("Ticket URL (optional)"),
      "https://tickets.example.com/OPS-12",
    );
    await userEvent.click(within(form).getByRole("button", { name: /Override BLOCK/ }));
    await vi.waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0]).toEqual({
      url: `/api/v1/releases/${RELEASE.id}/override`,
      method: "POST",
      body: {
        reason: "because the outage needs this fix now",
        ticket_url: "https://tickets.example.com/OPS-12",
        revision: 1,
      },
      key: expect.stringMatching(/^override-/),
    });
    const banner = await screen.findByTestId("override-banner");
    expect(banner).toHaveTextContent("Originally BLOCKED · overridden");
    expect(banner).toHaveTextContent("Reason: Hotfix for the outage, reviewed by the refunds team.");
    expect(within(banner).getByRole("link", { name: "https://tickets.example.com/OPS-12" })).toHaveAttribute(
      "rel",
      "noopener noreferrer",
    );
    expect(screen.queryByRole("form", { name: "Override the gate" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Override$/ })).not.toBeInTheDocument();
  });

  it("does not offer an override the gate does not allow", async () => {
    // A reviewer when the project's policy keeps reviewers out.
    const strict = {
      ...liveBlockedGate,
      policy: { ...liveBlockedGate.policy, allow_reviewer_override: false },
    };
    stub(releaseAPI(liveBlockedDetail, { 1: strict }));
    const { unmount } = renderAs(
      <ReleaseDetail releaseId={RELEASE.id} />,
      ["read", "release.override"],
      "reviewer",
    );
    expect(await screen.findByText(/does not let reviewers override/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Override$/ })).not.toBeInTheDocument();
    unmount();
    // A gate that passed: nothing to override.
    const passed = {
      ...liveBlockedDetail,
      revisions: [{ ...liveBlockedDetail.revisions[0]!, outcome: "PASS", effective_outcome: "PASS" }],
    } as ReleaseDetailData;
    stub(releaseAPI(passed, { 1: { ...livePassedGate, revision: 1 } }));
    renderAs(<ReleaseDetail releaseId={RELEASE.id} />, ["read", "release.override"]);
    expect(await screen.findByRole("heading", { name: "Why it passes" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Override$/ })).not.toBeInTheDocument();
  });

  it("shows an overridden release with its original outcome and no second override", async () => {
    stub(releaseAPI(liveOverriddenDetail, { 1: liveOverriddenGate }));
    renderAs(<ReleaseDetail releaseId={liveOverriddenDetail.release.id} />, ["read", "release.override"]);
    const header = await screen.findByTestId("release-header");
    expect(header).toHaveTextContent("Overridden");
    expect(header).toHaveTextContent("originally BLOCK");
    const banner = await screen.findByTestId("override-banner");
    expect(banner).toHaveTextContent("Originally BLOCKED");
    expect(screen.getByRole("heading", { name: "Why it is blocked" })).toBeInTheDocument();
    expect(screen.getByText("exit code 0 (the job passes)")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Override$/ })).not.toBeInTheDocument();
  });

  it("evaluates again as a new revision, and keeps earlier revisions readable", async () => {
    const two = {
      ...liveBlockedDetail,
      release: { ...RELEASE, gate: { ...liveBlockedDetail.revisions[0]!, revision: 2 } },
      revisions: [{ ...liveBlockedDetail.revisions[0]!, revision: 2 }, liveBlockedDetail.revisions[0]!],
    } as ReleaseDetailData;
    search = new URLSearchParams({ revision: "1" });
    stub((url, init) => {
      if (init?.method === "POST") {
        return new Response(
          JSON.stringify({ ...liveBlockedGate, revision: 3, status: "EVALUATING", decision: null }),
          {
            status: 202,
          },
        );
      }
      return releaseAPI(two, { 1: liveBlockedGate, 2: { ...liveBlockedGate, revision: 2 } })(url);
    });
    renderAs(<ReleaseDetail releaseId={RELEASE.id} />, ["read", "release.write"]);
    expect(await screen.findByText(/An earlier revision, kept as it was decided/)).toBeInTheDocument();
    expect(fetched).toContain(`/api/v1/releases/${RELEASE.id}/gate?revision=1`);
    expect(screen.getByLabelText("Revision")).toHaveValue("1");
    const history = await screen.findAllByTestId("revision-row");
    expect(history.map((r) => r.getAttribute("aria-current"))).toEqual([null, "true"]);
    await userEvent.click(screen.getByRole("button", { name: /Evaluate again/ }));
    await vi.waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0]).toMatchObject({
      url: `/api/v1/releases/${RELEASE.id}/evaluate`,
      method: "POST",
      key: expect.stringMatching(/^release-evaluate-/),
    });
    // Back to the latest revision.
    expect(replace).toHaveBeenCalledWith("/releases", { scroll: false });
  });

  it("says a release is evaluating while its gate is pending", async () => {
    const pending = {
      ...liveBlockedDetail,
      revisions: [
        {
          ...liveBlockedDetail.revisions[0]!,
          status: "EVALUATING",
          effective_outcome: "PENDING",
          outcome: null,
        },
      ],
    } as ReleaseDetailData;
    stub(
      releaseAPI(pending, { 1: { ...liveBlockedGate, status: "EVALUATING", decision: null, summary: null } }),
    );
    renderAs(<ReleaseDetail releaseId={RELEASE.id} />, ["read", "release.override", "release.write"]);
    expect(await screen.findByRole("status")).toHaveTextContent("Evaluating revision 1…");
    expect(within(screen.getByTestId("release-header")).getByTestId("gate-outcome")).toHaveTextContent(
      "Evaluating",
    );
    expect(screen.queryByRole("button", { name: /^Override$/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Evaluate again/ })).toBeDisabled();
  });
});
