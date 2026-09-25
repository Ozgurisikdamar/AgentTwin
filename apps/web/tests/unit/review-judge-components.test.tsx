import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { JudgesView } from "@/components/judges/judges-view";
import { ReviewQueue, pendingWhy } from "@/components/reviews/review-queue";
import { MeProvider } from "@/components/shell/me-context";
import type { Calibration, EvaluationSchemas, ReviewQueuePage } from "@/lib/api/evaluation";
import type { Me } from "@/lib/types";
import { liveEvalRun, liveJudge } from "./eval-fixtures";

vi.mock("next/navigation", () => ({
  usePathname: () => "/reviews",
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), refresh: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

const RUN = liveEvalRun.run.id;
const PROJECT = liveEvalRun.run.project_id;
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
      return new Response(JSON.stringify(handler(url, method) ?? { items: [] }), {
        status: method === "POST" ? 202 : 200,
      });
    }),
  );
}

afterEach(() => vi.unstubAllGlobals());

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

const projects = { items: [{ id: PROJECT, slug: "support", name: "Support" }] };

describe("ReviewQueue", () => {
  const queue = {
    items: [
      {
        eval_run_id: RUN,
        position: 7,
        scenario_name: "refund-tool-success-lie",
        severity: "critical",
        classification: "INCOMPLETE",
        project_id: PROJECT,
        agent_name: "support-refund-agent",
        baseline_version: "1.2.4",
        candidate_version: "1.3.0",
        finished_at: "2026-09-25T07:31:54.486044Z",
        pending: [
          {
            side: "CANDIDATE",
            expectation_id: "reply-admits-unconfirmed-refund",
            status: "ERROR",
            reason: "The judge's answer was not valid JSON.",
            critical: true,
          },
          {
            side: "BASELINE",
            expectation_id: "reply-confirms",
            status: "PASS",
            reason: null,
            critical: true,
          },
        ],
      },
    ],
    next_cursor: null,
  } satisfies ReviewQueuePage;

  it("lists each case with the results that need a person, and why", async () => {
    stub((url) => (url.startsWith("/api/v1/reviews") ? queue : url === "/api/v1/projects" ? projects : null));
    renderAs(<ReviewQueue />, ["read"]);
    const item = await screen.findByTestId("review-item");
    expect(within(item).getByRole("link", { name: "refund-tool-success-lie" })).toHaveAttribute(
      "href",
      `/evaluations/${RUN}/cases/refund-tool-success-lie`,
    );
    expect(item).toHaveTextContent("support-refund-agent v1.2.4 → v1.3.0");
    const pending = within(item).getAllByTestId("pending-result");
    expect(pending[0]).toHaveTextContent("candidate");
    expect(pending[0]).toHaveTextContent("the judge could not grade it");
    expect(pending[0]).toHaveTextContent("not valid JSON");
    expect(pending[1]).toHaveTextContent("critical, graded by a judge that is not calibrated for it");
  });

  it("says when there is nothing to review", async () => {
    stub((url) => (url.startsWith("/api/v1/reviews") ? { items: [], next_cursor: null } : projects));
    renderAs(<ReviewQueue />, ["read"]);
    expect(await screen.findByText("Nothing to review")).toBeInTheDocument();
  });

  it("explains every reason a result is queued", () => {
    expect(pendingWhy({ status: "SKIPPED", critical: false })).toMatch(/^not judged/);
    expect(pendingWhy({ status: "FAIL", critical: false })).toBe("graded by an uncalibrated judge");
  });
});

describe("JudgesView", () => {
  const calibration = {
    id: "01a0d77c-0000-7000-8000-000000000001",
    organization_id: liveEvalRun.run.organization_id,
    project_id: PROJECT,
    criterion: "task_completion",
    status: "COMPLETED",
    example_count: 20,
    examples_sha256: "e".repeat(64),
    judge: {
      provider: "deterministic-fake",
      model: "keyword-overlap-v1",
      kind: "deterministic-fake",
      prompt_version: "judge-prompt/1",
      prompt_sha256: liveJudge.judge.prompt_sha256,
    },
    metrics: {
      examples: 20,
      agreed: 19,
      accuracy: 0.95,
      kappa: 0.9,
      confusion: { true_pass: 10, true_fail: 9, false_pass: 1, false_fail: 0 },
      errors: 0,
    },
    calibrated: true,
    reason: "Agreement 95% and kappa 0.90 meet the requirements.",
    error: null,
    requested_by: "user:u-1",
    created_at: "2026-09-25T08:00:00Z",
    started_at: "2026-09-25T08:00:01Z",
    finished_at: "2026-09-25T08:00:02Z",
  } satisfies Calibration;
  const judge = {
    ...liveJudge,
    criteria: liveJudge.criteria.map((c) =>
      c.criterion === "task_completion" ? { ...c, calibrated: true, calibration } : c,
    ),
  } satisfies EvaluationSchemas["JudgeDescription"];

  function stubJudge() {
    stub((url, method) => {
      if (url === "/api/v1/projects") return projects;
      if (url === `/api/v1/judges?project_id=${PROJECT}`) return judge;
      if (url.startsWith("/api/v1/judges/calibrations?")) return { items: [calibration], next_cursor: null };
      if (url === `/api/v1/judges/calibrations/${calibration.id}`)
        return {
          calibration: {
            ...calibration,
            disagreements: [{ id: "ex-7", human: "fail", judge: "pass", score: 0.8 }],
          },
        };
      if (url === "/api/v1/judges/calibrations" && method === "POST")
        return { calibration: { ...calibration, status: "QUEUED" } };
      return null;
    });
  }

  const example = (i: number) =>
    JSON.stringify({
      id: `ex-${i}`,
      rubric: "The reply says the refund was issued.",
      customer_message: "Refund please.",
      answer: i % 2 ? "The refund was issued." : "I cannot help.",
      human_label: i % 2 ? "pass" : "fail",
    });

  it("shows the judge, what each criterion's calibration says, and its disagreements", async () => {
    stubJudge();
    renderAs(<JudgesView />, ["read"]);
    expect(await screen.findByTestId("judge-identity")).toHaveTextContent("not a language model");
    const criteria = screen.getAllByTestId("criterion");
    expect(criteria).toHaveLength(8);
    const task = criteria.find((c) => c.getAttribute("data-criterion") === "task_completion")!;
    expect(task).toHaveAttribute("data-calibrated", "true");
    expect(task).toHaveTextContent("agreed 19 of 20 (95%)");
    expect(task).toHaveTextContent("κ 0.90");
    expect(criteria.find((c) => c.getAttribute("data-criterion") === "rubric")).toHaveTextContent(
      "never calibrated",
    );
    await userEvent.click(await screen.findByRole("button", { name: "Disagreements" }));
    const table = await screen.findByTestId("disagreements");
    expect(table).toHaveTextContent("ex-7failpass (0.80)");
    // Without review.write there is no form.
    expect(screen.queryByTestId("calibrate-form")).not.toBeInTheDocument();
  });

  it("sends pasted examples for the chosen criterion, and nothing while they are invalid", async () => {
    stubJudge();
    renderAs(<JudgesView />, ["read", "review.write"]);
    const form = await screen.findByTestId("calibrate-form");
    const box = within(form).getByLabelText(/Labeled examples/);
    const send = within(form).getByRole("button", { name: /Calibrate/ });
    expect(send).toBeDisabled();
    fireEvent.change(box, { target: { value: '{"id": "a"}' } });
    expect(
      within(form).getByText(/example 1: rubric, customer_message, answer must be text/),
    ).toBeInTheDocument();
    expect(send).toBeDisabled();
    fireEvent.change(box, { target: { value: example(1) } });
    expect(within(form).getByTestId("example-count")).toHaveTextContent(
      "1 example — a calibration needs at least 20",
    );
    const lines = Array.from({ length: 20 }, (_, i) => example(i + 1)).join("\n");
    fireEvent.change(box, { target: { value: lines } });
    expect(within(form).getByTestId("example-count")).toHaveTextContent("20 examples.");
    await userEvent.selectOptions(within(form).getByLabelText("Criterion"), "correctness");
    await userEvent.click(send);
    expect(sent).toHaveLength(1);
    expect(sent[0]!.url).toBe("/api/v1/judges/calibrations");
    expect(sent[0]!.key).toMatch(/^calibrate-[0-9a-f]{32}$/);
    const body = sent[0]!.body as { project_id: string; criterion: string; examples: { id: string }[] };
    expect(body.project_id).toBe(PROJECT);
    expect(body.criterion).toBe("correctness");
    expect(body.examples.map((e) => e.id)).toEqual(Array.from({ length: 20 }, (_, i) => `ex-${i + 1}`));
    expect(box).toHaveValue("");
  });
});
