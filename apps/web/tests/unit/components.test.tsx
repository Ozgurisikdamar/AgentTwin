import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { OutcomeCard } from "@/components/traces/outcome-card";
import { WaterfallView } from "@/components/traces/waterfall";
import { buildWaterfall } from "@/lib/spans";
import type { Outcome, Trace } from "@/lib/types";
import { candidateSpans } from "./fixtures";

describe("WaterfallView", () => {
  it("renders every span with an accessible timing label and supports collapse", async () => {
    const onSelect = vi.fn();
    const w = buildWaterfall(candidateSpans());
    render(<WaterfallView waterfall={w} selected="root" onSelect={onSelect} />);
    expect(screen.getAllByTestId("waterfall-row")).toHaveLength(7);
    const refund = screen.getByRole("button", {
      name: /Tool call execute_tool refund_payment, write irreversible risk, error, starts at 200 ms, lasts 100 ms/,
    });
    await userEvent.click(refund);
    expect(onSelect).toHaveBeenCalledWith("t2");
    // The compact risk tag is visible text, not color alone.
    expect(within(refund).getByText("irreversible")).toHaveAttribute("title", "Risk: write irreversible");
    // Duration labels never leave the timeline: the root bar spans the whole
    // trace, so its label sits inside the bar.
    const rows = screen.getAllByTestId("waterfall-row");
    expect(rows[0]!.querySelector("[data-placement]")).toHaveAttribute("data-placement", "inside");
    // Collapsing the failed refund hides its HTTP child span.
    await userEvent.click(screen.getByRole("button", { name: "Collapse execute_tool refund_payment" }));
    expect(screen.getAllByTestId("waterfall-row")).toHaveLength(6);
    expect(screen.getByRole("button", { name: "Expand execute_tool refund_payment" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    // The selected span is announced as pressed.
    expect(screen.getByRole("button", { name: /Agent run invoke_agent/ })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });
});

const trace = {
  outcome_status: "FAILURE",
  outcome_verified: true,
} as unknown as Trace;

describe("OutcomeCard", () => {
  it("flags a contradicted self-report and compares state", () => {
    const outcome: Outcome = {
      status: "FAILURE",
      business_outcome: "REFUND_INCORRECT",
      verified: true,
      verification_source: "state_assertion",
      claimed_status: "SUCCESS",
      contradiction: true,
      expected_state: { refund_count: 1, refunded_amount: 50 },
      actual_state: { refund_count: 2, refunded_amount: 100 },
      notes: "Duplicate refund",
      source: "api",
      recorded_by: "apikey:1",
      recorded_at: "2026-09-24T10:00:00Z",
    };
    render(<OutcomeCard trace={trace} outcome={outcome} spans={candidateSpans()} />);
    const card = screen.getByTestId("outcome-card");
    expect(within(card).getByRole("alert")).toHaveTextContent(
      "The agent claimed Success, but the evidence shows Failure",
    );
    expect(within(card).getAllByText("mismatch")).toHaveLength(2);
    expect(within(card).getByText("verified")).toBeInTheDocument();
    expect(within(card).getByText("State assertion")).toBeInTheDocument();
  });

  it("labels an unverified self-report as a claim", () => {
    const claimed = { outcome_status: "SUCCESS", outcome_verified: false } as unknown as Trace;
    render(<OutcomeCard trace={claimed} outcome={null} spans={candidateSpans()} />);
    const card = screen.getByTestId("outcome-card");
    expect(within(card).getByText("claimed")).toBeInTheDocument();
    expect(within(card).getByText("None — self-reported only")).toBeInTheDocument();
    expect(within(card).queryByRole("alert")).not.toBeInTheDocument();
  });
});
