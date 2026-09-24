/** Wire types of the AgentTwin public API (subset used by the web app). */

export type TraceStatus = "OK" | "ERROR" | "UNSET";
export type OutcomeStatus = "SUCCESS" | "PARTIAL" | "FAILURE" | "UNKNOWN";
export type SpanKind =
  "agent" | "model" | "tool" | "retrieval" | "policy" | "outcome" | "http" | "mcp" | "other";

export interface Me {
  principal: { org: string; sub: string; email?: string; role: string; all_projects?: boolean };
  user: { id: string; email: string; display_name: string };
  organization: { id: string; slug: string; name: string };
  memberships: {
    organization_id: string;
    organization_slug: string;
    organization_name: string;
    role: string;
  }[];
  permissions: string[];
}

export interface Project {
  id: string;
  slug: string;
  name: string;
  description?: string;
  content_mode?: string;
}

export interface TraceSummary {
  agent: string;
  agent_version?: string;
  outcome: OutcomeStatus | null;
  outcome_verified?: boolean;
  tools: string[] | null;
  errors: string[] | null;
  violations: string[] | null;
  policy_decisions: string[] | null;
  step_count: number;
  retry_count: number;
  cost_usd: number;
  cost_known: boolean;
  duration_ms: number;
  model: string | null;
  prompt_hash: string | null;
  final_state_diff?: Record<string, unknown>;
  failing_tool?: string;
  last_successful_step?: string;
  error_type?: string;
  tool_sequence_sketch: string;
}

export interface Trace {
  project_id: string;
  trace_id: string;
  organization_id: string;
  environment: string;
  agent_name: string | null;
  agent_version: string | null;
  session_id: string | null;
  release_id: string | null;
  commit_sha: string | null;
  source: string;
  simulation_run_id: string | null;
  scenario_id: string | null;
  root_span_id: string | null;
  root_name: string | null;
  status: TraceStatus;
  started_at: string;
  ended_at: string | null;
  duration_ms: number | null;
  span_count: number;
  model_call_count: number;
  tool_call_count: number;
  error_count: number;
  retry_count: number;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number | null;
  models: string[] | null;
  tools: string[] | null;
  policy_decisions: string[] | null;
  signals: string[] | null;
  prompt_hash: string | null;
  semconv_version: string;
  sdk_name: string | null;
  sdk_version: string | null;
  content_mode: string;
  content_dropped: boolean;
  truncated: boolean;
  outcome_status: OutcomeStatus | null;
  outcome_verified: boolean | null;
  human_reviewed: boolean;
  flagged: boolean;
  summary: TraceSummary | null;
  finalized: boolean;
  content_purged: boolean;
  expires_at: string;
}

export interface SpanAttributes {
  operation?: string;
  agent_name?: string;
  agent_version?: string;
  session_id?: string;
  environment?: string;
  source?: string;
  provider?: string;
  request_model?: string;
  response_model?: string;
  input_tokens?: number;
  output_tokens?: number;
  temperature?: number;
  max_tokens?: number;
  finish_reasons?: string[];
  cost_usd?: number;
  prompt_hash?: string;
  prompt_version?: string;
  tool_name?: string;
  tool_version?: string;
  tool_call_id?: string;
  tool_risk?: string;
  tool_args_hash?: string;
  tool_result_status?: string;
  idempotency_key_hash?: string;
  attempt?: number;
  retrieval_source?: string;
  document_count?: number;
  error_type?: string;
  exception_type?: string;
  exception_message?: string;
  policy_decision?: string;
  policy_name?: string;
  policy_version?: string;
  policy_rule?: string;
  outcome_status?: string;
  outcome_claimed?: string;
  business_outcome?: string;
  outcome_verified?: boolean;
  verification_source?: string;
  http_method?: string;
  http_host?: string;
  http_status?: number;
  mcp_method?: string;
  mcp_server?: string;
  simulation_run_id?: string;
  scenario_id?: string;
  final_state_diff?: Record<string, unknown>;
  extra?: Record<string, unknown>;
}

export interface SpanContent {
  input?: string;
  output?: string;
  system_instructions?: string;
  tool_args?: string;
  tool_result?: string;
}

export interface SpanEvent {
  name: string;
  time: string;
  attributes?: Record<string, unknown>;
}

export interface Span {
  span_id: string;
  parent_span_id: string | null;
  name: string;
  kind: SpanKind;
  status: TraceStatus;
  status_message: string | null;
  started_at: string;
  ended_at: string;
  duration_ms: number;
  tool_name: string | null;
  tool_risk: string | null;
  model: string | null;
  policy_decision: string | null;
  attributes: SpanAttributes | null;
  events: SpanEvent[] | null;
  content?: SpanContent | null;
  semconv_version: string;
}

export interface Outcome {
  status: OutcomeStatus;
  business_outcome: string | null;
  verified: boolean;
  verification_source: string;
  claimed_status: OutcomeStatus | null;
  contradiction: boolean;
  expected_state?: Record<string, unknown> | null;
  actual_state?: Record<string, unknown> | null;
  notes: string | null;
  source: string;
  recorded_by: string;
  recorded_at: string;
}

export interface Flag {
  id: string;
  kind: string;
  reason: string;
  flagged_by: string;
  created_at: string;
}

export interface TraceDetail {
  trace: Trace;
  spans: Span[];
  outcome: Outcome | null;
  flags: Flag[] | null;
}

export interface TracePage {
  items: Trace[];
  next_cursor: string;
}

export interface Facet {
  value: string;
  count: number;
}

export type FacetDimension =
  | "agent"
  | "agent_version"
  | "environment"
  | "release"
  | "source"
  | "status"
  | "outcome"
  | "model"
  | "tool"
  | "signal"
  | "policy_decision";

export interface FacetsResponse {
  from: string;
  facets: Record<FacetDimension, Facet[]>;
  sampled: boolean;
}

export interface TraceStats {
  total: number;
  errors: number;
  error_rate: number;
  p50_ms: number | null;
  p95_ms: number | null;
  cost_usd: number;
  by_outcome: Record<string, number>;
  by_signal: Record<string, number>;
  by_tool: Record<string, number>;
  by_agent_version: Record<string, number>;
  unverified_outcomes: number;
  contradictions: number;
}

export interface Agent {
  id: string;
  project_id: string;
  name: string;
  description?: string | null;
  created_by: string;
  created_at: string;
  version_count: number;
  latest_version?: string | null;
}

export interface AgentVersionTool {
  name: string;
  tool_version: number;
  risk: string;
  definition_sha256: string;
  approval_condition?: string;
}

export interface AgentVersion {
  id: string;
  agent_id: string;
  agent_name: string;
  project_id: string;
  version: string;
  manifest_sha256: string;
  prompt_sha256?: string | null;
  model_provider?: string | null;
  model_name?: string | null;
  runtime_endpoint?: string | null;
  commit_sha?: string | null;
  created_by: string;
  created_at: string;
  tools: AgentVersionTool[] | null;
}

export interface ApiErrorBody {
  error?: { code?: string; message?: string; request_id?: string; details?: Record<string, unknown> };
}
