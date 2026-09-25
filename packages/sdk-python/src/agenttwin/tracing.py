"""Agent tracing on top of OpenTelemetry.

Design goals (spec §13):

* the instrumented application never waits for the network: spans are queued
  in a bounded buffer and exported by a background thread in batches;
* a telemetry outage never raises into the host agent: exporter failures are
  counted, not propagated, and instrumentation helpers never throw;
* content capture is off by default and redacted client-side before export;
* every span of an agent run carries the run context (agent, version,
  environment, source, session, simulation run), so one process can serve
  several agents, versions and run sources at once.

The SDK owns a private TracerProvider and never replaces the application's
global OpenTelemetry provider unless ``install_global=True``.
"""

from __future__ import annotations

import contextvars
import functools
import inspect
import json
import logging
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Literal, ParamSpec, TypeVar, cast

from opentelemetry import context as otel_context
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace import Span as SDKSpan
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace import SpanKind, Status, StatusCode

from agenttwin import _attributes as A
from agenttwin._version import __version__
from agenttwin.config import Config, ContentMode, Source
from agenttwin.hashing import canonical_json, sha256_hex
from agenttwin.redaction import SECRET_RULES, Redactor, Rule, truncate

__all__ = [
    "AgentRun",
    "AgentTwin",
    "ExportStats",
    "ModelCall",
    "Retrieval",
    "ToolCall",
    "ToolResultStatus",
]

ToolResultStatus = Literal["ok", "error", "timeout", "rate_limited", "denied", "invalid"]
PolicyDecision = Literal["allow", "allow_with_limits", "require_approval", "deny"]
OutcomeStatus = Literal["SUCCESS", "PARTIAL", "FAILURE", "UNKNOWN"]
VerificationSource = Literal[
    "state_assertion",
    "tool_twin_state",
    "tool_result",
    "external_callback",
    "human_review",
    "semantic_judge",
    "unavailable",
]
OUTCOME_STATUSES: frozenset[str] = frozenset({"SUCCESS", "PARTIAL", "FAILURE", "UNKNOWN"})
VERIFICATION_SOURCES: frozenset[str] = frozenset(
    {
        "state_assertion",
        "tool_twin_state",
        "tool_result",
        "external_callback",
        "human_review",
        "semantic_judge",
        "unavailable",
    }
)

_TOOL_RESULT_STATUSES = {"ok", "error", "timeout", "rate_limited", "denied", "invalid"}
# Tool risk tiers (spec §81); unknown tools are treated conservatively server-side.
_RISK_LEVELS = {"READ", "WRITE_REVERSIBLE", "WRITE_IRREVERSIBLE", "EXECUTE", "ADMIN"}

P = ParamSpec("P")
R = TypeVar("R")

_log = logging.getLogger("agenttwin")

_current_run: contextvars.ContextVar[AgentRun | None] = contextvars.ContextVar("agenttwin_run", default=None)


# ---------------------------------------------------------------- export plumbing


@dataclass
class ExportStats:
    """Counters for the export pipeline (spans)."""

    ended: int = 0
    exported: int = 0
    failed: int = 0

    @property
    def not_exported(self) -> int:
        """Spans that ended but were not (yet) exported: queued, dropped on a
        full queue, or failed."""
        return self.ended - self.exported


class _SafeExporter(SpanExporter):
    """Never lets an exporter exception escape into the SDK worker."""

    def __init__(self, inner: SpanExporter, stats: ExportStats, lock: threading.Lock) -> None:
        self._inner = inner
        self._stats = stats
        self._lock = lock

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        try:
            result = self._inner.export(spans)
        except Exception:  # noqa: BLE001 - telemetry must never crash the host
            result = SpanExportResult.FAILURE
        with self._lock:
            if result == SpanExportResult.SUCCESS:
                self._stats.exported += len(spans)
            else:
                self._stats.failed += len(spans)
        return result

    def shutdown(self) -> None:
        try:
            self._inner.shutdown()
        except Exception:  # noqa: BLE001 - telemetry must never raise into the host
            _log.debug("agenttwin: instrumentation error suppressed", exc_info=True)

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        try:
            return bool(self._inner.force_flush(timeout_millis))
        except Exception:  # noqa: BLE001
            return False


class _RunContextProcessor(SpanProcessor):
    """Copies the run context onto every span of a run and counts ended spans.

    The context is looked up by trace id, so spans started from worker
    threads or other tasks of the same trace are covered as well.
    """

    def __init__(self, stats: ExportStats, lock: threading.Lock) -> None:
        self._contexts: dict[int, dict[str, Any]] = {}
        self._lock = lock
        self._stats = stats

    def register(self, trace_id: int, attrs: dict[str, Any]) -> None:
        with self._lock:
            self._contexts[trace_id] = attrs

    def unregister(self, trace_id: int) -> None:
        with self._lock:
            self._contexts.pop(trace_id, None)

    def on_start(self, span: SDKSpan, parent_context: otel_context.Context | None = None) -> None:
        trace_id = span.get_span_context().trace_id
        with self._lock:
            attrs = self._contexts.get(trace_id)
        if not attrs:
            return
        existing = span.attributes or {}
        for k, v in attrs.items():
            if k not in existing:
                span.set_attribute(k, v)

    def on_end(self, span: ReadableSpan) -> None:
        with self._lock:
            self._stats.ended += 1

    def shutdown(self) -> None:
        with self._lock:
            self._contexts.clear()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


# ---------------------------------------------------------------- content policy


class _Content:
    """Decides what content may leave the process (ADR-0008)."""

    def __init__(self, config: Config) -> None:
        self.mode: ContentMode = config.content_mode
        self.max_bytes = config.max_content_bytes
        red = config.redaction
        if self.mode == "redacted":
            self.redactor: Redactor | None = red.redactor()
        elif self.mode == "full":
            import re

            rules: list[Rule] = list(SECRET_RULES)
            rules.extend(Rule("custom", re.compile(p)) for p in red.custom_patterns)
            self.redactor = Redactor(rules, red.strategy, json_paths=red.json_paths)
        else:
            self.redactor = None

    @property
    def redacted(self) -> bool:
        return self.mode == "redacted"

    def text(self, s: str | None) -> str | None:
        if self.redactor is None or s is None:
            return None
        out, _ = self.redactor.text(str(s))
        if out is None:
            return None
        return truncate(out, self.max_bytes)[0]

    def value(self, v: Any) -> str | None:
        if self.redactor is None or v is None:
            return None
        try:
            red, _ = self.redactor.value(_jsonable(v))
            if red is None:
                return None
            encoded = json.dumps(red, ensure_ascii=False, separators=(",", ":"), default=str)
        except Exception:  # noqa: BLE001 - never fail the host on odd values
            return None
        return truncate(encoded, self.max_bytes)[0]


def _jsonable(v: Any) -> Any:
    """Best-effort conversion to plain JSON types."""
    try:
        return json.loads(json.dumps(v, default=str))
    except (TypeError, ValueError):
        return repr(v)


def args_hash(args: Any) -> str:
    """SHA-256 of the canonical JSON of tool arguments (never raises)."""
    try:
        return sha256_hex(canonical_json(_jsonable(args)))
    except (TypeError, ValueError):
        return sha256_hex(repr(args))


def idempotency_key_hash(key: str) -> str:
    """Short, non-reversible identity of an idempotency key."""
    return sha256_hex(key)[:16]


# ---------------------------------------------------------------- client


class AgentTwin:
    """A configured SDK instance.

    >>> at = AgentTwin(Config.from_env(content_mode="redacted"))
    >>> with at.agent_run("support-refund-agent", "1.3.0", input="refund ORD-1") as run:
    ...     with run.tool_call("lookup_order", args={"order_id": "ORD-1"}, risk="READ") as call:
    ...         call.set_result({"status": "delivered"})
    ...     run.outcome("SUCCESS", business_outcome="answered")
    """

    def __init__(
        self,
        config: Config | None = None,
        *,
        exporter: SpanExporter | None = None,
        install_global: bool = False,
    ) -> None:
        self.config = config or Config.from_env()
        self._content = _Content(self.config)
        self._lock = threading.Lock()
        self.stats = ExportStats()
        resource_attrs: dict[str, Any] = {
            A.SERVICE_NAME: self.config.service_name or "agenttwin-agent",
            A.DEPLOYMENT_ENVIRONMENT: self.config.environment,
            A.SDK_NAME: "agenttwin-python",
            A.SDK_VERSION: __version__,
            A.CONTENT_MODE: self.config.content_mode,
            A.CONTENT_REDACTED: self._content.redacted,
            A.SOURCE: self.config.source,
        }
        if self.config.project:
            resource_attrs[A.PROJECT] = self.config.project
        if self.config.release_id:
            resource_attrs[A.RELEASE_ID] = self.config.release_id
        if self.config.commit_sha:
            resource_attrs[A.COMMIT_SHA] = self.config.commit_sha
        self._provider = TracerProvider(
            resource=Resource.create(resource_attrs),
            sampler=ParentBased(TraceIdRatioBased(self.config.sample_ratio)),
        )
        self._run_ctx = _RunContextProcessor(self.stats, self._lock)
        self._provider.add_span_processor(self._run_ctx)
        self._batch: BatchSpanProcessor | None = None
        if self.config.enabled:
            inner = exporter or self._otlp_exporter()
            self._batch = BatchSpanProcessor(
                _SafeExporter(inner, self.stats, self._lock),
                max_queue_size=self.config.max_queue_size,
                max_export_batch_size=min(self.config.max_export_batch_size, self.config.max_queue_size),
                schedule_delay_millis=self.config.schedule_delay_ms,
                export_timeout_millis=self.config.export_timeout_s * 1000,
            )
            self._provider.add_span_processor(self._batch)
        self._tracer = self._provider.get_tracer("agenttwin", __version__)
        if install_global:
            otel_trace.set_tracer_provider(self._provider)

    def _otlp_exporter(self) -> SpanExporter:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        headers = {"x-agenttwin-api-key": self.config.api_key} if self.config.api_key else {}
        endpoint = self.config.otlp_endpoint.rstrip("/")
        if not endpoint.endswith("/v1/traces"):
            endpoint += "/v1/traces"
        return cast(
            SpanExporter,
            OTLPSpanExporter(endpoint=endpoint, headers=headers, timeout=self.config.export_timeout_s),
        )

    @property
    def tracer(self) -> otel_trace.Tracer:
        return self._tracer

    def agent_run(
        self,
        agent: str,
        version: str | None = None,
        *,
        input: str | None = None,
        input_context: Mapping[str, Any] | None = None,
        session_id: str | None = None,
        environment: str | None = None,
        source: Source | None = None,
        release_id: str | None = None,
        commit_sha: str | None = None,
        simulation_run_id: str | None = None,
        scenario_id: str | None = None,
        agent_id: str | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> AgentRun:
        """Start the root span of one agent run (use as a context manager).

        ``input_context`` is the request context the run acts in (tenant,
        customer, ...): content, recorded only when the content mode allows
        and redacted like the input. A production failure's scenario draft
        replays it (ADR-0032)."""
        ctx: dict[str, Any] = {
            A.AGENT_NAME: agent,
            A.ENVIRONMENT: environment or self.config.environment,
            A.SOURCE: source or self.config.source,
        }
        for key, value in (
            (A.AGENT_VERSION, version),
            (A.SESSION_ID, session_id),
            (A.RELEASE_ID, release_id or self.config.release_id),
            (A.COMMIT_SHA, commit_sha or self.config.commit_sha),
            (A.SIMULATION_RUN_ID, simulation_run_id),
            (A.SCENARIO_ID, scenario_id),
            (A.AGENT_ID, agent_id),
        ):
            if value:
                ctx[key] = value
        return AgentRun(self, agent, ctx, input=input, input_context=input_context, attributes=attributes)

    def flush(self, timeout_s: float = 10.0) -> bool:
        """Export everything queued so far (blocks; use in tests/shutdown)."""
        if self._batch is None:
            return True
        try:
            return bool(self._batch.force_flush(int(timeout_s * 1000)))
        except Exception:  # noqa: BLE001
            return False

    def shutdown(self) -> None:
        """Flush and stop the exporter thread. Idempotent."""
        try:
            self._provider.shutdown()
        except Exception:  # noqa: BLE001 - telemetry must never raise into the host
            _log.debug("agenttwin: instrumentation error suppressed", exc_info=True)


# ---------------------------------------------------------------- spans


class _SpanScope:
    """Common context-manager behavior: current-span activation, exception
    capture, and safe attribute setting."""

    def __init__(self, span: otel_trace.Span) -> None:
        self.span = span
        self._token: object | None = None
        self._status_set = False

    def _activate(self) -> None:
        self._token = otel_context.attach(otel_trace.set_span_in_context(self.span))

    def _deactivate(self) -> None:
        if self._token is not None:
            otel_context.detach(self._token)  # type: ignore[arg-type]
            self._token = None

    def set_attribute(self, key: str, value: Any) -> None:
        if value is None:
            return
        try:
            self.span.set_attribute(key, value)
        except Exception:  # noqa: BLE001 - telemetry must never raise into the host
            _log.debug("agenttwin: instrumentation error suppressed", exc_info=True)

    def _fail(self, error_type: str, message: str | None = None) -> None:
        self.set_attribute(A.ERROR_TYPE, error_type)
        self.span.set_status(Status(StatusCode.ERROR, message))
        self._status_set = True

    def _finish(self, exc: BaseException | None) -> None:
        if exc is not None and not self._status_set:
            self.span.record_exception(exc)
            self._fail(type(exc).__name__, None)
        elif not self._status_set:
            self.span.set_status(Status(StatusCode.OK))
        self.span.end()

    @property
    def trace_id(self) -> str:
        return format(self.span.get_span_context().trace_id, "032x")

    @property
    def span_id(self) -> str:
        return format(self.span.get_span_context().span_id, "016x")

    @property
    def traceparent(self) -> str | None:
        """The W3C ``traceparent`` of this span, to pass to a service that
        joins the trace (the runtime gateway records its decision on it);
        ``None`` when tracing is not recording."""
        ctx = self.span.get_span_context()
        if not ctx.is_valid:
            return None
        return f"00-{ctx.trace_id:032x}-{ctx.span_id:016x}-{int(ctx.trace_flags):02x}"


class AgentRun(_SpanScope):
    """The root span of one agent run."""

    def __init__(
        self,
        client: AgentTwin,
        agent: str,
        ctx: dict[str, Any],
        *,
        input: str | None,
        attributes: Mapping[str, Any] | None,
        input_context: Mapping[str, Any] | None = None,
    ) -> None:
        self.client = client
        self.agent = agent
        self._ctx = ctx
        self._input = input
        self._input_context = input_context
        self._attributes = attributes
        self._run_token: contextvars.Token[AgentRun | None] | None = None
        self._outcome_recorded = False
        # The span is created on __enter__.
        super().__init__(otel_trace.INVALID_SPAN)

    def __enter__(self) -> AgentRun:
        attrs: dict[str, Any] = dict(self._ctx)
        attrs.update(
            {
                A.SPAN_KIND: "agent",
                A.GENAI_OPERATION: "invoke_agent",
                A.GENAI_AGENT_NAME: self.agent,
            }
        )
        if A.SESSION_ID in self._ctx:
            attrs[A.GENAI_CONVERSATION_ID] = self._ctx[A.SESSION_ID]
        if self._attributes:
            attrs.update({k: v for k, v in self._attributes.items() if v is not None})
        self.span = self.client.tracer.start_span(
            f"invoke_agent {self.agent}", kind=SpanKind.INTERNAL, attributes=attrs
        )
        sc = self.span.get_span_context()
        if sc.is_valid:
            self.client._run_ctx.register(sc.trace_id, dict(self._ctx))
        self.set_attribute(A.INPUT, self.client._content.text(self._input))
        if self._input_context:
            self.set_attribute(A.INPUT_CONTEXT, self.client._content.value(dict(self._input_context)))
        self._activate()
        self._run_token = _current_run.set(self)
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        try:
            self._finish(exc)
        finally:
            if self._run_token is not None:
                _current_run.reset(self._run_token)
            self._deactivate()
            sc = self.span.get_span_context()
            self.client._run_ctx.unregister(sc.trace_id)

    # -- child spans --------------------------------------------------------

    def model_call(
        self,
        provider: str,
        model: str,
        *,
        input_messages: Sequence[Mapping[str, Any]] | None = None,
        system_instructions: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        prompt_hash: str | None = None,
        prompt_version: str | None = None,
    ) -> ModelCall:
        return ModelCall(
            self.client,
            provider,
            model,
            input_messages=input_messages,
            system_instructions=system_instructions,
            temperature=temperature,
            max_tokens=max_tokens,
            prompt_hash=prompt_hash,
            prompt_version=prompt_version,
        )

    def tool_call(
        self,
        name: str,
        *,
        args: Mapping[str, Any] | None = None,
        risk: str | None = None,
        version: str | None = None,
        idempotency_key: str | None = None,
        attempt: int | None = None,
        call_id: str | None = None,
    ) -> ToolCall:
        return ToolCall(
            self.client,
            name,
            args=args,
            risk=risk,
            version=version,
            idempotency_key=idempotency_key,
            attempt=attempt,
            call_id=call_id,
        )

    def retrieval(self, source: str, *, query: str | None = None) -> Retrieval:
        return Retrieval(self.client, source, query=query)

    def policy_decision(
        self,
        decision: PolicyDecision,
        *,
        policy: str | None = None,
        version: str | None = None,
        rule: str | None = None,
        tool: str | None = None,
        reason: str | None = None,
        decision_id: str | None = None,
        approval_id: str | None = None,
    ) -> None:
        """Record a policy decision taken for this run (point-in-time span,
        a child of the current span: inside a tool call, of that call).
        ``decision_id`` and ``approval_id`` link it to the runtime gateway's
        record."""
        try:
            attrs: dict[str, Any] = {A.SPAN_KIND: "policy", A.POLICY_DECISION: decision}
            for key, value in (
                (A.POLICY_NAME, policy),
                (A.POLICY_VERSION, version),
                (A.POLICY_RULE, rule),
                (A.GENAI_TOOL_NAME, tool),
                (A.POLICY_DECISION_ID, decision_id),
                (A.POLICY_APPROVAL_ID, approval_id),
            ):
                if value:
                    attrs[key] = value
            span = self.client.tracer.start_span("policy.decision", attributes=attrs)
            text = self.client._content.text(reason)
            if text:
                span.set_attribute(A.OUTPUT, text)
            span.set_status(Status(StatusCode.OK))
            span.end()
        except Exception:  # noqa: BLE001 - telemetry must never raise into the host
            _log.debug("agenttwin: instrumentation error suppressed", exc_info=True)

    def outcome(
        self,
        status: OutcomeStatus,
        *,
        business_outcome: str | None = None,
        claimed: OutcomeStatus | None = None,
        verified: bool = False,
        verification_source: VerificationSource = "unavailable",
        state_diff: Mapping[str, Any] | None = None,
    ) -> None:
        """Report the run's outcome.

        An agent's own final answer is not verification (spec §15): leave
        ``verification_source="unavailable"`` unless an independent check
        (state assertion, tool result, callback...) backs the status.
        """
        if status not in OUTCOME_STATUSES or (claimed is not None and claimed not in OUTCOME_STATUSES):
            raise ValueError(f"outcome status must be one of {sorted(OUTCOME_STATUSES)}")
        if verification_source not in VERIFICATION_SOURCES:
            raise ValueError(f"verification_source must be one of {sorted(VERIFICATION_SOURCES)}")
        if verified and verification_source == "unavailable":
            raise ValueError("an outcome cannot be verified when verification is unavailable")
        try:
            attrs: dict[str, Any] = {
                A.SPAN_KIND: "outcome",
                A.OUTCOME_STATUS: status,
                A.OUTCOME_VERIFIED: verified,
                A.OUTCOME_VERIFICATION_SOURCE: verification_source,
            }
            if business_outcome:
                attrs[A.OUTCOME_BUSINESS] = business_outcome
            if claimed:
                attrs[A.OUTCOME_CLAIMED] = claimed
            if state_diff:
                attrs[A.STATE_DIFF] = canonical_json(_jsonable(state_diff))
            span = self.client.tracer.start_span("outcome.verify", attributes=attrs)
            span.set_status(Status(StatusCode.OK))
            span.end()
            self._outcome_recorded = True
        except Exception:  # noqa: BLE001 - telemetry must never raise into the host
            _log.debug("agenttwin: instrumentation error suppressed", exc_info=True)

    def set_output(self, text: str | None) -> None:
        self.set_attribute(A.OUTPUT, self.client._content.text(text))

    def set_error(self, error_type: str, message: str | None = None) -> None:
        """Mark the run failed without raising (e.g. step budget exhausted)."""
        self._fail(error_type, message)


class ModelCall(_SpanScope):
    """A model (LLM) call inside a run."""

    def __init__(
        self,
        client: AgentTwin,
        provider: str,
        model: str,
        *,
        input_messages: Sequence[Mapping[str, Any]] | None,
        system_instructions: str | None,
        temperature: float | None,
        max_tokens: int | None,
        prompt_hash: str | None,
        prompt_version: str | None,
    ) -> None:
        self.client = client
        attrs: dict[str, Any] = {
            A.SPAN_KIND: "model",
            A.GENAI_OPERATION: "chat",
            A.GENAI_PROVIDER: provider,
            A.GENAI_REQUEST_MODEL: model,
        }
        if temperature is not None:
            attrs[A.GENAI_REQUEST_TEMPERATURE] = float(temperature)
        if max_tokens is not None:
            attrs[A.GENAI_REQUEST_MAX_TOKENS] = int(max_tokens)
        if prompt_hash:
            attrs[A.PROMPT_HASH] = prompt_hash
        if prompt_version:
            attrs[A.PROMPT_VERSION] = prompt_version
        super().__init__(client.tracer.start_span(f"chat {model}", kind=SpanKind.CLIENT, attributes=attrs))
        content = client._content
        self.set_attribute(
            A.GENAI_INPUT_MESSAGES, content.value(list(input_messages)) if input_messages else None
        )
        self.set_attribute(A.GENAI_SYSTEM_INSTRUCTIONS, content.text(system_instructions))

    def __enter__(self) -> ModelCall:
        self._activate()
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        try:
            self._finish(exc)
        finally:
            self._deactivate()

    def record_response(
        self,
        *,
        output_messages: Sequence[Mapping[str, Any]] | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        finish_reasons: Sequence[str] | None = None,
        response_model: str | None = None,
        cost_usd: float | None = None,
    ) -> None:
        if input_tokens is not None:
            self.set_attribute(A.GENAI_USAGE_INPUT_TOKENS, int(input_tokens))
        if output_tokens is not None:
            self.set_attribute(A.GENAI_USAGE_OUTPUT_TOKENS, int(output_tokens))
        if finish_reasons:
            self.set_attribute(A.GENAI_FINISH_REASONS, list(finish_reasons))
        if response_model:
            self.set_attribute(A.GENAI_RESPONSE_MODEL, response_model)
        if cost_usd is not None:
            self.set_attribute(A.COST_USD, float(cost_usd))
        if output_messages:
            self.set_attribute(A.GENAI_OUTPUT_MESSAGES, self.client._content.value(list(output_messages)))

    def set_error(self, error_type: str, message: str | None = None) -> None:
        self._fail(error_type, message)


class ToolCall(_SpanScope):
    """A tool call inside a run. The result status is one of ``ok``,
    ``error``, ``timeout``, ``rate_limited``, ``denied`` and ``invalid``."""

    def __init__(
        self,
        client: AgentTwin,
        name: str,
        *,
        args: Mapping[str, Any] | None,
        risk: str | None,
        version: str | None,
        idempotency_key: str | None,
        attempt: int | None,
        call_id: str | None,
    ) -> None:
        self.client = client
        self.name = name
        attrs: dict[str, Any] = {
            A.SPAN_KIND: "tool",
            A.GENAI_OPERATION: "execute_tool",
            A.GENAI_TOOL_NAME: name,
            A.TOOL_ARGS_HASH: args_hash(dict(args) if args is not None else {}),
        }
        if risk:
            level = risk.upper()
            if level not in _RISK_LEVELS:
                raise ValueError(f"unknown tool risk {risk!r}; expected one of {sorted(_RISK_LEVELS)}")
            attrs[A.TOOL_RISK] = level
        if version:
            attrs[A.TOOL_VERSION] = version
        if idempotency_key:
            attrs[A.TOOL_IDEMPOTENCY_KEY_HASH] = idempotency_key_hash(idempotency_key)
        if attempt is not None:
            attrs[A.TOOL_ATTEMPT] = int(attempt)
        if call_id:
            attrs[A.GENAI_TOOL_CALL_ID] = call_id
        super().__init__(
            client.tracer.start_span(f"execute_tool {name}", kind=SpanKind.INTERNAL, attributes=attrs)
        )
        self.set_attribute(
            A.GENAI_TOOL_ARGUMENTS, client._content.value(dict(args)) if args is not None else None
        )
        self._result_status: str | None = None

    def __enter__(self) -> ToolCall:
        self._activate()
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        try:
            if exc is not None and self._result_status is None:
                status = "timeout" if isinstance(exc, TimeoutError) else "error"
                self.set_attribute(A.TOOL_RESULT_STATUS, status)
            self._finish(exc)
        finally:
            self._deactivate()

    def set_result(self, result: Any = None, *, status: ToolResultStatus = "ok") -> None:
        """Record the tool result. Non-``ok`` statuses mark the span failed."""
        if status not in _TOOL_RESULT_STATUSES:
            raise ValueError(f"unknown tool result status {status!r}")
        self._result_status = status
        self.set_attribute(A.TOOL_RESULT_STATUS, status)
        if result is not None:
            self.set_attribute(A.GENAI_TOOL_RESULT, self.client._content.value(result))
        if status != "ok":
            self._fail(status)

    def set_error(
        self, status: ToolResultStatus, error_type: str | None = None, *, http_status: int | None = None
    ) -> None:
        """Record a failed call (``timeout``, ``rate_limited``, ``error``...)."""
        if status == "ok":
            raise ValueError("set_error requires a failure status")
        self._result_status = status
        self.set_attribute(A.TOOL_RESULT_STATUS, status)
        if http_status is not None:
            self.set_attribute(A.HTTP_STATUS, int(http_status))
        self._fail(error_type or status)


class Retrieval(_SpanScope):
    """A retrieval step (knowledge base, search index...)."""

    def __init__(self, client: AgentTwin, source: str, *, query: str | None) -> None:
        self.client = client
        attrs = {A.SPAN_KIND: "retrieval", A.RETRIEVAL_SOURCE: source}
        super().__init__(client.tracer.start_span(f"retrieval {source}", attributes=attrs))
        self.set_attribute(A.INPUT, client._content.text(query))

    def __enter__(self) -> Retrieval:
        self._activate()
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        try:
            self._finish(exc)
        finally:
            self._deactivate()

    def set_documents(self, documents: Sequence[Any]) -> None:
        self.set_attribute(A.RETRIEVAL_DOCUMENT_COUNT, len(documents))
        self.set_attribute(A.OUTPUT, self.client._content.value(list(documents)))


# ---------------------------------------------------------------- module-level API


_default_client: AgentTwin | None = None
_default_lock = threading.Lock()


def configure(config: Config | None = None, **overrides: Any) -> AgentTwin:
    """Configure the process-wide default client (idempotent replacement)."""
    global _default_client
    with _default_lock:
        if _default_client is not None:
            _default_client.shutdown()
        _default_client = AgentTwin(config or Config.from_env(**overrides))
        return _default_client


def default_client() -> AgentTwin:
    global _default_client
    with _default_lock:
        if _default_client is None:
            _default_client = AgentTwin(Config.from_env())
        return _default_client


def current_run() -> AgentRun | None:
    """The agent run active in this context, if any."""
    return _current_run.get()


def AgentTrace(
    agent: str,
    version: str | None = None,
    *,
    project: str | None = None,
    **kwargs: Any,
) -> AgentRun:
    """``with AgentTrace(agent="refund-agent", version="1.3.0") as trace:``

    Uses the default client; ``project`` is informational (the API key
    determines the project server-side).
    """
    client = default_client()
    attrs = dict(kwargs.pop("attributes", None) or {})
    if project:
        attrs[A.PROJECT] = project
    return client.agent_run(agent, version, attributes=attrs, **kwargs)


def tool_span(
    name: str | None = None,
    *,
    risk: str | None = None,
    version: str | None = None,
    idempotency_key_arg: str | None = "idempotency_key",
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Decorate a tool function so every call is traced as a tool span.

    Arguments are bound by name for hashing and (policy permitting) capture;
    exceptions are recorded and re-raised; ``TimeoutError`` becomes a
    ``timeout`` result. Works for sync and async functions.
    """

    def decorate(fn: Callable[P, R]) -> Callable[P, R]:
        tool_name = name or fn.__name__
        sig = inspect.signature(fn)

        def bound_args(*args: Any, **kwargs: Any) -> dict[str, Any]:
            try:
                b = sig.bind_partial(*args, **kwargs)
                b.apply_defaults()
                return {k: v for k, v in b.arguments.items() if k not in ("self", "cls")}
            except TypeError:
                return {"args": list(args), "kwargs": kwargs}

        def open_call(arguments: dict[str, Any]) -> ToolCall:
            key = arguments.get(idempotency_key_arg) if idempotency_key_arg else None
            return ToolCall(
                default_client(),
                tool_name,
                args=arguments,
                risk=risk,
                version=version,
                idempotency_key=key if isinstance(key, str) else None,
                attempt=None,
                call_id=None,
            )

        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                with open_call(bound_args(*args, **kwargs)) as call:
                    result = await fn(*args, **kwargs)
                    call.set_result(result)
                    return result

            return cast(Callable[P, R], async_wrapper)

        @functools.wraps(fn)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            with open_call(bound_args(*args, **kwargs)) as call:
                result = fn(*args, **kwargs)
                call.set_result(result)
                return result

        return wrapper

    return decorate


def outcome(status: OutcomeStatus, **kwargs: Any) -> None:
    """Record the outcome of the current agent run (no-op outside a run)."""
    run = current_run()
    if run is not None:
        run.outcome(status, **kwargs)


@contextmanager
def timed() -> Iterator[Callable[[], float]]:
    """Tiny helper returning elapsed milliseconds (used by benchmarks)."""
    start = time.perf_counter()
    yield lambda: (time.perf_counter() - start) * 1000
