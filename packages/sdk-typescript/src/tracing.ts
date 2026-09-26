/**
 * Agent tracing.
 *
 * Design goals (spec §13), the same as the Python SDK's:
 *
 * - the instrumented application never waits for the network: ended spans
 *   go into a bounded queue and are exported in batches in the background;
 * - a telemetry outage never throws into the host agent: exporter failures
 *   are counted, not propagated, and instrumentation helpers never throw
 *   (except on programming errors such as an unknown risk level);
 * - content capture is off by default and redacted client-side before export;
 * - every span of an agent run carries the run context (agent, version,
 *   environment, source, session, simulation run), so one process can serve
 *   several agents, versions and run sources at once.
 *
 * The active span follows asynchronous work through AsyncLocalStorage: a
 * tool called anywhere inside `agentRun(..., async (run) => ...)` is a child
 * of that run, also across awaits, timers and promise chains.
 */

import { AsyncLocalStorage } from "node:async_hooks";
import { randomFillSync } from "node:crypto";

import * as A from "./attributes.js";
import { configFromEnv, makeConfig, type Config, type ConfigOptions, type Source } from "./config.js";
import { argsHash, ContentPolicy, idempotencyKeyHash, toJsonValue } from "./content.js";
import { BatchSpanProcessor, ExportStats, OTLPHttpExporter, type SpanExporter } from "./export.js";
import { canonicalJson } from "./hashing.js";
import {
  Double,
  SpanKind,
  StatusCode,
  type AttributeValue,
  type ReadableSpan,
  type Resource,
  type SpanEvent,
} from "./otlp.js";
import { Redactor, truncate } from "./redaction.js";
import { SDK_VERSION } from "./version.js";

export type RiskLevel = "READ" | "WRITE_REVERSIBLE" | "WRITE_IRREVERSIBLE" | "EXECUTE" | "ADMIN";
export type ToolResultStatus = "ok" | "error" | "timeout" | "rate_limited" | "denied" | "invalid";
export type PolicyDecision = "allow" | "allow_with_limits" | "require_approval" | "deny";
export type OutcomeStatus = "SUCCESS" | "PARTIAL" | "FAILURE" | "UNKNOWN";
export type VerificationSource =
  | "state_assertion"
  | "tool_twin_state"
  | "tool_result"
  | "external_callback"
  | "human_review"
  | "semantic_judge"
  | "unavailable";

export const RISK_LEVELS: readonly RiskLevel[] = [
  "READ",
  "WRITE_REVERSIBLE",
  "WRITE_IRREVERSIBLE",
  "EXECUTE",
  "ADMIN",
];
export const TOOL_RESULT_STATUSES: readonly ToolResultStatus[] = [
  "ok",
  "error",
  "timeout",
  "rate_limited",
  "denied",
  "invalid",
];
export const OUTCOME_STATUSES: readonly OutcomeStatus[] = ["SUCCESS", "PARTIAL", "FAILURE", "UNKNOWN"];
export const VERIFICATION_SOURCES: readonly VerificationSource[] = [
  "state_assertion",
  "tool_twin_state",
  "tool_result",
  "external_callback",
  "human_review",
  "semantic_judge",
  "unavailable",
];

/** Attribute limit per span (OpenTelemetry's default); the rest is counted as dropped. */
const MAX_ATTRIBUTES = 128;
const MAX_MESSAGE_BYTES = 512;

// ---------------------------------------------------------------- context, ids, clock

interface Active {
  readonly span: Span;
  readonly run: AgentRun | undefined;
}

const storage = new AsyncLocalStorage<Active>();

interface ParentContext {
  readonly traceId: string;
  readonly spanId: string;
  readonly sampled: boolean;
}

const ID_POOL = Buffer.allocUnsafe(4096);
let idPos = ID_POOL.length;

function randomHex(bytes: number): string {
  for (;;) {
    if (idPos + bytes > ID_POOL.length) {
      randomFillSync(ID_POOL);
      idPos = 0;
    }
    const hex = ID_POOL.toString("hex", idPos, idPos + bytes);
    idPos += bytes;
    if (!/^0+$/.test(hex)) return hex; // all-zero ids are invalid
  }
}

const EPOCH_NS = BigInt(Date.now()) * 1_000_000n;
const HR_ORIGIN = process.hrtime.bigint();

/** Wall-clock nanoseconds, monotonic within the process. */
function nowNs(): bigint {
  return EPOCH_NS + (process.hrtime.bigint() - HR_ORIGIN);
}

const TRACEPARENT = /^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})(?:-.*)?$/;

/** Parses a W3C `traceparent` header; undefined when it is not valid. */
export function parseTraceparent(header: string | undefined): ParentContext | undefined {
  const m = TRACEPARENT.exec((header ?? "").trim().toLowerCase());
  if (!m) return undefined;
  const [, version, traceId, spanId, flags] = m as unknown as [string, string, string, string, string];
  if (version === "ff" || /^0+$/.test(traceId) || /^0+$/.test(spanId)) return undefined;
  return { traceId, spanId, sampled: (parseInt(flags, 16) & 1) === 1 };
}

function errorName(err: unknown): string {
  if (err instanceof Error) return err.name && err.name !== "Error" ? err.name : err.constructor.name;
  if (typeof err === "object" && err !== null) return err.constructor?.name ?? "Object";
  return typeof err;
}

function errorMessage(err: unknown): string {
  if (err instanceof Error) return err.message;
  try {
    return String(err);
  } catch {
    return "";
  }
}

function isPromiseLike(v: unknown): v is PromiseLike<unknown> {
  return (
    (typeof v === "object" || typeof v === "function") &&
    v !== null &&
    typeof (v as { then?: unknown }).then === "function"
  );
}

// ---------------------------------------------------------------- client

export interface AgentTwinOptions {
  /** Where spans go instead of the OTLP/HTTP exporter (tests, custom sinks). */
  readonly exporter?: SpanExporter;
}

export interface AgentRunOptions {
  readonly agent: string;
  readonly version?: string;
  /** Informational: the API key determines the project server-side. */
  readonly project?: string;
  /** The user input; content, recorded only when the content mode allows (redacted). */
  readonly input?: string;
  /**
   * The request context the run acts in (tenant, customer, ...): content,
   * recorded only when the content mode allows and redacted like the input.
   * A production failure's scenario draft replays it (ADR-0032).
   */
  readonly inputContext?: Readonly<Record<string, unknown>>;
  readonly sessionId?: string;
  readonly environment?: string;
  readonly source?: Source;
  readonly releaseId?: string;
  readonly commitSha?: string;
  readonly simulationRunId?: string;
  readonly scenarioId?: string;
  readonly agentId?: string;
  readonly attributes?: Readonly<Record<string, AttributeValue | null | undefined>>;
  /** A W3C `traceparent` to join (a request from an upstream service). */
  readonly traceparent?: string;
}

/**
 * A configured SDK instance.
 *
 * ```ts
 * const at = new AgentTwin(configFromEnv({ contentMode: "redacted" }));
 * await at.agentRun({ agent: "support-refund-agent", version: "1.3.0", input: "refund ORD-1" }, async (run) => {
 *   await run.toolCall("lookup_order", { args: { order_id: "ORD-1" }, risk: "READ" }, () => lookupOrder("ORD-1"));
 *   run.outcome("SUCCESS", { businessOutcome: "answered" });
 * });
 * ```
 */
export class AgentTwin {
  readonly config: Config;
  readonly stats = new ExportStats();
  /** @internal */ readonly content: ContentPolicy;
  /** @internal */ readonly runContexts = new Map<string, Readonly<Record<string, AttributeValue>>>();
  /** @internal */ readonly exceptionRedactor: Redactor;
  readonly resource: Resource;
  private readonly processor: BatchSpanProcessor | undefined;
  private readonly sampleBound: bigint;
  private closed = false;

  constructor(config?: Config | ConfigOptions, options: AgentTwinOptions = {}) {
    this.config = config === undefined ? configFromEnv() : makeConfig(config);
    this.content = new ContentPolicy(this.config);
    // Status and exception messages: what the trace service keeps of them.
    this.exceptionRedactor = this.config.contentMode === "full" ? Redactor.secrets() : Redactor.all();
    const resource: Record<string, AttributeValue> = {
      [A.SERVICE_NAME]: this.config.serviceName ?? "agenttwin-agent",
      [A.DEPLOYMENT_ENVIRONMENT]: this.config.environment,
      [A.SDK_NAME]: "agenttwin-typescript",
      [A.SDK_VERSION]: SDK_VERSION,
      [A.TELEMETRY_SDK_NAME]: "agenttwin",
      [A.TELEMETRY_SDK_LANGUAGE]: "nodejs",
      [A.TELEMETRY_SDK_VERSION]: SDK_VERSION,
      [A.CONTENT_MODE]: this.config.contentMode,
      [A.CONTENT_REDACTED]: this.content.redacted,
      [A.SOURCE]: this.config.source,
    };
    if (this.config.project) resource[A.PROJECT] = this.config.project;
    if (this.config.releaseId) resource[A.RELEASE_ID] = this.config.releaseId;
    if (this.config.commitSha) resource[A.COMMIT_SHA] = this.config.commitSha;
    this.resource = { attributes: Object.freeze(resource) };
    const ratio = this.config.sampleRatio;
    this.sampleBound = BigInt(Math.round(ratio * 2 ** 64));
    if (this.config.enabled) {
      const exporter =
        options.exporter ??
        new OTLPHttpExporter({
          endpoint: this.config.otlpEndpoint,
          headers: this.config.apiKey ? { "x-agenttwin-api-key": this.config.apiKey } : {},
          timeoutMs: this.config.exportTimeoutMs,
          resource: this.resource,
          scope: { name: "agenttwin", version: SDK_VERSION },
        });
      this.processor = new BatchSpanProcessor(exporter, this.stats, this.config);
      LIVE.add(this);
      registerExitFlush();
    }
  }

  /**
   * Runs `fn` as one agent run: the root span of the trace, active for
   * everything `fn` calls. Returns what `fn` returns (a promise when `fn` is
   * async); an exception marks the run failed and is rethrown.
   */
  agentRun<T>(options: AgentRunOptions, fn: (run: AgentRun) => T): T {
    return runIn(this.startAgentRun(options), fn);
  }

  /** Starts an agent run you end yourself (`run.end()`), e.g. across callbacks. */
  startAgentRun(options: AgentRunOptions): AgentRun {
    const ctx: Record<string, AttributeValue> = {
      [A.AGENT_NAME]: options.agent,
      [A.ENVIRONMENT]: options.environment ?? this.config.environment,
      [A.SOURCE]: options.source ?? this.config.source,
    };
    const optional: [string, string | undefined][] = [
      [A.AGENT_VERSION, options.version],
      [A.SESSION_ID, options.sessionId],
      [A.RELEASE_ID, options.releaseId ?? this.config.releaseId],
      [A.COMMIT_SHA, options.commitSha ?? this.config.commitSha],
      [A.SIMULATION_RUN_ID, options.simulationRunId],
      [A.SCENARIO_ID, options.scenarioId],
      [A.AGENT_ID, options.agentId],
    ];
    for (const [key, value] of optional) if (value) ctx[key] = value;
    const attrs: Record<string, AttributeValue | null | undefined> = {
      ...ctx,
      [A.SPAN_KIND]: "agent",
      [A.GENAI_OPERATION]: "invoke_agent",
      [A.GENAI_AGENT_NAME]: options.agent,
    };
    if (options.sessionId) attrs[A.GENAI_CONVERSATION_ID] = options.sessionId;
    if (options.project) attrs[A.PROJECT] = options.project;
    Object.assign(attrs, options.attributes);
    const parent = options.traceparent === undefined ? undefined : parseTraceparent(options.traceparent);
    const run = new AgentRun(this, options.agent, `invoke_agent ${options.agent}`, attrs, parent, ctx);
    run.setAttribute(A.INPUT, this.content.text(options.input));
    if (options.inputContext && Object.keys(options.inputContext).length > 0) {
      run.setAttribute(A.INPUT_CONTEXT, this.content.value(options.inputContext));
    }
    return run;
  }

  /** Exports everything queued so far; resolves to false on timeout. */
  flush(timeoutMs = 10_000): Promise<boolean> {
    return this.processor?.flush(timeoutMs) ?? Promise.resolve(true);
  }

  /** Flushes and stops exporting; later spans are dropped. Idempotent. */
  async shutdown(timeoutMs = 10_000): Promise<void> {
    if (this.closed) return;
    this.closed = true;
    LIVE.delete(this);
    try {
      await this.processor?.shutdown(timeoutMs);
    } catch {}
  }

  /** @internal Resolves a new span's trace, parent and sampling decision. */
  context(parent: ParentContext | undefined): {
    traceId: string;
    parentSpanId: string | undefined;
    sampled: boolean;
  } {
    if (parent !== undefined)
      return { traceId: parent.traceId, parentSpanId: parent.spanId, sampled: parent.sampled };
    const traceId = randomHex(16);
    return { traceId, parentSpanId: undefined, sampled: this.sample(traceId) };
  }

  private sample(traceId: string): boolean {
    if (this.config.sampleRatio >= 1) return true;
    if (this.config.sampleRatio <= 0) return false;
    // OpenTelemetry's TraceIdRatioBased: the low 64 bits against the bound.
    return BigInt(`0x${traceId.slice(16)}`) < this.sampleBound;
  }

  /** @internal */
  onEnd(span: ReadableSpan): void {
    this.stats.ended++;
    this.processor?.onEnd(span);
  }
}

// Clients whose queued spans are flushed when the process is about to exit.
const LIVE = new Set<AgentTwin>();
let exitHookRegistered = false;

function registerExitFlush(): void {
  if (exitHookRegistered) return;
  exitHookRegistered = true;
  // beforeExit fires once the event loop is empty; the flush's requests keep
  // it alive until they finish (bounded by each client's export timeout).
  process.once("beforeExit", () => {
    for (const client of LIVE) void client.flush(client.config.exportTimeoutMs + 1000);
  });
}

// ---------------------------------------------------------------- spans

/** Runs `fn` with `span` active and ends the span when `fn` settles. */
function runIn<S extends Span, T>(
  span: S,
  fn: (span: S) => T,
  onSuccess?: (span: S, value: unknown) => void,
): T {
  let result: T;
  try {
    result = storage.run({ span, run: span instanceof AgentRun ? span : span.run }, () => fn(span));
  } catch (err) {
    span.end(err);
    throw err;
  }
  if (isPromiseLike(result)) {
    return Promise.resolve(result).then(
      (value) => {
        onSuccess?.(span, value);
        span.end();
        return value;
      },
      (err: unknown) => {
        span.end(err);
        throw err;
      },
    ) as T;
  }
  onSuccess?.(span, result);
  span.end();
  return result;
}

/** One unit of work in a trace. Every method is safe to call after `end()`. */
export class Span {
  readonly traceId: string;
  readonly spanId: string;
  readonly parentSpanId: string | undefined;
  readonly name: string;
  /** Whether the span is sampled: an unsampled span records nothing. */
  readonly recording: boolean;
  /** The agent run this span belongs to, if any. */
  readonly run: AgentRun | undefined;
  protected readonly client: AgentTwin;
  private readonly kind: SpanKind;
  private readonly startNs = nowNs();
  private readonly attributes: Record<string, AttributeValue> = {};
  private attributeCount = 0;
  private dropped = 0;
  private readonly events: SpanEvent[] = [];
  private status: { code: StatusCode; message?: string } = { code: StatusCode.UNSET };
  private statusSet = false;
  private ended = false;

  /** @internal */
  constructor(
    client: AgentTwin,
    name: string,
    kind: SpanKind,
    attributes: Readonly<Record<string, AttributeValue | null | undefined>>,
    parent: ParentContext | undefined,
    run: AgentRun | undefined,
  ) {
    this.client = client;
    this.name = name;
    this.kind = kind;
    this.run = run;
    const ctx = client.context(parent);
    this.traceId = ctx.traceId;
    this.parentSpanId = ctx.parentSpanId;
    this.recording = ctx.sampled;
    this.spanId = randomHex(8);
    for (const [k, v] of Object.entries(attributes)) this.setAttribute(k, v);
    // The run context of this trace, unless the span sets its own.
    const runContext = client.runContexts.get(this.traceId);
    if (runContext !== undefined) {
      for (const [k, v] of Object.entries(runContext)) if (!(k in this.attributes)) this.setAttribute(k, v);
    }
  }

  /** The W3C `traceparent` of this span, for a service that joins the trace
   * (the runtime gateway records its decision on it). */
  get traceparent(): string {
    return `00-${this.traceId}-${this.spanId}-${this.recording ? "01" : "00"}`;
  }

  /** @internal The parent context of a child span. */
  get parentContext(): ParentContext {
    return { traceId: this.traceId, spanId: this.spanId, sampled: this.recording };
  }

  /** Whether `end()` was called. */
  get isEnded(): boolean {
    return this.ended;
  }

  /** Sets an attribute; null and undefined are ignored. Never throws. */
  setAttribute(key: string, value: AttributeValue | null | undefined): void {
    if (value === null || value === undefined || !this.recording || this.ended) return;
    if (!(key in this.attributes)) {
      if (this.attributeCount >= MAX_ATTRIBUTES) {
        this.dropped++;
        return;
      }
      this.attributeCount++;
    }
    this.attributes[key] = value;
  }

  /** @internal */
  protected fail(errorType: string, message?: string): void {
    this.setAttribute(A.ERROR_TYPE, errorType);
    this.status =
      message === undefined
        ? { code: StatusCode.ERROR }
        : { code: StatusCode.ERROR, message: this.redactMessage(message) };
    this.statusSet = true;
  }

  private redactMessage(message: string): string {
    const text = this.client.exceptionRedactor.text(message).text ?? "";
    return truncate(text, MAX_MESSAGE_BYTES).text;
  }

  private recordException(err: unknown): void {
    if (!this.recording) return;
    this.events.push({
      name: "exception",
      timeUnixNano: nowNs(),
      attributes: {
        [A.EXCEPTION_TYPE]: errorName(err),
        [A.EXCEPTION_MESSAGE]: this.redactMessage(errorMessage(err)),
      },
    });
  }

  /**
   * Ends the span. With an error it records the exception and marks the span
   * failed (unless a status was set explicitly). Idempotent; never throws.
   */
  end(error?: unknown): void {
    if (this.ended) return;
    try {
      if (error !== undefined && !this.statusSet) {
        this.recordException(error);
        this.fail(errorName(error));
      } else if (!this.statusSet) {
        this.status = { code: StatusCode.OK };
      }
      this.onEnding();
    } catch {}
    this.ended = true;
    if (!this.recording) return;
    try {
      this.client.onEnd(
        Object.freeze({
          traceId: this.traceId,
          spanId: this.spanId,
          parentSpanId: this.parentSpanId,
          name: this.name,
          kind: this.kind,
          startTimeUnixNano: this.startNs,
          endTimeUnixNano: nowNs(),
          attributes: this.attributes,
          droppedAttributesCount: this.dropped,
          events: this.events,
          status: this.status,
        }),
      );
    } catch {}
  }

  /** @internal Hook for subclasses, before the span is handed to export. */
  protected onEnding(): void {}

  /** Ends the span (`using span = run.startToolCall(...)`). */
  [Symbol.dispose](): void {
    this.end();
  }
}

/** The parent of a new child of `owner`: the active span when it belongs to
 * the same trace (a tool inside a model call), else `owner` itself. */
function childParent(owner: Span): ParentContext {
  const active = storage.getStore()?.span;
  return active !== undefined && active.traceId === owner.traceId && !active.isEnded
    ? active.parentContext
    : owner.parentContext;
}

export interface ModelCallOptions {
  readonly inputMessages?: readonly Readonly<Record<string, unknown>>[];
  readonly systemInstructions?: string;
  readonly temperature?: number;
  readonly maxTokens?: number;
  readonly promptHash?: string;
  readonly promptVersion?: string;
}

export interface ToolCallOptions {
  readonly args?: Readonly<Record<string, unknown>>;
  readonly risk?: RiskLevel | Lowercase<RiskLevel>;
  readonly version?: string;
  /** Recorded only as a short hash. */
  readonly idempotencyKey?: string;
  readonly attempt?: number;
  readonly callId?: string;
}

export interface RetrievalOptions {
  readonly query?: string;
}

export interface PolicyDecisionOptions {
  readonly policy?: string;
  readonly version?: string;
  readonly rule?: string;
  readonly tool?: string;
  readonly reason?: string;
  /** The runtime gateway's record of the decision (ADR-0033). */
  readonly decisionId?: string;
  readonly approvalId?: string;
}

export interface OutcomeOptions {
  readonly businessOutcome?: string;
  readonly claimed?: OutcomeStatus;
  readonly verified?: boolean;
  readonly verificationSource?: VerificationSource;
  readonly stateDiff?: Readonly<Record<string, unknown>>;
}

type Callback<S, T> = (span: S) => T;

/** The root span of one agent run. */
export class AgentRun extends Span {
  readonly agent: string;
  private readonly ctx: Readonly<Record<string, AttributeValue>>;

  /** @internal */
  constructor(
    client: AgentTwin,
    agent: string,
    name: string,
    attributes: Readonly<Record<string, AttributeValue | null | undefined>>,
    parent: ParentContext | undefined,
    ctx: Readonly<Record<string, AttributeValue>>,
  ) {
    super(client, name, SpanKind.INTERNAL, attributes, parent, undefined);
    this.agent = agent;
    this.ctx = ctx;
    if (this.recording) client.runContexts.set(this.traceId, ctx);
  }

  protected override onEnding(): void {
    if (this.client.runContexts.get(this.traceId) === this.ctx) this.client.runContexts.delete(this.traceId);
  }

  // -- model calls

  /** Runs `fn` as a model call; the value it returns is not recorded. */
  modelCall<T>(provider: string, model: string, fn: Callback<ModelCall, T>): T;
  modelCall<T>(provider: string, model: string, options: ModelCallOptions, fn: Callback<ModelCall, T>): T;
  modelCall<T>(
    provider: string,
    model: string,
    optionsOrFn: ModelCallOptions | Callback<ModelCall, T>,
    fn?: Callback<ModelCall, T>,
  ): T {
    const [options, callback] = split<ModelCallOptions, Callback<ModelCall, T>>(optionsOrFn, fn);
    return runIn(this.startModelCall(provider, model, options), callback);
  }

  startModelCall(provider: string, model: string, options: ModelCallOptions = {}): ModelCall {
    return new ModelCall(this.client, this, provider, model, options);
  }

  // -- tool calls

  /**
   * Runs `fn` as a tool call. Unless `fn` sets a result itself, the value it
   * returns is recorded as the result (status `ok`); an exception marks the
   * call failed (`timeout` for a TimeoutError, else `error`) and is rethrown.
   */
  toolCall<T>(name: string, fn: Callback<ToolCall, T>): T;
  toolCall<T>(name: string, options: ToolCallOptions, fn: Callback<ToolCall, T>): T;
  toolCall<T>(
    name: string,
    optionsOrFn: ToolCallOptions | Callback<ToolCall, T>,
    fn?: Callback<ToolCall, T>,
  ): T {
    const [options, callback] = split<ToolCallOptions, Callback<ToolCall, T>>(optionsOrFn, fn);
    return runIn(this.startToolCall(name, options), callback, recordReturnedResult);
  }

  startToolCall(name: string, options: ToolCallOptions = {}): ToolCall {
    return new ToolCall(this.client, childParent(this), this, name, options);
  }

  // -- retrieval

  retrieval<T>(source: string, fn: Callback<Retrieval, T>): T;
  retrieval<T>(source: string, options: RetrievalOptions, fn: Callback<Retrieval, T>): T;
  retrieval<T>(
    source: string,
    optionsOrFn: RetrievalOptions | Callback<Retrieval, T>,
    fn?: Callback<Retrieval, T>,
  ): T {
    const [options, callback] = split<RetrievalOptions, Callback<Retrieval, T>>(optionsOrFn, fn);
    return runIn(this.startRetrieval(source, options), callback);
  }

  startRetrieval(source: string, options: RetrievalOptions = {}): Retrieval {
    return new Retrieval(this.client, childParent(this), this, source, options);
  }

  // -- point-in-time records

  /**
   * Records a policy decision taken for this run (a point-in-time span, a
   * child of the active span: inside a tool call, of that call).
   */
  policyDecision(decision: PolicyDecision, options: PolicyDecisionOptions = {}): void {
    try {
      const attrs: Record<string, AttributeValue | undefined> = {
        [A.SPAN_KIND]: "policy",
        [A.POLICY_DECISION]: decision,
        [A.POLICY_NAME]: options.policy || undefined,
        [A.POLICY_VERSION]: options.version || undefined,
        [A.POLICY_RULE]: options.rule || undefined,
        [A.GENAI_TOOL_NAME]: options.tool || undefined,
        [A.POLICY_DECISION_ID]: options.decisionId || undefined,
        [A.POLICY_APPROVAL_ID]: options.approvalId || undefined,
      };
      const span = new Span(
        this.client,
        "policy.decision",
        SpanKind.INTERNAL,
        attrs,
        childParent(this),
        this,
      );
      span.setAttribute(A.OUTPUT, this.client.content.text(options.reason));
      span.end();
    } catch {}
  }

  /**
   * Reports the run's outcome.
   *
   * An agent's own final answer is not verification (spec §15): leave
   * `verificationSource` at "unavailable" unless an independent check
   * (state assertion, tool result, callback...) backs the status.
   */
  outcome(status: OutcomeStatus, options: OutcomeOptions = {}): void {
    const verified = options.verified ?? false;
    const source = options.verificationSource ?? "unavailable";
    if (
      !OUTCOME_STATUSES.includes(status) ||
      (options.claimed !== undefined && !OUTCOME_STATUSES.includes(options.claimed))
    ) {
      throw new RangeError(`outcome status must be one of ${OUTCOME_STATUSES.join(", ")}`);
    }
    if (!VERIFICATION_SOURCES.includes(source)) {
      throw new RangeError(`verificationSource must be one of ${VERIFICATION_SOURCES.join(", ")}`);
    }
    if (verified && source === "unavailable") {
      throw new RangeError("an outcome cannot be verified when verification is unavailable");
    }
    try {
      const attrs: Record<string, AttributeValue | undefined> = {
        [A.SPAN_KIND]: "outcome",
        [A.OUTCOME_STATUS]: status,
        [A.OUTCOME_VERIFIED]: verified,
        [A.OUTCOME_VERIFICATION_SOURCE]: source,
        [A.OUTCOME_BUSINESS]: options.businessOutcome || undefined,
        [A.OUTCOME_CLAIMED]: options.claimed,
      };
      if (options.stateDiff && Object.keys(options.stateDiff).length > 0) {
        attrs[A.STATE_DIFF] = canonicalJson(toJsonValue(options.stateDiff));
      }
      new Span(this.client, "outcome.verify", SpanKind.INTERNAL, attrs, childParent(this), this).end();
    } catch {}
  }

  /** The agent's final answer (content: recorded only when the content mode allows). */
  setOutput(text: string | null | undefined): void {
    this.setAttribute(A.OUTPUT, this.client.content.text(text));
  }

  /** Marks the run failed without an exception (e.g. step budget exhausted). */
  setError(errorType: string, message?: string): void {
    this.fail(errorType, message);
  }
}

function split<O extends object, F>(optionsOrFn: O | F, fn: F | undefined): [O, F] {
  if (typeof optionsOrFn === "function") return [{} as O, optionsOrFn as F];
  if (fn === undefined) throw new TypeError("a callback is required");
  return [optionsOrFn as O, fn];
}

function recordReturnedResult(call: ToolCall, value: unknown): void {
  if (call.resultStatus === undefined) call.setResult(value);
}

/** A model (LLM) call inside a run. */
export class ModelCall extends Span {
  /** @internal */
  constructor(client: AgentTwin, run: AgentRun, provider: string, model: string, options: ModelCallOptions) {
    super(
      client,
      `chat ${model}`,
      SpanKind.CLIENT,
      {
        [A.SPAN_KIND]: "model",
        [A.GENAI_OPERATION]: "chat",
        [A.GENAI_PROVIDER]: provider,
        [A.GENAI_REQUEST_MODEL]: model,
        [A.GENAI_REQUEST_TEMPERATURE]:
          options.temperature === undefined ? undefined : new Double(options.temperature),
        [A.GENAI_REQUEST_MAX_TOKENS]:
          options.maxTokens === undefined ? undefined : Math.trunc(options.maxTokens),
        [A.PROMPT_HASH]: options.promptHash || undefined,
        [A.PROMPT_VERSION]: options.promptVersion || undefined,
      },
      childParent(run),
      run,
    );
    if (options.inputMessages && options.inputMessages.length > 0) {
      this.setAttribute(A.GENAI_INPUT_MESSAGES, client.content.value(options.inputMessages));
    }
    this.setAttribute(A.GENAI_SYSTEM_INSTRUCTIONS, client.content.text(options.systemInstructions));
  }

  recordResponse(response: {
    readonly outputMessages?: readonly Readonly<Record<string, unknown>>[];
    readonly inputTokens?: number;
    readonly outputTokens?: number;
    readonly finishReasons?: readonly string[];
    readonly responseModel?: string;
    readonly costUsd?: number;
  }): void {
    if (response.inputTokens !== undefined)
      this.setAttribute(A.GENAI_USAGE_INPUT_TOKENS, Math.trunc(response.inputTokens));
    if (response.outputTokens !== undefined) {
      this.setAttribute(A.GENAI_USAGE_OUTPUT_TOKENS, Math.trunc(response.outputTokens));
    }
    if (response.finishReasons && response.finishReasons.length > 0) {
      this.setAttribute(A.GENAI_FINISH_REASONS, [...response.finishReasons]);
    }
    if (response.responseModel) this.setAttribute(A.GENAI_RESPONSE_MODEL, response.responseModel);
    if (response.costUsd !== undefined) this.setAttribute(A.COST_USD, new Double(response.costUsd));
    if (response.outputMessages && response.outputMessages.length > 0) {
      this.setAttribute(A.GENAI_OUTPUT_MESSAGES, this.client.content.value(response.outputMessages));
    }
  }

  setError(errorType: string, message?: string): void {
    this.fail(errorType, message);
  }
}

function riskLevel(risk: string | undefined): RiskLevel | undefined {
  if (!risk) return undefined;
  const level = risk.toUpperCase() as RiskLevel;
  if (!RISK_LEVELS.includes(level)) {
    throw new RangeError(`unknown tool risk ${risk}; expected one of ${RISK_LEVELS.join(", ")}`);
  }
  return level;
}

/** A tool call inside a run (or on its own, from `toolSpan` outside a run). */
export class ToolCall extends Span {
  readonly toolName: string;
  private recordedStatus: ToolResultStatus | undefined;

  /** @internal */
  constructor(
    client: AgentTwin,
    parent: ParentContext | undefined,
    run: AgentRun | undefined,
    name: string,
    options: ToolCallOptions,
  ) {
    const risk = riskLevel(options.risk);
    super(
      client,
      `execute_tool ${name}`,
      SpanKind.INTERNAL,
      {
        [A.SPAN_KIND]: "tool",
        [A.GENAI_OPERATION]: "execute_tool",
        [A.GENAI_TOOL_NAME]: name,
        [A.TOOL_ARGS_HASH]: argsHash(options.args ?? {}),
        [A.TOOL_RISK]: risk,
        [A.TOOL_VERSION]: options.version || undefined,
        [A.TOOL_IDEMPOTENCY_KEY_HASH]: options.idempotencyKey
          ? idempotencyKeyHash(options.idempotencyKey)
          : undefined,
        [A.TOOL_ATTEMPT]: options.attempt === undefined ? undefined : Math.trunc(options.attempt),
        [A.GENAI_TOOL_CALL_ID]: options.callId || undefined,
      },
      parent,
      run,
    );
    this.toolName = name;
    if (options.args !== undefined)
      this.setAttribute(A.GENAI_TOOL_ARGUMENTS, client.content.value(options.args));
  }

  /** The result status recorded so far, if any. */
  get resultStatus(): ToolResultStatus | undefined {
    return this.recordedStatus;
  }

  /** Records the tool result. A status other than `ok` marks the span failed. */
  setResult(result?: unknown, status: ToolResultStatus = "ok"): void {
    if (!TOOL_RESULT_STATUSES.includes(status)) throw new RangeError(`unknown tool result status ${status}`);
    this.recordedStatus = status;
    this.setAttribute(A.TOOL_RESULT_STATUS, status);
    if (result !== undefined && result !== null) {
      this.setAttribute(A.GENAI_TOOL_RESULT, this.client.content.value(result));
    }
    if (status !== "ok") this.fail(status);
  }

  /** Records a failed call (`timeout`, `rate_limited`, `error`...). */
  setError(
    status: Exclude<ToolResultStatus, "ok">,
    errorType?: string,
    options: { httpStatus?: number } = {},
  ): void {
    if ((status as string) === "ok" || !TOOL_RESULT_STATUSES.includes(status)) {
      throw new RangeError("setError requires a failure status");
    }
    this.recordedStatus = status;
    this.setAttribute(A.TOOL_RESULT_STATUS, status);
    if (options.httpStatus !== undefined) this.setAttribute(A.HTTP_STATUS, Math.trunc(options.httpStatus));
    this.fail(errorType || status);
  }

  override end(error?: unknown): void {
    if (error !== undefined && this.recordedStatus === undefined && !this.isEnded) {
      this.recordedStatus = errorName(error) === "TimeoutError" ? "timeout" : "error";
      this.setAttribute(A.TOOL_RESULT_STATUS, this.recordedStatus);
    }
    super.end(error);
  }
}

/** A retrieval step (knowledge base, search index...). */
export class Retrieval extends Span {
  /** @internal */
  constructor(
    client: AgentTwin,
    parent: ParentContext,
    run: AgentRun,
    source: string,
    options: RetrievalOptions,
  ) {
    super(
      client,
      `retrieval ${source}`,
      SpanKind.INTERNAL,
      { [A.SPAN_KIND]: "retrieval", [A.RETRIEVAL_SOURCE]: source },
      parent,
      run,
    );
    this.setAttribute(A.INPUT, client.content.text(options.query));
  }

  setDocuments(documents: readonly unknown[]): void {
    this.setAttribute(A.RETRIEVAL_DOCUMENT_COUNT, documents.length);
    this.setAttribute(A.OUTPUT, this.client.content.value(documents));
  }
}

// ---------------------------------------------------------------- module-level API

let defaultInstance: AgentTwin | undefined;

/**
 * Configures the process-wide default client from `AGENTTWIN_*` variables;
 * `options` win. Replaces (and shuts down) a previous default client.
 */
export function configure(options: ConfigOptions = {}, clientOptions: AgentTwinOptions = {}): AgentTwin {
  const previous = defaultInstance;
  defaultInstance = new AgentTwin(configFromEnv(options), clientOptions);
  void previous?.shutdown();
  return defaultInstance;
}

/** The process-wide default client (configured from the environment on first use). */
export function defaultClient(): AgentTwin {
  defaultInstance ??= new AgentTwin(configFromEnv());
  return defaultInstance;
}

/** The agent run active in this asynchronous context, if any. */
export function currentRun(): AgentRun | undefined {
  return storage.getStore()?.run;
}

/** The span active in this asynchronous context, if any. */
export function currentSpan(): Span | undefined {
  return storage.getStore()?.span;
}

/**
 * `await agentTrace({ agent: "refund-agent", version: "1.3.0" }, async (trace) => { ... })`
 *
 * Runs `fn` as an agent run of the default client (see `AgentTwin.agentRun`).
 */
export function agentTrace<T>(options: AgentRunOptions, fn: (run: AgentRun) => T): T {
  return defaultClient().agentRun(options, fn);
}

/** Starts an agent run of the default client that you end yourself. */
export function startAgentTrace(options: AgentRunOptions): AgentRun {
  return defaultClient().startAgentRun(options);
}

/** Records the outcome of the active agent run (no-op outside a run). */
export function outcome(status: OutcomeStatus, options: OutcomeOptions = {}): void {
  currentRun()?.outcome(status, options);
}

export interface ToolSpanOptions {
  /** The tool name; the function's name by default. */
  readonly name?: string;
  readonly risk?: RiskLevel | Lowercase<RiskLevel>;
  readonly version?: string;
  /**
   * Names for positional arguments, so they are recorded as
   * `{name: value}`. Without it, a single plain-object argument is recorded
   * as the arguments, and anything else as `{args: [...]}`.
   */
  readonly argNames?: readonly string[];
  /** The argument holding the idempotency key (recorded as a hash only);
   * `idempotencyKey` or `idempotency_key` by default, null for none. */
  readonly idempotencyKeyArg?: string | null;
  /** The client; the default client at call time otherwise. */
  readonly client?: AgentTwin;
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any -- any function
type AnyFunction = (this: any, ...args: any[]) => any;

/**
 * Wraps a tool function so every call is traced as a tool span: a child of
 * the active agent run, with the arguments hashed (and, as the content mode
 * allows, recorded), the returned value as the result, exceptions recorded
 * and rethrown (`timeout` for a TimeoutError). Works for sync and async
 * functions.
 *
 * ```ts
 * const refundPayment = toolSpan({ name: "refund_payment", risk: "WRITE_IRREVERSIBLE" }, async (args: Refund) => ...);
 * ```
 */
export function toolSpan<F extends AnyFunction>(fn: F): F;
export function toolSpan<F extends AnyFunction>(options: ToolSpanOptions, fn: F): F;
export function toolSpan<F extends AnyFunction>(optionsOrFn: ToolSpanOptions | F, maybeFn?: F): F {
  const [options, fn] = split<ToolSpanOptions, F>(optionsOrFn, maybeFn);
  const name = options.name || fn.name || "tool";
  riskLevel(options.risk); // a wrong risk level fails where the tool is declared
  const wrapped = function (this: unknown, ...args: unknown[]): unknown {
    const bound = boundArgs(args, options.argNames);
    const keyArg = options.idempotencyKeyArg;
    const key =
      keyArg === null
        ? undefined
        : keyArg !== undefined
          ? bound[keyArg]
          : (bound.idempotencyKey ?? bound.idempotency_key);
    const run = currentRun();
    const call = new ToolCall(options.client ?? defaultClient(), currentSpan()?.parentContext, run, name, {
      args: bound,
      ...(options.risk === undefined ? {} : { risk: options.risk }),
      ...(options.version === undefined ? {} : { version: options.version }),
      ...(typeof key === "string" ? { idempotencyKey: key } : {}),
    });
    return runIn(call, () => fn.apply(this, args) as unknown, recordReturnedResult);
  };
  Object.defineProperty(wrapped, "name", { value: fn.name });
  return wrapped as F;
}

function boundArgs(args: readonly unknown[], names: readonly string[] | undefined): Record<string, unknown> {
  if (names !== undefined) {
    const out: Record<string, unknown> = {};
    names.forEach((n, i) => {
      if (i < args.length && args[i] !== undefined) out[n] = args[i];
    });
    return out;
  }
  if (args.length === 0) return {};
  const [first] = args;
  if (args.length === 1 && isPlainObject(first)) return first;
  return { args: [...args] };
}

function isPlainObject(v: unknown): v is Record<string, unknown> {
  if (typeof v !== "object" || v === null) return false;
  const proto: unknown = Object.getPrototypeOf(v);
  return proto === Object.prototype || proto === null;
}
