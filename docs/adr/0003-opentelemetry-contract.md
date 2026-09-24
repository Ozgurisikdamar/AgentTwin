# ADR-0003 — OpenTelemetry as the telemetry contract

* Status: accepted · Date: 2026-09-24

## Context
Agents are built with many frameworks. AgentTwin must stay framework-neutral and
must not invent a proprietary logging protocol. The OTel GenAI semantic conventions
are still evolving (attribute renames such as `gen_ai.system` → `gen_ai.provider.name`,
`gen_ai.usage.prompt_tokens` → `gen_ai.usage.input_tokens`).

## Decision
* Transport: OTLP. The OpenTelemetry Collector handles protocol receiving (gRPC/HTTP,
  protobuf/JSON), batching and memory limits. The trace-service only accepts
  OTLP/HTTP **JSON** from the collector (or directly from SDKs in tests).
* Business logic never reads raw OTel attribute names. The trace-service normalizes
  spans through **versioned semconv mapping adapters** (`genai-v1.37` current,
  `genai-legacy` previous) into an internal schema, and stores the detected
  `semconv_version` with every trace. Both adapters have fixture tests.
* AgentTwin-specific facts use the `agenttwin.*` namespace
  (`agenttwin.tool.risk`, `agenttwin.policy.decision`, `agenttwin.outcome.status`, ...).

## Consequences
Traces can additionally be exported to Langfuse, Phoenix or any OTLP backend by
adding a collector exporter; AgentTwin's unique data (releases, simulations,
regressions, gates, policy evidence) does not depend on the tracing vendor.
