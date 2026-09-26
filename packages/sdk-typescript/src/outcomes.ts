/**
 * Delayed outcome reporting over the AgentTwin REST API.
 *
 * Some outcomes are only known after the agent run ended (a refund settles,
 * a customer replies). `reportOutcome` attaches such an outcome to the trace
 * through `POST /api/v1/traces/{traceId}/outcome` with the project API key
 * (scope `traces:write`).
 */

import { configFromEnv, type Config } from "./config.js";
import {
  OUTCOME_STATUSES,
  VERIFICATION_SOURCES,
  type OutcomeStatus,
  type VerificationSource,
} from "./tracing.js";

/** The API rejected the request or could not be reached (`status` 0). */
export class APIError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly details: Readonly<Record<string, unknown>> = {},
  ) {
    super(message);
    this.name = "APIError";
  }

  override toString(): string {
    return `${this.status} ${this.code}: ${this.message}`;
  }
}

/** The API rejected or could not accept the outcome. */
export class OutcomeReportError extends APIError {
  constructor(
    status: number,
    code: string,
    message: string,
    details: Readonly<Record<string, unknown>> = {},
  ) {
    super(status, code, message, details);
    this.name = "OutcomeReportError";
  }
}

export interface ReportOutcomeOptions {
  /** API URL and key; from `AGENTTWIN_API_URL` / `AGENTTWIN_API_KEY` by default. */
  readonly config?: Config;
  readonly businessOutcome?: string;
  readonly verified?: boolean;
  /** `external_callback` by default: the caller checked the outcome itself. */
  readonly verificationSource?: VerificationSource;
  /** What the agent claimed; a contradicting verification is flagged. */
  readonly claimedStatus?: OutcomeStatus;
  readonly expectedState?: Readonly<Record<string, unknown>>;
  readonly actualState?: Readonly<Record<string, unknown>>;
  readonly notes?: string;
  /** Makes a retried report safe: the API replays the first answer. */
  readonly idempotencyKey?: string;
  readonly timeoutMs?: number;
  /** For tests: the fetch implementation. */
  readonly fetch?: typeof fetch;
}

const MAX_RESPONSE_BYTES = 1 << 20;

/**
 * Records an outcome for `traceId`; resolves to the stored outcome. Rejects
 * with OutcomeReportError on API errors (status 0 when the API is not
 * reachable) and with RangeError on invalid input.
 */
export async function reportOutcome(
  traceId: string,
  status: OutcomeStatus,
  options: ReportOutcomeOptions = {},
): Promise<Record<string, unknown>> {
  const cfg = options.config ?? configFromEnv();
  if (!cfg.apiUrl || !cfg.apiKey) {
    throw new RangeError("reportOutcome needs apiUrl and apiKey (AGENTTWIN_API_URL / AGENTTWIN_API_KEY)");
  }
  const base = new URL(cfg.apiUrl);
  if (base.protocol !== "http:" && base.protocol !== "https:") {
    throw new RangeError("apiUrl must be an http(s) URL");
  }
  const source = options.verificationSource ?? "external_callback";
  if (
    !OUTCOME_STATUSES.includes(status) ||
    (options.claimedStatus !== undefined && !OUTCOME_STATUSES.includes(options.claimedStatus))
  ) {
    throw new RangeError(`status must be one of ${OUTCOME_STATUSES.join(", ")}`);
  }
  if (!VERIFICATION_SOURCES.includes(source)) {
    throw new RangeError(`verificationSource must be one of ${VERIFICATION_SOURCES.join(", ")}`);
  }
  if (!/^[0-9a-fA-F]{32}$/.test(traceId)) throw new RangeError("traceId must be 32 hex characters");

  const body: Record<string, unknown> = {
    status,
    verified: options.verified ?? false,
    verification_source: source,
  };
  const optional: [string, unknown][] = [
    ["business_outcome", options.businessOutcome],
    ["claimed_status", options.claimedStatus],
    ["expected_state", options.expectedState],
    ["actual_state", options.actualState],
    ["notes", options.notes],
  ];
  for (const [key, value] of optional) if (value !== undefined) body[key] = value;

  const url = `${cfg.apiUrl.replace(/\/+$/, "")}/api/v1/traces/${traceId.toLowerCase()}/outcome`;
  const headers: Record<string, string> = {
    accept: "application/json",
    "content-type": "application/json",
    "x-agenttwin-api-key": cfg.apiKey,
  };
  if (options.idempotencyKey) headers["idempotency-key"] = options.idempotencyKey;
  let res: Response;
  try {
    res = await (options.fetch ?? fetch)(url, {
      method: "POST",
      headers,
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(options.timeoutMs ?? 5000),
    });
  } catch (err) {
    const reason = err instanceof Error ? err.message : String(err);
    throw new OutcomeReportError(0, "UNAVAILABLE", `${cfg.apiUrl} is not reachable: ${reason}`);
  }
  const raw = await res.arrayBuffer().catch(() => new ArrayBuffer(0));
  if (raw.byteLength > MAX_RESPONSE_BYTES) {
    throw new OutcomeReportError(0, "RESPONSE_TOO_LARGE", "response exceeded the client limit");
  }
  let decoded: unknown;
  try {
    decoded = raw.byteLength === 0 ? undefined : JSON.parse(Buffer.from(raw).toString("utf8"));
  } catch {
    decoded = undefined;
  }
  if (!res.ok) {
    const error = (
      decoded as { error?: { code?: unknown; message?: unknown; details?: unknown } } | undefined
    )?.error;
    throw new OutcomeReportError(
      res.status,
      typeof error?.code === "string" ? error.code : "HTTP_ERROR",
      typeof error?.message === "string" ? error.message : res.statusText || "request failed",
      typeof error?.details === "object" && error.details !== null
        ? (error.details as Record<string, unknown>)
        : {},
    );
  }
  return (decoded ?? {}) as Record<string, unknown>;
}
