import { Lock } from "lucide-react";
import type { ReactNode } from "react";
import { CopyButton } from "@/components/ui/copy-button";
import { KeyValue, type KeyValueItem } from "@/components/ui/key-value";
import { formatCost, formatDuration, formatNumber, humanize, shortId } from "@/lib/format";
import type { Span, SpanAttributes, SpanContent } from "@/lib/types";
import { KIND_LABEL, KindIcon, RiskBadge, StatusBadge } from "./badges";

function Mono({ children }: { children: ReactNode }) {
  return <span className="font-mono text-xs">{children}</span>;
}

function Hash({ value, label }: { value: string | undefined; label: string }) {
  if (!value) return null;
  return (
    <span className="inline-flex items-center gap-1">
      <Mono>{shortId(value, 12)}</Mono>
      <CopyButton value={value} label={label} />
    </span>
  );
}

function kindItems(span: Span, a: SpanAttributes): KeyValueItem[] {
  switch (span.kind) {
    case "model":
      return [
        { label: "Provider", value: a.provider },
        { label: "Model", value: a.response_model ?? a.request_model },
        {
          label: "Tokens",
          value:
            a.input_tokens !== undefined || a.output_tokens !== undefined
              ? `${formatNumber(a.input_tokens)} in · ${formatNumber(a.output_tokens)} out`
              : null,
        },
        { label: "Temperature", value: a.temperature },
        { label: "Finish reasons", value: a.finish_reasons?.join(", ") },
        { label: "Cost", value: a.cost_usd !== undefined ? formatCost(a.cost_usd) : null },
        { label: "Prompt", value: a.prompt_hash ? <Hash value={a.prompt_hash} label="prompt hash" /> : null },
        { label: "Prompt version", value: a.prompt_version },
      ];
    case "tool":
      return [
        { label: "Tool", value: a.tool_name },
        { label: "Risk", value: <RiskBadge risk={a.tool_risk} /> },
        { label: "Result", value: humanize(a.tool_result_status) },
        { label: "Attempt", value: a.attempt },
        {
          label: "Idempotency key",
          value: a.idempotency_key_hash ? (
            <Hash value={a.idempotency_key_hash} label="idempotency key hash" />
          ) : (
            "none"
          ),
        },
        {
          label: "Arguments",
          value: a.tool_args_hash ? <Hash value={a.tool_args_hash} label="arguments hash" /> : null,
        },
        { label: "Tool version", value: a.tool_version },
        { label: "HTTP status", value: a.http_status },
        { label: "Call id", value: a.tool_call_id ? <Mono>{a.tool_call_id}</Mono> : null },
      ];
    case "retrieval":
      return [
        { label: "Source", value: a.retrieval_source },
        { label: "Documents", value: a.document_count },
      ];
    case "policy":
      return [
        { label: "Decision", value: humanize(a.policy_decision) },
        {
          label: "Policy",
          value: a.policy_name ? `${a.policy_name}${a.policy_version ? ` v${a.policy_version}` : ""}` : null,
        },
        { label: "Rule", value: a.policy_rule },
      ];
    case "outcome":
      return [
        { label: "Outcome", value: humanize(a.outcome_status) },
        { label: "Claimed", value: humanize(a.outcome_claimed) },
        { label: "Verified", value: a.outcome_verified ? "yes" : "no (self-report)" },
        { label: "Evidence", value: humanize(a.verification_source) },
        { label: "Business outcome", value: humanize(a.business_outcome) },
      ];
    case "http":
    case "mcp":
      return [
        { label: "Method", value: a.http_method ?? a.mcp_method },
        { label: "Host", value: a.http_host ?? a.mcp_server },
        { label: "Status", value: a.http_status },
      ];
    default:
      return [];
  }
}

const CONTENT_FIELDS: { key: keyof SpanContent; label: string }[] = [
  { key: "system_instructions", label: "System instructions" },
  { key: "input", label: "Input" },
  { key: "tool_args", label: "Tool arguments" },
  { key: "tool_result", label: "Tool result" },
  { key: "output", label: "Output" },
];

function prettyContent(raw: string): string {
  try {
    return JSON.stringify(JSON.parse(raw), null, 2);
  } catch {
    return raw;
  }
}

export function SpanDetails({
  span,
  offsetMs,
  contentMode,
}: {
  span: Span;
  offsetMs: number;
  contentMode: string;
}) {
  const a = span.attributes ?? {};
  const errorItems: KeyValueItem[] =
    span.status === "ERROR" || a.error_type || a.exception_type
      ? [
          { label: "Error type", value: a.error_type ?? a.exception_type },
          { label: "Message", value: a.exception_message ?? span.status_message },
        ]
      : [];
  const extra = Object.entries(a.extra ?? {});
  const diff = Object.entries(a.final_state_diff ?? {});
  const content = span.content ?? undefined;
  const hasContent = CONTENT_FIELDS.some((f) => content?.[f.key]);

  return (
    <div className="space-y-4" data-testid="span-details">
      <div className="flex flex-wrap items-center gap-2">
        <KindIcon kind={span.kind} className="h-4 w-4 text-slate-600" />
        <h3 className="min-w-0 truncate text-sm font-semibold text-slate-900">{span.name}</h3>
        <StatusBadge status={span.status} />
      </div>
      <KeyValue
        items={[
          { label: "Kind", value: KIND_LABEL[span.kind] ?? span.kind },
          { label: "Starts at", value: `+${formatDuration(offsetMs)}` },
          { label: "Duration", value: formatDuration(span.duration_ms) },
          { label: "Span id", value: <Hash value={span.span_id} label="span id" /> },
          ...kindItems(span, a),
          ...errorItems,
        ]}
      />
      {diff.length > 0 ? (
        <section>
          <h4 className="mb-1 text-xs font-medium uppercase tracking-wide text-slate-500">State change</h4>
          <ul className="space-y-0.5 font-mono text-xs">
            {diff.map(([k, v]) => (
              <li key={k}>
                {k}:{" "}
                {Array.isArray(v) && v.length === 2
                  ? `${JSON.stringify(v[0])} → ${JSON.stringify(v[1])}`
                  : JSON.stringify(v)}
              </li>
            ))}
          </ul>
        </section>
      ) : null}
      {span.events?.length ? (
        <section>
          <h4 className="mb-1 text-xs font-medium uppercase tracking-wide text-slate-500">Events</h4>
          <ul className="space-y-1 text-xs">
            {span.events.map((ev, i) => (
              <li key={`${ev.name}-${i}`} className="rounded bg-slate-50 px-2 py-1">
                <span className="font-medium">{ev.name}</span>
                {ev.attributes ? (
                  <span className="ml-2 break-all font-mono text-slate-600">
                    {JSON.stringify(ev.attributes)}
                  </span>
                ) : null}
              </li>
            ))}
          </ul>
        </section>
      ) : null}
      {extra.length > 0 ? (
        <section>
          <h4 className="mb-1 text-xs font-medium uppercase tracking-wide text-slate-500">
            Other attributes
          </h4>
          <KeyValue
            items={extra.map(([k, v]) => ({
              label: k,
              value: <Mono>{typeof v === "string" ? v : JSON.stringify(v)}</Mono>,
            }))}
          />
        </section>
      ) : null}
      <section>
        <h4 className="mb-1 text-xs font-medium uppercase tracking-wide text-slate-500">Content</h4>
        {hasContent ? (
          <div className="space-y-2">
            <p className="text-xs text-slate-500">
              <Lock className="mr-1 inline-block h-3.5 w-3.5 align-[-2px]" aria-hidden="true" />
              Captured under the project&apos;s <strong>{contentMode}</strong> content policy
              {contentMode === "redacted" ? "; secrets and personal data were masked before storage" : ""}.
            </p>
            {CONTENT_FIELDS.filter((f) => content?.[f.key]).map((f) => (
              <details
                key={f.key}
                className="rounded border border-slate-200"
                open={f.key !== "system_instructions"}
              >
                <summary className="cursor-pointer px-2 py-1 text-xs font-medium text-slate-700">
                  {f.label}
                </summary>
                <pre className="max-h-64 relative overflow-auto whitespace-pre-wrap break-words border-t border-slate-100 bg-slate-50 p-2 font-mono text-xs text-slate-800">
                  {prettyContent(content?.[f.key] ?? "")}
                </pre>
              </details>
            ))}
          </div>
        ) : (
          <p className="text-xs text-slate-500">
            {contentMode === "off"
              ? "Content capture is off for this project; only metadata, hashes and counts are stored."
              : "No content was captured for this span."}
          </p>
        )}
      </section>
    </div>
  );
}
