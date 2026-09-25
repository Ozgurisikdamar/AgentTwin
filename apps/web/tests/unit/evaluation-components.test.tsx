import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { EvalCaseView } from "@/components/evaluations/case-comparison";
import { EvalRunDetailView } from "@/components/evaluations/eval-run-detail";
import { EvalRunList } from "@/components/evaluations/eval-run-list";
import { NewEvaluation, defaultPair } from "@/components/evaluations/new-evaluation";
import { MeProvider } from "@/components/shell/me-context";
import type { ControlPlaneSchemas } from "@/lib/api/control-plane";
import type { EvaluationSchemas, ReviewResponse } from "@/lib/api/evaluation";
import type { AgentVersion, Me } from "@/lib/types";
import { liveDataset, liveDatasets, liveEvalRun, liveToolSuccessLie } from "./eval-fixtures";

const push = vi.fn();
vi.mock("next/navigation", () => ({
  usePathname: () => "/evaluations",
  useRouter: () => ({ push, replace: vi.fn(), refresh: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

const RUN = liveEvalRun.run.id;
const PROJECT = liveEvalRun.run.project_id;
const AGENT = "01a0d778-5b90-7000-8000-000000000001";

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
});

function renderAs(
  ui: ReactNode,
  permissions: string[],
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } }),
) {
  const me = {
    user: { id: "u-1", email: "e@demo.agenttwin.dev", display_name: "Eve" },
    permissions,
  } as unknown as Me;
  return render(
    <QueryClientProvider client={qc}>
      <MeProvider me={me}>{ui}</MeProvider>
    </QueryClientProvider>,
  );
}

describe("EvalRunList", () => {
  beforeEach(() =>
    stub((url) =>
      url.startsWith("/api/v1/eval-runs")
        ? ({ items: [liveEvalRun.run], next_cursor: null } satisfies EvaluationSchemas["EvalRunPage"])
        : { items: [] },
    ),
  );

  it("shows the pair, the counts in words and the suite; New evaluation needs eval.run", async () => {
    const { unmount } = renderAs(<EvalRunList />, ["read"]);
    const row = await screen.findByTestId("eval-run-row");
    expect(row).toHaveAttribute("data-status", "COMPLETED");
    expect(within(row).getByText("v1.2.4")).toBeInTheDocument();
    expect(within(row).getByText("v1.3.0")).toBeInTheDocument();
    const badges = within(row).getByTestId("count-badges");
    expect(badges).toHaveTextContent("2 new critical failures");
    expect(badges).toHaveTextContent("1 regressed");
    expect(badges).toHaveTextContent("6 unchanged");
    expect(badges).not.toHaveTextContent("improved"); // zero counts are left out
    expect(row).toHaveTextContent("refund-regression-suite v1");
    expect(screen.queryByRole("link", { name: /New evaluation/ })).not.toBeInTheDocument();
    unmount();
    renderAs(<EvalRunList />, ["read", "eval.run"]);
    expect(await screen.findByRole("link", { name: /New evaluation/ })).toHaveAttribute(
      "href",
      "/evaluations/new",
    );
  });
});

describe("EvalRunDetailView", () => {
  beforeEach(() => stub((url) => (url === `/api/v1/eval-runs/${RUN}` ? liveEvalRun : { items: [] })));

  it("leads with what the candidate breaks, then every case worst first", async () => {
    renderAs(<EvalRunDetailView runId={RUN} />, ["read"]);
    expect(await screen.findByTestId("eval-verdict")).toHaveTextContent(
      "v1.3.0 newly fails a critical expectation in 2 scenarios",
    );
    expect(screen.getByTestId("eval-summary")).toHaveTextContent(
      "2 new critical failures · 1 regressed · 6 unchanged · refund-regression-suite v1",
    );
    const failures = screen.getByTestId("new-critical-failures");
    expect(within(failures).getByRole("link", { name: "refund-tool-success-lie" })).toHaveAttribute(
      "href",
      `/evaluations/${RUN}/cases/refund-tool-success-lie`,
    );
    expect(failures).toHaveTextContent("no-unverified-success");
    const rows = screen.getAllByTestId("compared-case");
    expect(rows.map((r) => r.getAttribute("data-classification")).slice(0, 4)).toEqual([
      "NEW_CRITICAL_FAILURE",
      "NEW_CRITICAL_FAILURE",
      "REGRESSED",
      "UNCHANGED",
    ]);
    const tiles = screen.getAllByTestId("count-tile");
    expect(tiles.map((t) => t.textContent)).toEqual([
      "2New critical failure",
      "1Regressed",
      "0Incomplete",
      "0Improved",
      "6Unchanged",
    ]);
    // The sides' totals: the candidate passes fewer (worse), and unknown cost is not a zero.
    const passed = screen
      .getAllByTestId("side-total")
      .find((r) => r.getAttribute("data-metric") === "passed")!;
    expect(passed).toHaveAttribute("data-change", "worse");
    const cost = screen
      .getAllByTestId("side-total")
      .find((r) => r.getAttribute("data-metric") === "cost_usd")!;
    expect(cost).toHaveTextContent("Cost——");
    // The fake judge is said to be a fake, and not calibrated.
    expect(screen.getByTestId("eval-pinning")).toHaveTextContent("not a language model");
    expect(screen.getByTestId("eval-pinning")).toHaveTextContent("not calibrated");
    // Read-only users get no actions.
    expect(screen.queryByRole("button", { name: /Run again/ })).not.toBeInTheDocument();
  });

  it("drops the dataset's cached results once the run has finished, and only then", async () => {
    const dataset = liveEvalRun.run.selection.dataset!.id;
    const cached = (qc: QueryClient) => {
      qc.setQueryData(["dataset", dataset, "1"], liveDataset);
      qc.setQueryData(["eval-runs", "", "", "", ""], { pages: [], pageParams: [] });
      qc.setQueryData(["review-queue", ""], { pages: [], pageParams: [] });
    };
    const stale = (qc: QueryClient) =>
      [
        ["dataset", dataset, "1"],
        ["eval-runs", "", "", "", ""],
        ["review-queue", ""],
      ].map((key) => qc.getQueryState(key)?.isInvalidated);

    const running = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    cached(running);
    stub((url) =>
      url === `/api/v1/eval-runs/${RUN}`
        ? { ...liveEvalRun, run: { ...liveEvalRun.run, status: "RUNNING", finished_at: null } }
        : { items: [] },
    );
    const first = renderAs(<EvalRunDetailView runId={RUN} />, ["read"], running);
    await screen.findByTestId("eval-run-id");
    expect(stale(running)).toEqual([false, false, false]);
    first.unmount();

    const done = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    cached(done);
    stub((url) => (url === `/api/v1/eval-runs/${RUN}` ? liveEvalRun : { items: [] }));
    renderAs(<EvalRunDetailView runId={RUN} />, ["read"], done);
    await screen.findByTestId("eval-verdict");
    await vi.waitFor(() => expect(stale(done)).toEqual([true, true, true]));
  });

  it("runs the same pair on the same pinned suite and seed again", async () => {
    stub((url, init) =>
      init?.method === "POST"
        ? ({
            run: { ...liveEvalRun.run, id: "01a0d77a-0000-7000-8000-00000000000a", status: "QUEUED" },
          } satisfies EvaluationSchemas["EvalRunResponse"])
        : url === `/api/v1/eval-runs/${RUN}`
          ? liveEvalRun
          : { items: [] },
    );
    renderAs(<EvalRunDetailView runId={RUN} />, ["read", "eval.run"]);
    await userEvent.click(await screen.findByRole("button", { name: /Run again/ }));
    expect(sent).toHaveLength(1);
    expect(sent[0]).toMatchObject({
      url: "/api/v1/eval-runs",
      method: "POST",
      body: {
        project_id: PROJECT,
        agent: "support-refund-agent",
        baseline_version: "1.2.4",
        candidate_version: "1.3.0",
        dataset_id: liveEvalRun.run.selection.dataset!.id,
        dataset_version: 1,
        seed: 42,
      },
    });
    expect(sent[0]!.key).toMatch(/^eval-again-[0-9a-f]{32}$/);
    await vi.waitFor(() =>
      expect(push).toHaveBeenCalledWith("/evaluations/01a0d77a-0000-7000-8000-00000000000a"),
    );
  });
});

describe("EvalCaseView", () => {
  const SCENARIO = "refund-tool-success-lie";
  const CASE = `/api/v1/eval-runs/${RUN}/cases/${SCENARIO}`;

  function stubCase(onReview?: () => unknown) {
    stub((url, init) => {
      if (url === `/api/v1/eval-runs/${RUN}`) return liveEvalRun;
      if (url === CASE) return liveToolSuccessLie;
      if (url === `${CASE}/reviews` && init?.method === "POST") return onReview?.();
      return { items: [] };
    });
  }

  it("explains where the candidate diverged and what each expectation did", async () => {
    stubCase();
    renderAs(<EvalCaseView runId={RUN} scenario={SCENARIO} />, ["read"]);
    expect(await screen.findByTestId("impact")).toHaveTextContent(
      "The candidate called the irreversible refund_payment without first calling get_refund_policy",
    );
    expect(screen.getByTestId("divergence")).toHaveAttribute("data-kind", "tool");
    const changes = screen.getAllByTestId("expectation-change");
    expect(changes.map((c) => [c.getAttribute("data-expectation"), c.getAttribute("data-change")])).toEqual([
      ["no-unverified-success", "broken"],
      ["no-false-confirmation", "broken"],
      ["handed-to-a-human", "broken"],
      ["reply-admits-unconfirmed-refund", "broken"],
    ]);
    expect(within(changes[0]!).getByText("hallucinated success")).toBeInTheDocument();
    // The semantic one shows both judge scores.
    expect(changes[3]).toHaveTextContent("score 1.00");
    expect(changes[3]).toHaveTextContent("score 0.00");
    // Metrics: better and worse are said, and unknown cost stays unknown.
    const metric = (name: string) =>
      screen.getAllByTestId("metric").find((m) => m.getAttribute("data-metric") === name)!;
    expect(metric("critical_failures")).toHaveAttribute("data-change", "worse");
    expect(metric("critical_failures")).toHaveTextContent("+3");
    expect(metric("cost_usd")).toHaveTextContent("Cost———");
    // The candidate sent an email the baseline did not, and dropped the escalation.
    expect(screen.getByTestId("tool-selection")).toHaveTextContent("+ send_email");
    expect(screen.getByTestId("tool-selection")).toHaveTextContent("− escalate_to_human");
    expect(screen.getAllByTestId("aligned-step").map((s) => s.getAttribute("data-match"))).toEqual([
      "same",
      "baseline_only",
      "changed",
      "baseline_only",
      "baseline_only",
      "baseline_only",
      "candidate_only",
      "candidate_only",
    ]);
    expect(screen.getAllByTestId("verdict")).toHaveLength(2);
    // Without review.write there is nothing to review.
    expect(screen.queryByRole("button", { name: /^Review / })).not.toBeInTheDocument();
  });

  it("records a reviewer's verdict with a note and says what it changed", async () => {
    const response = {
      review: {
        id: "01a0d77b-0000-7000-8000-000000000001",
        eval_run_id: RUN,
        scenario_name: SCENARIO,
        side: "CANDIDATE",
        expectation_id: "reply-admits-unconfirmed-refund",
        original_status: "FAIL",
        original_label: "SEMANTIC_FAIL",
        status: "PASS",
        note: "The reply does say the refund is unconfirmed.",
        reviewer: "user:u-1",
        created_at: "2026-09-25T08:00:00Z",
      },
      classification: "NEW_CRITICAL_FAILURE",
      previous_classification: "NEW_CRITICAL_FAILURE",
      run: liveEvalRun.run,
    } satisfies ReviewResponse;
    stubCase(() => response);
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const dataset = liveEvalRun.run.selection.dataset!.id;
    qc.setQueryData(["dataset", dataset, ""], liveDataset);
    qc.setQueryData(["eval-runs", "", "", "", ""], { pages: [], pageParams: [] });
    renderAs(<EvalCaseView runId={RUN} scenario={SCENARIO} />, ["read", "review.write"], qc);
    await userEvent.click(
      await screen.findByRole("button", { name: "Review reply-admits-unconfirmed-refund on the candidate" }),
    );
    const form = screen.getByTestId("review-form");
    // A failed result is offered as a pass by default; the note is required.
    expect(within(form).getByRole("radio", { name: "Pass" })).toBeChecked();
    const save = within(form).getByRole("button", { name: "Save review" });
    expect(save).toBeDisabled();
    await userEvent.type(within(form).getByLabelText(/Why/), "The reply does say the refund is unconfirmed.");
    await userEvent.click(save);
    expect(sent).toHaveLength(1);
    expect(sent[0]).toEqual({
      url: `${CASE}/reviews`,
      method: "POST",
      body: {
        side: "CANDIDATE",
        expectation_id: "reply-admits-unconfirmed-refund",
        status: "PASS",
        note: "The reply does say the refund is unconfirmed.",
      },
      key: expect.stringMatching(/^review-[0-9a-f]{32}$/),
    });
    expect(await screen.findByTestId("review-outcome")).toHaveTextContent(
      "The case stays new critical failure.",
    );
    expect(screen.queryByTestId("review-form")).not.toBeInTheDocument();
    // A review can classify the case again: the dataset and the run list are refetched.
    expect(qc.getQueryState(["dataset", dataset, ""])?.isInvalidated).toBe(true);
    expect(qc.getQueryState(["eval-runs", "", "", "", ""])?.isInvalidated).toBe(true);
  });

  it("shows why a review was refused", async () => {
    stubCase(
      () =>
        new Response(
          JSON.stringify({
            error: {
              code: "EXPECTATION_NOT_REVIEWABLE",
              message: "This is the simulation's finding about the run itself.",
              request_id: "req-1",
            },
          }),
          { status: 409 },
        ),
    );
    renderAs(<EvalCaseView runId={RUN} scenario={SCENARIO} />, ["read", "review.write"]);
    await userEvent.click(
      await screen.findByRole("button", { name: "Review no-unverified-success on the candidate" }),
    );
    const form = screen.getByTestId("review-form");
    await userEvent.type(within(form).getByLabelText(/Why/), "x");
    await userEvent.click(within(form).getByRole("button", { name: "Save review" }));
    expect(await within(form).findByRole("alert")).toHaveTextContent("EXPECTATION_NOT_REVIEWABLE");
    expect(screen.getByTestId("review-form")).toBeInTheDocument(); // still open to fix
  });
});

describe("NewEvaluation", () => {
  const version = (v: string, id: string) =>
    ({
      id,
      agent_id: AGENT,
      agent_name: "support-refund-agent",
      project_id: PROJECT,
      version: v,
      manifest: { tools: [] },
      manifest_sha256: "a".repeat(64),
      model_name: "scripted-planner-v1",
      created_by: "system:seed",
      created_at: "2026-09-25T07:29:40Z",
    }) as unknown as AgentVersion;

  it("pairs the newest version with the one before it", () => {
    const pair = defaultPair([version("1.2.4", "a"), version("1.3.1", "b"), version("1.3.0", "c")]);
    expect(pair).toEqual({ baseline: "1.3.0", candidate: "1.3.1" });
    expect(defaultPair([version("2.0.0", "a")])).toEqual({ baseline: "2.0.0", candidate: "2.0.0" });
    expect(defaultPair([])).toEqual({ baseline: "", candidate: "" });
  });

  it("starts an evaluation of the chosen versions on the latest dataset version", async () => {
    const projects = {
      items: [
        { id: PROJECT, organization_id: liveEvalRun.run.organization_id, slug: "support", name: "Support" },
      ],
    } as unknown as ControlPlaneSchemas["ProjectList"];
    stub((url, init) => {
      if (init?.method === "POST")
        return { run: { ...liveEvalRun.run, id: "01a0d77a-0000-7000-8000-00000000000b" } };
      if (url === "/api/v1/projects") return projects;
      if (url === "/api/v1/simulations/capabilities") return { agents: ["support-refund-agent"] };
      if (url === `/api/v1/projects/${PROJECT}/agents`)
        return {
          items: [{ id: AGENT, project_id: PROJECT, name: "support-refund-agent", version_count: 3 }],
        };
      if (url === `/api/v1/agents/${AGENT}/versions`)
        return { items: [version("1.2.4", "a"), version("1.3.0", "b"), version("1.3.1", "c")] };
      if (url.startsWith("/api/v1/datasets?")) return liveDatasets;
      if (url === `/api/v1/datasets/${liveDataset.dataset.id}`) return liveDataset;
      return { items: [] };
    });
    renderAs(<NewEvaluation />, ["read", "eval.run"]);
    const submit = await screen.findByRole("button", { name: "Compare on 9 scenarios" });
    // The versions load after the agent.
    await within(screen.getByLabelText("Baseline version")).findByRole("option", { name: /^1\.2\.4/ });
    await userEvent.selectOptions(screen.getByLabelText("Baseline version"), "1.2.4");
    await userEvent.selectOptions(screen.getByLabelText("Candidate version"), "1.3.0");
    await userEvent.type(screen.getByLabelText("Seed (optional)"), "42");
    await userEvent.click(submit);
    expect(sent).toHaveLength(1);
    // The latest dataset version: left to the service, which pins it.
    expect(sent[0]!.body).toEqual({
      project_id: PROJECT,
      agent: "support-refund-agent",
      baseline_version: "1.2.4",
      candidate_version: "1.3.0",
      dataset_id: liveDataset.dataset.id,
      seed: 42,
    });
    await vi.waitFor(() =>
      expect(push).toHaveBeenCalledWith("/evaluations/01a0d77a-0000-7000-8000-00000000000b"),
    );
  });

  it("is not offered to a role that cannot run evaluations", () => {
    stub(() => ({ items: [] }));
    renderAs(<NewEvaluation />, ["read"]);
    expect(screen.getByText("Your role cannot start evaluations")).toBeInTheDocument();
  });
});
