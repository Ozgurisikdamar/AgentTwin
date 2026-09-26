/**
 * Attribute names emitted by the SDK: the same keys as the Python SDK
 * (`agenttwin/_attributes.py`). They follow the OpenTelemetry GenAI semantic
 * conventions (v1.37 generation) where one exists and the `agenttwin.*`
 * namespace otherwise.
 */

// Resource
export const SERVICE_NAME = "service.name";
export const SERVICE_VERSION = "service.version";
export const DEPLOYMENT_ENVIRONMENT = "deployment.environment.name";
export const SDK_NAME = "agenttwin.sdk.name";
export const SDK_VERSION = "agenttwin.sdk.version";
export const PROJECT = "agenttwin.project";
export const CONTENT_MODE = "agenttwin.content.mode";
export const CONTENT_REDACTED = "agenttwin.content.redacted";

// Trace-scoped context (copied onto every span of a run)
export const ENVIRONMENT = "agenttwin.environment";
export const SOURCE = "agenttwin.source";
export const AGENT_NAME = "agenttwin.agent.name";
export const AGENT_VERSION = "agenttwin.agent.version";
export const AGENT_ID = "agenttwin.agent.id";
export const SESSION_ID = "agenttwin.session.id";
export const RELEASE_ID = "agenttwin.release.id";
export const COMMIT_SHA = "agenttwin.commit.sha";
export const SIMULATION_RUN_ID = "agenttwin.simulation.run_id";
export const SCENARIO_ID = "agenttwin.scenario.id";
export const SPAN_KIND = "agenttwin.span.kind";

// GenAI semantic conventions (v1.37 generation)
export const GENAI_OPERATION = "gen_ai.operation.name";
export const GENAI_PROVIDER = "gen_ai.provider.name";
export const GENAI_AGENT_NAME = "gen_ai.agent.name";
export const GENAI_CONVERSATION_ID = "gen_ai.conversation.id";
export const GENAI_REQUEST_MODEL = "gen_ai.request.model";
export const GENAI_RESPONSE_MODEL = "gen_ai.response.model";
export const GENAI_REQUEST_TEMPERATURE = "gen_ai.request.temperature";
export const GENAI_REQUEST_MAX_TOKENS = "gen_ai.request.max_tokens";
export const GENAI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens";
export const GENAI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens";
export const GENAI_FINISH_REASONS = "gen_ai.response.finish_reasons";
export const GENAI_INPUT_MESSAGES = "gen_ai.input.messages";
export const GENAI_OUTPUT_MESSAGES = "gen_ai.output.messages";
export const GENAI_SYSTEM_INSTRUCTIONS = "gen_ai.system_instructions";
export const GENAI_TOOL_NAME = "gen_ai.tool.name";
export const GENAI_TOOL_CALL_ID = "gen_ai.tool.call.id";
export const GENAI_TOOL_ARGUMENTS = "gen_ai.tool.call.arguments";
export const GENAI_TOOL_RESULT = "gen_ai.tool.call.result";

// Model call extras
export const COST_USD = "agenttwin.cost.usd";
export const PROMPT_HASH = "agenttwin.prompt.hash";
export const PROMPT_VERSION = "agenttwin.prompt.version";

// Tool call
export const TOOL_VERSION = "agenttwin.tool.version";
export const TOOL_RISK = "agenttwin.tool.risk";
export const TOOL_ARGS_HASH = "agenttwin.tool.args_hash";
export const TOOL_RESULT_STATUS = "agenttwin.tool.result_status";
export const TOOL_IDEMPOTENCY_KEY_HASH = "agenttwin.tool.idempotency_key_hash";
export const TOOL_ATTEMPT = "agenttwin.tool.attempt";

// Retrieval
export const RETRIEVAL_SOURCE = "agenttwin.retrieval.source";
export const RETRIEVAL_DOCUMENT_COUNT = "agenttwin.retrieval.document_count";

// Agent input/output content
export const INPUT = "agenttwin.input";
/** The request context the run acted in (tenant, customer, ...), as JSON. */
/** Content: governed by the content mode and redacted like the input. */
export const INPUT_CONTEXT = "agenttwin.input.context";
export const OUTPUT = "agenttwin.output";

// Errors
export const ERROR_TYPE = "error.type";

// Policy decisions
export const POLICY_DECISION = "agenttwin.policy.decision";
export const POLICY_NAME = "agenttwin.policy.name";
export const POLICY_VERSION = "agenttwin.policy.version";
export const POLICY_RULE = "agenttwin.policy.rule";
/** The runtime gateway's record of the decision (ADR-0033). */
export const POLICY_DECISION_ID = "agenttwin.policy.decision_id";
/** The approval request a require_approval decision opened or used. */
export const POLICY_APPROVAL_ID = "agenttwin.policy.approval_id";

// Outcome
export const OUTCOME_STATUS = "agenttwin.outcome.status";
export const OUTCOME_CLAIMED = "agenttwin.outcome.claimed";
export const OUTCOME_BUSINESS = "agenttwin.outcome.business";
export const OUTCOME_VERIFIED = "agenttwin.outcome.verified";
export const OUTCOME_VERIFICATION_SOURCE = "agenttwin.outcome.verification_source";
export const STATE_DIFF = "agenttwin.state.diff";

// HTTP client spans
export const HTTP_METHOD = "http.request.method";
export const HTTP_STATUS = "http.response.status_code";
export const SERVER_ADDRESS = "server.address";

// OpenTelemetry resource and exception conventions
export const TELEMETRY_SDK_NAME = "telemetry.sdk.name";
export const TELEMETRY_SDK_LANGUAGE = "telemetry.sdk.language";
export const TELEMETRY_SDK_VERSION = "telemetry.sdk.version";
export const EXCEPTION_TYPE = "exception.type";
export const EXCEPTION_MESSAGE = "exception.message";

/** Trace-scoped attributes copied onto every span of a run. */
export const TRACE_CONTEXT_KEYS: readonly string[] = [
  ENVIRONMENT,
  SOURCE,
  AGENT_NAME,
  AGENT_VERSION,
  AGENT_ID,
  SESSION_ID,
  RELEASE_ID,
  COMMIT_SHA,
  SIMULATION_RUN_ID,
  SCENARIO_ID,
];
