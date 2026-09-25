"""Attribute names emitted by the SDK.

This is the only module that knows raw attribute names. They follow the
OpenTelemetry GenAI semantic conventions (v1.37 generation) where one exists
and the ``agenttwin.*`` namespace otherwise; the trace-service maps both the
current and the legacy GenAI generation onto its internal schema.
"""

from __future__ import annotations

# Resource
SERVICE_NAME = "service.name"
SERVICE_VERSION = "service.version"
DEPLOYMENT_ENVIRONMENT = "deployment.environment.name"
SDK_NAME = "agenttwin.sdk.name"
SDK_VERSION = "agenttwin.sdk.version"
PROJECT = "agenttwin.project"
CONTENT_MODE = "agenttwin.content.mode"
CONTENT_REDACTED = "agenttwin.content.redacted"

# Trace-scoped context (copied onto every span of a run)
ENVIRONMENT = "agenttwin.environment"
SOURCE = "agenttwin.source"
AGENT_NAME = "agenttwin.agent.name"
AGENT_VERSION = "agenttwin.agent.version"
AGENT_ID = "agenttwin.agent.id"
SESSION_ID = "agenttwin.session.id"
RELEASE_ID = "agenttwin.release.id"
COMMIT_SHA = "agenttwin.commit.sha"
SIMULATION_RUN_ID = "agenttwin.simulation.run_id"
SCENARIO_ID = "agenttwin.scenario.id"

SPAN_KIND = "agenttwin.span.kind"

# GenAI semantic conventions (v1.37 generation)
GENAI_OPERATION = "gen_ai.operation.name"
GENAI_PROVIDER = "gen_ai.provider.name"
GENAI_AGENT_NAME = "gen_ai.agent.name"
GENAI_CONVERSATION_ID = "gen_ai.conversation.id"
GENAI_REQUEST_MODEL = "gen_ai.request.model"
GENAI_RESPONSE_MODEL = "gen_ai.response.model"
GENAI_REQUEST_TEMPERATURE = "gen_ai.request.temperature"
GENAI_REQUEST_MAX_TOKENS = "gen_ai.request.max_tokens"
GENAI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GENAI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
GENAI_FINISH_REASONS = "gen_ai.response.finish_reasons"
GENAI_INPUT_MESSAGES = "gen_ai.input.messages"
GENAI_OUTPUT_MESSAGES = "gen_ai.output.messages"
GENAI_SYSTEM_INSTRUCTIONS = "gen_ai.system_instructions"
GENAI_TOOL_NAME = "gen_ai.tool.name"
GENAI_TOOL_CALL_ID = "gen_ai.tool.call.id"
GENAI_TOOL_ARGUMENTS = "gen_ai.tool.call.arguments"
GENAI_TOOL_RESULT = "gen_ai.tool.call.result"

# Model call extras
COST_USD = "agenttwin.cost.usd"
PROMPT_HASH = "agenttwin.prompt.hash"
PROMPT_VERSION = "agenttwin.prompt.version"

# Tool call
TOOL_VERSION = "agenttwin.tool.version"
TOOL_RISK = "agenttwin.tool.risk"
TOOL_ARGS_HASH = "agenttwin.tool.args_hash"
TOOL_RESULT_STATUS = "agenttwin.tool.result_status"
TOOL_IDEMPOTENCY_KEY_HASH = "agenttwin.tool.idempotency_key_hash"
TOOL_ATTEMPT = "agenttwin.tool.attempt"

# Retrieval
RETRIEVAL_SOURCE = "agenttwin.retrieval.source"
RETRIEVAL_DOCUMENT_COUNT = "agenttwin.retrieval.document_count"

# Agent input/output content
INPUT = "agenttwin.input"
OUTPUT = "agenttwin.output"

# Errors
ERROR_TYPE = "error.type"

# Policy decisions
POLICY_DECISION = "agenttwin.policy.decision"
POLICY_NAME = "agenttwin.policy.name"
POLICY_VERSION = "agenttwin.policy.version"
POLICY_RULE = "agenttwin.policy.rule"

# Outcome
OUTCOME_STATUS = "agenttwin.outcome.status"
OUTCOME_CLAIMED = "agenttwin.outcome.claimed"
OUTCOME_BUSINESS = "agenttwin.outcome.business"
OUTCOME_VERIFIED = "agenttwin.outcome.verified"
OUTCOME_VERIFICATION_SOURCE = "agenttwin.outcome.verification_source"
STATE_DIFF = "agenttwin.state.diff"

# HTTP client spans
HTTP_METHOD = "http.request.method"
HTTP_STATUS = "http.response.status_code"
SERVER_ADDRESS = "server.address"

# Trace-scoped attributes the context processor copies onto every span.
TRACE_CONTEXT_KEYS = (
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
)
