import type { Span, SpanKind } from "@/lib/types";

const T0 = Date.parse("2026-09-24T10:00:00.000Z");

export function span(
  id: string,
  parent: string | null,
  kind: SpanKind,
  startMs: number,
  durMs: number,
  extra: Partial<Span> = {},
): Span {
  return {
    span_id: id,
    parent_span_id: parent,
    name: extra.name ?? `${kind}-${id}`,
    kind,
    status: "OK",
    status_message: null,
    started_at: new Date(T0 + startMs).toISOString(),
    ended_at: new Date(T0 + startMs + durMs).toISOString(),
    duration_ms: durMs,
    tool_name: null,
    tool_risk: null,
    model: null,
    policy_decision: null,
    attributes: {},
    events: null,
    semconv_version: "genai-v1.37",
    ...extra,
  };
}

/** The demo regression: refund times out after mutation and is retried. */
export function candidateSpans(): Span[] {
  return [
    span("root", null, "agent", 0, 2000, { name: "invoke_agent support-refund-agent" }),
    span("m1", "root", "model", 10, 40, { name: "chat scripted-planner-v1" }),
    span("t1", "root", "tool", 100, 20, {
      name: "execute_tool lookup_order",
      tool_name: "lookup_order",
      tool_risk: "READ",
      attributes: { tool_name: "lookup_order", tool_args_hash: "h-o" },
    }),
    span("t2", "root", "tool", 200, 100, {
      name: "execute_tool refund_payment",
      tool_name: "refund_payment",
      tool_risk: "WRITE_IRREVERSIBLE",
      status: "ERROR",
      attributes: {
        tool_name: "refund_payment",
        tool_args_hash: "h-r",
        tool_result_status: "timeout",
        attempt: 1,
      },
    }),
    span("h1", "t2", "http", 205, 90, { name: "POST payments" }),
    span("t3", "root", "tool", 400, 50, {
      name: "execute_tool refund_payment",
      tool_name: "refund_payment",
      tool_risk: "WRITE_IRREVERSIBLE",
      attributes: {
        tool_name: "refund_payment",
        tool_args_hash: "h-r",
        tool_result_status: "ok",
        attempt: 2,
      },
    }),
    span("o1", "root", "outcome", 1900, 5, {
      name: "outcome.report",
      attributes: { outcome_status: "SUCCESS", outcome_claimed: "SUCCESS", outcome_verified: false },
    }),
  ];
}
