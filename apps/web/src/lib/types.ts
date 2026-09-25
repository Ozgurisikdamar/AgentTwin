/** Wire types of the AgentTwin public API (subset used by the web app). */

export type TraceStatus = "OK" | "ERROR" | "UNSET";
export type OutcomeStatus = "SUCCESS" | "PARTIAL" | "FAILURE" | "UNKNOWN";
export type SpanKind =
  "agent" | "model" | "tool" | "retrieval" | "policy" | "outcome" | "http" | "mcp" | "other";

export interface Me {
  principal: { org: string; sub: string; email?: string; role: string; all_projects?: boolean };
  /** Absent for an API key or a service. */
  user?: { id: string; email: string; display_name: string };
  organization: { id: string; slug: string; name: string };
  memberships?: {
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

/** The structured summary of a trace: empty (`{}`) until the trace is finalized. */
export interface TraceSummary {
  agent?: string;
  agent_version?: string;
  outcome?: OutcomeStatus | null;
  outcome_verified?: boolean;
  tools?: string[] | null;
  errors?: string[] | null;
  violations?: string[] | null;
  policy_decisions?: string[] | null;
  step_count?: number;
  retry_count?: number;
  cost_usd?: number;
  cost_known?: boolean;
  duration_ms?: number;
  model?: string | null;
  prompt_hash?: string | null;
  final_state_diff?: Record<string, unknown>;
  failing_tool?: string;
  last_successful_step?: string;
  error_type?: string;
  tool_sequence_sketch?: string;
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
  next_cursor: string | null;
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

/** A tool as the version's manifest declares it. */
export interface ManifestTool {
  name: string;
  risk: string;
  /** The condition under which a call needs a human approval. */
  approval_required_when?: string;
}

/** An agent version as the version list answers it (its manifest carries the tools). */
export interface AgentVersion {
  id: string;
  agent_id: string;
  agent_name: string;
  project_id: string;
  version: string;
  manifest: { tools: ManifestTool[] | null };
  manifest_sha256: string;
  prompt_sha256?: string | null;
  model_provider?: string | null;
  model_name?: string | null;
  runtime_endpoint?: string | null;
  commit_sha?: string | null;
  created_by: string;
  created_at: string;
}

export interface ApiErrorBody {
  error?: { code?: string; message?: string; request_id?: string; details?: Record<string, unknown> };
}

// ---------------------------------------------------------------- simulations

export type RunStatus =
  "QUEUED" | "PREPARING" | "RUNNING" | "EVALUATING" | "COMPLETED" | "FAILED" | "CANCELLED";
export type CaseStatus = "PENDING" | "RUNNING" | "PASSED" | "FAILED" | "ERRORED" | "CANCELLED";
export type Severity = "critical" | "high" | "medium" | "low";
export type ResultStatus = "PASS" | "FAIL" | "SKIPPED" | "ERROR";

export interface SimulationRun {
  id: string;
  organization_id: string;
  project_id: string;
  agent_name: string;
  agent_version: string;
  agent_version_id: string | null;
  side: string;
  eval_run_id: string | null;
  release_id: string | null;
  status: RunStatus;
  requested_by: string;
  cancel_requested: boolean;
  attempts: number;
  case_count: number;
  passed: number;
  failed: number;
  errored: number;
  cancelled: number;
  critical_failures: number;
  finished_cases: number;
  error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  updated_at: string;
}

export interface PinnedScenario {
  scenario: string;
  scenario_version_id: string;
  spec_hash: string;
  twin: string;
  twin_definition_id: string;
  twin_version: number;
  twin_spec_hash: string;
  seed: number;
}

export interface RunPinning {
  seed?: number;
  engine?: string;
  correlation_id?: string;
  evaluators?: Record<string, string>;
  agent?: {
    name: string;
    version: string;
    version_id?: string | null;
    manifest_sha256?: string | null;
    prompt_sha256?: string | null;
    model_provider?: string | null;
    model_name?: string | null;
    commit_sha?: string | null;
  };
  scenarios?: PinnedScenario[];
  selection?: { scenarios?: string[] | null; tags?: string[] | null };
}

export interface SimulationCase {
  id: string;
  run_id: string;
  position: number;
  scenario_id: string;
  scenario_version_id: string;
  scenario_name: string;
  severity: Severity;
  twin_definition_id: string;
  status: CaseStatus;
  seed: number;
  tenant: string | null;
  call_count: number;
  trace_id: string | null;
  reason: string | null;
  error: string | null;
  latency_ms: number | null;
  outcome_status: string | null;
  started_at: string | null;
  finished_at: string | null;
  labels: string[];
  score: number | null;
}

export interface RunTransition {
  seq: number;
  from_status: RunStatus | null;
  to_status: RunStatus;
  reason: string | null;
  at: string;
}

export interface SimulationRunDetail {
  run: SimulationRun & { pinning?: RunPinning };
  cases: SimulationCase[];
  transitions: RunTransition[];
}

export interface SimulationPage {
  items: SimulationRun[];
  next_cursor: string | null;
}

/** A started, cancelled or re-run simulation. */
export interface RunResponse {
  run: SimulationRun;
}

export interface Evidence {
  kind: string;
  detail: string;
  /** What the evidence points at (`tool_call:3`); absent for the agent's answer and state checks. */
  ref?: string;
  expected?: unknown;
  actual?: unknown;
}

export interface ExpectationResult {
  status: ResultStatus;
  label: string | null;
  score: number | null;
  reason: string;
  critical: boolean;
  evidence: Evidence[] | null;
  evaluator: string;
  evaluator_version: string;
  /** The expectation as the scenario declared it, parameters included (tool, path, ...). */
  expectation: { id: string; type: string; critical?: boolean; index?: number } & Record<string, unknown>;
}

export interface StateChange {
  op: "added" | "removed" | "changed";
  path: string;
  before?: unknown;
  after?: unknown;
}

export interface ToolCallRecord {
  seq: number;
  tool: string;
  risk: string | null;
  status: string;
  fault: string | null;
  http_status: number | null;
  error_code: string | null;
  arguments: Record<string, unknown> | null;
  response: unknown;
  changes: StateChange[] | null;
  mutated: boolean;
  replayed: boolean;
  redelivered: boolean;
  delay_ms: number;
  call_number: number;
  effect_key: string | null;
  expects_mutation: boolean;
  cross_tenant: string | null;
  policy_violation: string | null;
}

export interface RetrievalRecord {
  seq: number;
  kind: "retrieval";
  query: string;
  limit: number;
  documents: { id: string; trusted: boolean }[];
}

export interface CaseStep {
  seq: number;
  kind: "tool_call" | "retrieval" | string;
  tool: string | null;
  latency_ms: number | null;
  created_at: string;
  record: Partial<ToolCallRecord> & Partial<RetrievalRecord> & Record<string, unknown>;
}

export interface AgentResult {
  kind?: string;
  status?: string;
  output?: string;
  claimed_outcome?: string | null;
  business_outcome?: string | null;
  steps?: number;
  model?: string;
  http_status?: number | null;
  elapsed_ms?: number;
  error?: string | null;
  trace_id?: string | null;
}

export interface Verdict {
  status: CaseStatus;
  reason: string;
  /** Share of the evaluated expectations that passed; null when none was evaluated. */
  score: number | null;
  labels: string[];
  passed: number;
  failed: number;
  errored: number;
  skipped: number;
  critical_failures: number;
}

export interface ScenarioFault {
  target: string;
  when?: Record<string, unknown>;
  behavior: { type: string } & Record<string, unknown>;
}

export interface CaseDetail {
  case: SimulationCase & {
    verdict: Verdict | null;
    results: ExpectationResult[];
    state_diff: StateChange[];
    agent_result: AgentResult | null;
  };
  scenario: { document: ScenarioDocument; faults: ScenarioFault[] };
  twin: { id: string; name: string; version: number; spec_hash: string } | null;
  steps: CaseStep[];
  state: { initial: Record<string, unknown> | null; final: Record<string, unknown> | null };
}

export interface SimulationCapabilities {
  agents: string[];
  fault_types: string[];
  expectation_types: string[];
  evaluators: Record<string, string>;
  engine: string;
  limits: Record<string, number>;
}

// ------------------------------------------------------------------ scenarios

export interface ScenarioDocument {
  apiVersion: string;
  kind: string;
  metadata: {
    name: string;
    description?: string;
    severity: Severity;
    tags?: string[];
    owner?: string;
  } & Record<string, unknown>;
  spec: {
    agent?: string;
    twin?: string;
    seed?: number;
    covers?: string[];
    input?: {
      message?: string;
      context?: Record<string, unknown>;
      documents?: Record<string, unknown>[];
    };
    state?: Record<string, unknown>;
    faults?: ScenarioFault[];
    expectations?: ({ id?: string; type: string; critical?: boolean } & Record<string, unknown>)[];
  } & Record<string, unknown>;
}

export interface Scenario {
  id: string;
  organization_id: string;
  project_id: string;
  name: string;
  agent: string | null;
  twin: string | null;
  severity: Severity;
  tags: string[];
  source: string;
  latest_version: number;
  archived: boolean;
  created_by: string;
  created_at: string;
  updated_at: string;
  version_id?: string;
  spec_hash?: string;
  description?: string | null;
  expectation_count?: number;
  fault_count?: number;
}

export interface ScenarioPage {
  items: Scenario[];
  next_cursor: string | null;
}

export interface ScenarioVersion {
  id: string;
  version: number;
  spec_hash: string;
  created_by: string;
  created_at: string;
}

export interface ScenarioDetail {
  scenario: Scenario;
  version: number;
  version_created_by: string;
  version_created_at: string;
  document: ScenarioDocument;
  yaml: string;
  versions: ScenarioVersion[];
}

export interface ScenarioValidation {
  valid: boolean;
  problems: string[];
  warnings: string[];
  spec_hash: string | null;
  twin: { id: string; name: string; version: number; tool_count?: number } | null;
}

export interface ScenarioSaved {
  scenario: Scenario;
  version: number;
  created: boolean;
  warnings: string[];
}

export interface TwinSummary {
  id: string;
  project_id: string;
  name: string;
  version: number;
  spec_hash: string;
  tool_count: number;
  created_at: string;
}

export interface TwinList {
  items: TwinSummary[];
}

export interface TwinTool {
  name: string;
  risk: string;
}

export interface TwinDetail {
  twin: TwinSummary & { tools: TwinTool[] };
}
