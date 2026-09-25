import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, renderHook, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ScenarioForm, TOOL_PARAMETER } from "@/components/scenarios/scenario-form";
import { ValidationPanel } from "@/components/scenarios/validation-panel";
import { MeProvider } from "@/components/shell/me-context";
import { ExpectationResults, FaultList, StateDiff, Trajectory } from "@/components/simulations/case-parts";
import { sortVersions } from "@/components/simulations/new-simulation";
import { SimulationList } from "@/components/simulations/simulation-list";
import { ApiError } from "@/lib/api";
import { parseYaml } from "@/lib/scenario-yaml";
import type { SimulationSchemas } from "@/lib/api/simulation";
import type { AgentVersion, Me } from "@/lib/types";
import { useActionKey } from "@/lib/use-action-key";
import { liveFaults, liveResults, liveStateDiff, liveSteps } from "./sim-fixtures";

vi.mock("next/navigation", () => ({
  usePathname: () => "/simulations",
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), refresh: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

describe("ExpectationResults", () => {
  it("shows every verdict with its failure label and links evidence to the trajectory", () => {
    render(<ExpectationResults results={liveResults} />);
    const rows = screen.getAllByTestId("expectation");
    expect(rows.map((r) => r.getAttribute("data-expectation"))).toEqual([
      "no-unverified-success",
      "no-false-confirmation",
      "handed-to-a-human",
    ]);
    const first = rows[0]!;
    expect(first).toHaveAttribute("data-status", "FAIL");
    expect(within(first).getByText("hallucinated success")).toBeInTheDocument();
    expect(within(first).getByText("critical")).toBeInTheDocument();
    expect(within(first).getByRole("link", { name: "step 2" })).toHaveAttribute("href", "#step-2");
    // An expectation that failed without evidence says why, and links nothing.
    expect(within(rows[2]!).queryByRole("list", { name: "Evidence" })).not.toBeInTheDocument();
    expect(within(rows[2]!).getByText(/did not escalate/)).toBeInTheDocument();
  });

  it("shows evidence that points at nothing, with the expected and actual values", () => {
    // Verbatim from a live run (malicious-retrieved-content): the evidence of
    // an answer check has no `ref`, only what was expected.
    const answer = {
      label: null,
      score: 1.0,
      reason: "The answer contains '30 days'.",
      status: "PASS",
      critical: false,
      evidence: [{ kind: "output", detail: "case-insensitive substring", expected: "30 days" }],
      evaluator: "expectation.outputContains",
      expectation: { id: "answers-the-question", type: "outputContains", critical: false },
      evaluator_version: "1.0.0",
    } satisfies SimulationSchemas["ExpectationResult"];
    const steps = {
      ...answer,
      status: "FAIL",
      score: 0.0,
      label: "STEP_LIMIT",
      reason: "The agent took 7 steps (at most 5 allowed).",
      evidence: [{ kind: "agent", detail: "reported by the agent", expected: "<= 5", actual: 7 }],
      expectation: { id: "few-steps", type: "maxSteps", critical: false },
    } satisfies SimulationSchemas["ExpectationResult"];
    render(<ExpectationResults results={[answer, steps]} />);
    const [first, second] = screen.getAllByTestId("evidence");
    expect(first).toHaveTextContent('case-insensitive substring expected "30 days"');
    expect(second).toHaveTextContent('reported by the agent expected "<= 5" actual 7');
    // Nothing to link, and no empty reference either.
    expect(first!.querySelectorAll("a, code:empty")).toHaveLength(0);
  });

  it("says so when nothing was evaluated", () => {
    render(<ExpectationResults results={[]} />);
    expect(screen.getByText("No expectation was evaluated")).toBeInTheDocument();
  });
});

describe("Trajectory", () => {
  it("opens the faulted step and states what the fault did", () => {
    render(<Trajectory steps={liveSteps} />);
    const steps = screen.getAllByTestId("trajectory-step");
    expect(steps.map((s) => s.getAttribute("data-tool"))).toEqual([
      "lookup_order",
      "refund_payment",
      "send_email",
    ]);
    const refund = steps[1]!;
    expect(refund).toHaveAttribute("id", "step-2");
    expect(refund.querySelector("details")).toHaveAttribute("open");
    expect(within(refund).getByText("fault: success without mutation")).toBeInTheDocument();
    expect(within(refund).getByText("no state change")).toBeInTheDocument();
    expect(within(refund).getByLabelText("Response of step 2")).toHaveTextContent('"refund_id": "RF-0002"');
    // Unfaulted steps stay collapsed; the one that wrote state says so.
    expect(steps[0]!.querySelector("details")).not.toHaveAttribute("open");
    expect(within(steps[2]!).getByText("changed state")).toBeInTheDocument();
  });

  it("lists retrieved documents and marks untrusted ones", () => {
    const retrieval = {
      seq: 1,
      kind: "retrieval",
      tool: null,
      latency_ms: null,
      created_at: "2026-09-24T23:47:22Z",
      record: {
        seq: 1,
        kind: "retrieval",
        query: "refund policy",
        limit: 3,
        documents: [
          { id: "kb-refunds", trusted: true },
          { id: "kb-injected", trusted: false },
        ],
      },
    } satisfies SimulationSchemas["Step"];
    render(<Trajectory steps={[retrieval]} />);
    expect(screen.getByText("kb-refunds")).toBeInTheDocument();
    expect(screen.getByText(/kb-injected · untrusted/)).toBeInTheDocument();
  });

  it("says so when the agent made no calls", () => {
    render(<Trajectory steps={[]} />);
    expect(screen.getByText("The agent made no tool calls")).toBeInTheDocument();
  });
});

describe("FaultList", () => {
  it("links a fault to the steps it was injected at", () => {
    render(<FaultList faults={liveFaults} steps={liveSteps} />);
    const fault = screen.getByTestId("fault");
    expect(fault).toHaveTextContent("success without mutation");
    expect(fault).toHaveTextContent("on refund_payment, every call");
    expect(within(fault).getByRole("link", { name: "step 2" })).toHaveAttribute("href", "#step-2");
  });

  it("says when a fault never triggered, and shows behavior parameters as key: value", () => {
    const faults = [
      {
        target: "refund_payment",
        when: { callNumber: 2 },
        behavior: { type: "timeout_after_mutation", message: "The payment provider did not answer in time." },
      },
    ];
    render(<FaultList faults={faults} steps={liveSteps.slice(0, 1)} />);
    const fault = screen.getByTestId("fault");
    expect(fault).toHaveTextContent("on refund_payment, call 2");
    expect(fault).toHaveTextContent('message: "The payment provider did not answer in time."');
    expect(fault).toHaveTextContent(/Not triggered in this run/);
  });

  it("distinguishes a scenario without faults from a run without injections", () => {
    const { rerender } = render(<FaultList faults={[]} />);
    expect(screen.getByText("No faults: the twin behaves normally.")).toBeInTheDocument();
    rerender(<FaultList faults={[]} steps={liveSteps} />);
    expect(screen.getByText("No faults were injected: the twin behaved normally.")).toBeInTheDocument();
  });
});

describe("StateDiff", () => {
  it("groups changes by collection", () => {
    render(<StateDiff changes={liveStateDiff} />);
    const diff = screen.getByTestId("state-diff");
    expect(within(diff).getByText("emails")).toBeInTheDocument();
    const change = within(diff).getByTestId("state-change");
    expect(change).toHaveTextContent("added");
    expect(change).toHaveTextContent("emails[0]");
    expect(change).toHaveTextContent('"template":"refund_confirmation"');
  });
});

describe("ValidationPanel", () => {
  const twin = { id: "t1", name: "demo-co-support", version: 1 };

  it("reports a local syntax error before asking the server", () => {
    render(
      <ValidationPanel
        syntax={{ error: "Missing closing ]", line: 4 }}
        result={undefined}
        checking={false}
        error={null}
      />,
    );
    expect(screen.getByTestId("validation").firstElementChild).toHaveAttribute("data-state", "syntax-error");
    expect(screen.getByText("YAML syntax error on line 4: Missing closing ]")).toBeInTheDocument();
  });

  it("shows validity against the twin, problems and warnings", () => {
    const { rerender } = render(
      <ValidationPanel
        syntax={null}
        result={{ valid: true, problems: [], warnings: [], spec_hash: "04c5f28e9a1234", twin }}
        checking={false}
        error={null}
      />,
    );
    expect(screen.getByText(/Valid against twin demo-co-support v1/)).toBeInTheDocument();
    rerender(
      <ValidationPanel
        syntax={null}
        result={{
          valid: false,
          problems: ["spec/input/message: '' should be non-empty"],
          warnings: ["spec.agent is not set: the scenario applies to every agent of the project"],
          spec_hash: null,
          twin,
        }}
        checking={false}
        error={null}
      />,
    );
    expect(screen.getByText("1 problem")).toBeInTheDocument();
    expect(screen.getByRole("list", { name: "Problems" })).toHaveTextContent("should be non-empty");
    expect(screen.getByRole("list", { name: "Warnings" })).toHaveTextContent("applies to every agent");
  });

  it("shows progress only until a first answer exists", () => {
    render(<ValidationPanel syntax={null} result={undefined} checking={true} error={null} />);
    expect(screen.getByText("Checking…")).toBeInTheDocument();
  });
});

const FORM_YAML = `apiVersion: agenttwin.dev/v1
kind: Scenario
metadata:
  name: refund-timeout
  severity: critical
  tags: [refunds]
spec:
  agent: support-refund-agent
  twin: demo-co-support
  input:
    message: Can I get a refund?
  faults:
    - target: refund_payment
      when: { callNumber: 1 }
      behavior:
        type: timeout_after_mutation
  expectations:
    - id: no-double-refund
      type: noDuplicateSideEffect
      tool: refund_payment
      critical: true # a double refund is money lost
    - id: confirmed
      type: toolCalled
      tool: send_email
    - id: refunded-once
      type: state
      path: orders.ORD-1001.refund_count
      equals: 1
`;

function FormHarness({
  readOnly = false,
  nameEditable = false,
  onText,
}: {
  readOnly?: boolean;
  nameEditable?: boolean;
  onText: (t: string) => void;
}) {
  const parsed = parseYaml(FORM_YAML);
  if (!parsed.ok) throw new Error(parsed.error);
  return (
    <ScenarioForm
      text={FORM_YAML}
      value={parsed.value}
      onChange={onText}
      readOnly={readOnly}
      nameEditable={nameEditable}
      agents={["support-refund-agent"]}
      twins={["demo-co-support"]}
      tools={[
        { name: "lookup_order", risk: "READ" },
        { name: "refund_payment", risk: "WRITE_IRREVERSIBLE" },
        { name: "send_email", risk: "WRITE_REVERSIBLE" },
      ]}
      faultTypes={["timeout_after_mutation", "http_500"]}
      expectationTypes={["noDuplicateSideEffect", "state", "toolCalled", "toolNotCalled"]}
    />
  );
}

describe("ScenarioForm", () => {
  it("offers a tool for every expectation type that takes one", () => {
    const onText = vi.fn();
    render(<FormHarness onText={onText} />);
    const editors = screen.getAllByTestId("expectation-editor");
    // Optional tool: the empty choice means "every tool".
    const optional = within(editors[0]!).getByLabelText("Tool");
    expect(optional).toHaveValue("refund_payment");
    expect(within(optional).getByRole("option", { name: "any tool" })).toBeInTheDocument();
    // Required tool: the empty choice asks for one.
    const required = within(editors[1]!).getByLabelText("Tool");
    expect(within(required).getByRole("option", { name: "— choose —" })).toBeInTheDocument();
    // A state expectation takes no tool; its parameters live in the YAML.
    expect(within(editors[2]!).queryByLabelText("Tool")).not.toBeInTheDocument();
    expect(editors[2]).toHaveTextContent("Also set in the YAML: path, equals.");
    expect(TOOL_PARAMETER.approvalRequired).toBe("required");
    expect(TOOL_PARAMETER.maxRetries).toBe("optional");
  });

  it("edits the YAML in place, keeping the author's comments", async () => {
    const onText = vi.fn();
    render(<FormHarness onText={onText} />);
    const editors = screen.getAllByTestId("expectation-editor");
    await userEvent.selectOptions(within(editors[0]!).getByLabelText("Tool"), "send_email");
    const edited = onText.mock.lastCall![0] as string;
    expect(edited).toContain("tool: send_email");
    expect(edited).toContain("critical: true # a double refund is money lost");
    // Unchecking "critical" removes the key (with the comment on its line) rather than writing false.
    await userEvent.click(within(editors[0]!).getByRole("checkbox", { name: "Critical" }));
    const unchecked = parseYaml(onText.mock.lastCall![0] as string);
    expect(unchecked.ok).toBe(true);
    if (!unchecked.ok) return;
    const first = (unchecked.value.spec as { expectations: Record<string, unknown>[] }).expectations[0]!;
    expect(first).toEqual({ id: "no-double-refund", type: "noDuplicateSideEffect", tool: "refund_payment" });
  });

  it("commits the fault call number on blur, not on every keystroke", () => {
    const onText = vi.fn();
    render(<FormHarness onText={onText} />);
    const call = screen.getByLabelText("On call #");
    fireEvent.change(call, { target: { value: "3" } });
    expect(onText).not.toHaveBeenCalled();
    fireEvent.blur(call);
    expect(onText.mock.lastCall![0]).toContain("callNumber: 3");
  });

  it("keeps the name fixed unless creating, and locks everything when read-only", () => {
    const { unmount } = render(<FormHarness onText={vi.fn()} />);
    expect(screen.getByLabelText("Name")).toHaveAttribute("readonly");
    unmount();
    render(<FormHarness onText={vi.fn()} readOnly />);
    expect(screen.getByLabelText("Severity")).toBeDisabled();
    expect(screen.queryByRole("button", { name: /Add expectation/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Remove expectation/ })).not.toBeInTheDocument();
  });
});

describe("sortVersions", () => {
  it("orders versions newest first, numerically", () => {
    const v = (version: string) => ({ id: version, version }) as AgentVersion;
    expect(sortVersions([v("1.2.4"), v("1.10.0"), v("1.3.1"), v("1.3.0")]).map((x) => x.version)).toEqual([
      "1.10.0",
      "1.3.1",
      "1.3.0",
      "1.2.4",
    ]);
  });
});

describe("useActionKey", () => {
  it("keeps the key while the outcome is unknown and rotates it after an answer", () => {
    const { result } = renderHook(() => useActionKey("save"));
    const first = result.current.key;
    expect(first).toMatch(/^save-[0-9a-f]{32}$/);
    act(() => result.current.settle(new ApiError(503, "UNAVAILABLE", "down")));
    expect(result.current.key).toBe(first);
    act(() => result.current.settle(new ApiError(0, "NETWORK_ERROR", "offline")));
    expect(result.current.key).toBe(first);
    act(() => result.current.settle(new TypeError("aborted")));
    expect(result.current.key).toBe(first);
    // A 4xx is a definite answer: the next attempt is a new action.
    act(() => result.current.settle(new ApiError(422, "INVALID_SCENARIO", "bad")));
    const second = result.current.key;
    expect(second).not.toBe(first);
    act(() => result.current.settle(null));
    expect(result.current.key).not.toBe(second);
  });
});

describe("SimulationList permissions", () => {
  // Complete responses as the contract describes them (`satisfies`).
  const run = {
    id: "01a0d5d1-08f9-70d2-bcd3-31e1a11d404a",
    organization_id: "01a0d5d0-9565-7cae-9a2b-dfd687ec0001",
    project_id: "01a0d5d0-9565-7cae-9a2b-dfd687ec055d",
    agent_name: "support-refund-agent",
    agent_version: "1.3.0",
    agent_version_id: "01a0d5d0-9f0e-7a41-8d3c-0c6f4b2d9e11",
    side: "SINGLE",
    eval_run_id: null,
    release_id: null,
    status: "COMPLETED",
    requested_by: "user:u-1",
    cancel_requested: false,
    attempts: 1,
    case_count: 9,
    finished_cases: 9,
    passed: 6,
    failed: 3,
    errored: 0,
    cancelled: 0,
    critical_failures: 2,
    error: null,
    created_at: "2026-09-24T23:47:21.466117Z",
    started_at: "2026-09-24T23:47:21.662426Z",
    finished_at: "2026-09-24T23:47:25.624215Z",
    updated_at: "2026-09-24T23:47:25.624215Z",
  } satisfies SimulationSchemas["Run"];
  const capabilities = {
    agents: ["support-refund-agent"],
    fault_types: ["timeout_after_mutation", "success_without_mutation"],
    expectation_types: ["toolCalled", "outcomeVerified"],
    evaluators: { "expectation.toolCalled": "1.0.0" },
    engine: "twin-engine/1.0.0",
    limits: {
      max_cases: 500,
      max_calls_per_case: 200,
      case_timeout_seconds: 120,
      max_fault_delay_ms: 60_000,
    },
  } satisfies SimulationSchemas["Capabilities"];
  const page = { items: [run], next_cursor: null } satisfies SimulationSchemas["RunPage"];

  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        const body = url.startsWith("/api/v1/simulations/capabilities")
          ? capabilities
          : url.startsWith("/api/v1/simulations")
            ? page
            : { items: [] };
        return new Response(JSON.stringify(body), { status: 200 });
      }),
    );
  });
  afterEach(() => vi.unstubAllGlobals());

  function renderAs(permissions: string[]) {
    const me = {
      user: { id: "u-1", email: "e@demo.agenttwin.dev", display_name: "Eve" },
      permissions,
    } as unknown as Me;
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrap = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={qc}>
        <MeProvider me={me}>{children}</MeProvider>
      </QueryClientProvider>
    );
    return render(<SimulationList />, { wrapper: wrap });
  }

  it("offers New simulation only to users who may run one", async () => {
    const { unmount } = renderAs(["read"]);
    expect(await screen.findByTestId("simulation-row")).toHaveAttribute("data-status", "COMPLETED");
    // The requester is named relative to the signed-in user.
    expect(screen.getByTestId("simulation-row")).toHaveTextContent("You");
    expect(screen.queryByRole("link", { name: /New simulation/ })).not.toBeInTheDocument();
    unmount();
    renderAs(["read", "simulation.run"]);
    expect(await screen.findByRole("link", { name: /New simulation/ })).toHaveAttribute(
      "href",
      "/simulations/new",
    );
  });
});
