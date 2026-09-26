/**
 * The export pipeline: ended spans go into a bounded in-memory queue and are
 * exported in batches in the background. The application never waits for
 * the network, a full queue drops spans (counted) instead of blocking or
 * growing, and no export failure ever reaches the host application.
 */

import { encodeRequest, type ReadableSpan, type Resource, type Scope } from "./otlp.js";

/** Where finished spans go. `export` resolves to whether the batch was accepted. */
export interface SpanExporter {
  export(spans: readonly ReadableSpan[]): Promise<boolean>;
  shutdown?(): Promise<void>;
}

/** Counters of the export pipeline (spans). */
export class ExportStats {
  /** Recorded spans that ended. */
  ended = 0;
  /** Spans the exporter accepted. */
  exported = 0;
  /** Spans in batches the exporter rejected or could not deliver. */
  failed = 0;
  /** Spans dropped on a full queue or after shutdown. */
  dropped = 0;

  /** Spans that ended but were not (yet) exported: queued, dropped or failed. */
  get notExported(): number {
    return this.ended - this.exported;
  }
}

/** Keeps exported spans in memory; for tests. */
export class InMemorySpanExporter implements SpanExporter {
  private spans: ReadableSpan[] = [];

  export(spans: readonly ReadableSpan[]): Promise<boolean> {
    this.spans.push(...spans);
    return Promise.resolve(true);
  }

  getFinishedSpans(): readonly ReadableSpan[] {
    return this.spans;
  }

  reset(): void {
    this.spans = [];
  }
}

export interface OTLPHttpExporterOptions {
  /** OTLP/HTTP base URL or full `/v1/traces` URL. */
  readonly endpoint: string;
  readonly headers?: Readonly<Record<string, string>> | undefined;
  readonly timeoutMs: number;
  readonly resource: Resource;
  readonly scope: Scope;
  /** Delays before the retries of a retryable failure (429, 502-504, network). */
  readonly retryDelaysMs?: readonly number[] | undefined;
  /** For tests: the fetch implementation. */
  readonly fetch?: typeof fetch | undefined;
}

const RETRYABLE = new Set([429, 502, 503, 504]);

/** Sends spans as OTLP/HTTP JSON (what the AgentTwin collector accepts). */
export class OTLPHttpExporter implements SpanExporter {
  readonly url: string;
  private readonly options: OTLPHttpExporterOptions;
  private readonly fetchImpl: typeof fetch;
  private stopped = false;

  constructor(options: OTLPHttpExporterOptions) {
    const base = options.endpoint.replace(/\/+$/, "");
    this.url = base.endsWith("/v1/traces") ? base : `${base}/v1/traces`;
    this.options = options;
    this.fetchImpl = options.fetch ?? fetch;
  }

  async export(spans: readonly ReadableSpan[]): Promise<boolean> {
    if (spans.length === 0) return true;
    const body = encodeRequest(this.options.resource, this.options.scope, spans);
    const delays = this.options.retryDelaysMs ?? [250, 1000];
    for (let attempt = 0; ; attempt++) {
      let retryable = true;
      try {
        const res = await this.fetchImpl(this.url, {
          method: "POST",
          headers: { "content-type": "application/json", ...this.options.headers },
          body,
          signal: AbortSignal.timeout(this.options.timeoutMs),
        });
        // Read the body so the connection can be reused.
        await res.arrayBuffer().catch(() => undefined);
        if (res.ok) return true;
        retryable = RETRYABLE.has(res.status);
      } catch {
        // Network error or timeout: retryable.
      }
      const delay = delays[attempt];
      if (!retryable || delay === undefined || this.stopped) return false;
      await sleep(delay);
    }
  }

  shutdown(): Promise<void> {
    this.stopped = true;
    return Promise.resolve();
  }
}

export interface BatchOptions {
  readonly maxQueueSize: number;
  readonly maxExportBatchSize: number;
  readonly scheduleDelayMs: number;
}

/**
 * Batches ended spans for an exporter. One export is in flight at a time;
 * a batch leaves when it is full or when the schedule delay has passed since
 * the first span waiting.
 */
export class BatchSpanProcessor {
  private queue: ReadableSpan[] = [];
  private timer: NodeJS.Timeout | undefined;
  private immediate: NodeJS.Immediate | undefined;
  private running: Promise<void> | undefined;
  private stopped = false;
  private readonly batchSize: number;

  constructor(
    private readonly exporter: SpanExporter,
    private readonly stats: ExportStats,
    private readonly options: BatchOptions,
  ) {
    this.batchSize = Math.min(options.maxExportBatchSize, options.maxQueueSize);
  }

  /** Called on the application path: O(1), never waits, never throws. */
  onEnd(span: ReadableSpan): void {
    if (this.stopped || this.queue.length >= this.options.maxQueueSize) {
      this.stats.dropped++;
      return;
    }
    this.queue.push(span);
    if (this.queue.length >= this.batchSize) this.kickSoon();
    else this.schedule();
  }

  /** Exports everything queued; resolves to false when the timeout passed first. */
  async flush(timeoutMs: number): Promise<boolean> {
    let timer: NodeJS.Timeout | undefined;
    const deadline = new Promise<false>((resolve) => {
      timer = setTimeout(() => resolve(false), timeoutMs);
      timer.unref();
    });
    try {
      const drained = (async () => {
        while (this.queue.length > 0 || this.running !== undefined) {
          this.kick();
          await this.running;
        }
        return true as const;
      })();
      return await Promise.race([drained, deadline]);
    } finally {
      clearTimeout(timer);
    }
  }

  /** Flushes, then stops: later spans are dropped. Idempotent. */
  async shutdown(timeoutMs: number): Promise<boolean> {
    if (this.stopped) return true;
    const flushed = await this.flush(timeoutMs);
    // No timer can be pending here: a flush kicks (which clears it) and an
    // export ending after the stop schedules nothing.
    this.stopped = true;
    try {
      await this.exporter.shutdown?.();
    } catch {}
    return flushed;
  }

  private schedule(): void {
    if (this.timer !== undefined || this.running !== undefined) return;
    this.timer = setTimeout(() => {
      this.timer = undefined;
      this.kick();
    }, this.options.scheduleDelayMs);
    // Waiting spans never keep the process alive; exit hooks flush them.
    this.timer.unref();
  }

  /**
   * Starts an export on the next turn of the event loop, never inside the
   * caller's `end()`: encoding a batch is work the application should not
   * wait for.
   */
  private kickSoon(): void {
    if (this.immediate !== undefined || this.running !== undefined) return;
    this.immediate = setImmediate(() => {
      this.immediate = undefined;
      this.kick();
    });
    this.immediate.unref();
  }

  private kick(): void {
    if (this.running !== undefined) return;
    clearTimeout(this.timer);
    this.timer = undefined;
    this.running = this.drain().finally(() => {
      this.running = undefined;
      if (this.queue.length > 0 && !this.stopped) this.schedule();
    });
  }

  private async drain(): Promise<void> {
    do {
      const batch = this.queue.splice(0, this.batchSize);
      if (batch.length === 0) return;
      let ok = false;
      try {
        ok = await this.exporter.export(batch);
      } catch {}
      if (ok) this.stats.exported += batch.length;
      else this.stats.failed += batch.length;
      // A partial batch waits for the schedule (or a flush, which kicks again).
    } while (this.queue.length >= this.batchSize);
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms).unref());
}
