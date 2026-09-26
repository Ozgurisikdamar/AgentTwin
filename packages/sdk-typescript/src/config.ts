/** SDK configuration from options and `AGENTTWIN_*` environment variables. */

import { STRATEGIES, type RedactionConfig, type Strategy } from "./redaction.js";

export type ContentMode = "off" | "redacted" | "full";
export type Source = "production" | "simulation" | "replay" | "eval" | "test";

export const CONTENT_MODES: readonly ContentMode[] = ["off", "redacted", "full"];
export const SOURCES: readonly Source[] = ["production", "simulation", "replay", "eval", "test"];

/**
 * Everything the SDK needs; every field has a safe default.
 *
 * Content capture is **off** by default (ADR-0008): only metadata, hashes
 * and counts leave the process unless `contentMode` is set explicitly.
 */
export interface Config {
  readonly apiKey: string | undefined;
  /** OTLP/HTTP base URL (the collector); `/v1/traces` is appended. */
  readonly otlpEndpoint: string;
  /** AgentTwin API base URL, used for delayed outcome reporting. */
  readonly apiUrl: string | undefined;
  readonly project: string | undefined;
  readonly serviceName: string | undefined;
  readonly environment: string;
  readonly source: Source;
  readonly releaseId: string | undefined;
  readonly commitSha: string | undefined;
  readonly contentMode: ContentMode;
  readonly redaction: RedactionConfig;
  /** Head sampling ratio for new traces (a parent's decision is respected). */
  readonly sampleRatio: number;
  /** Bounded in-memory export queue; spans beyond it are dropped, never block. */
  readonly maxQueueSize: number;
  readonly maxExportBatchSize: number;
  readonly scheduleDelayMs: number;
  readonly exportTimeoutMs: number;
  /** Upper bound for any single content attribute (bytes, UTF-8). */
  readonly maxContentBytes: number;
  readonly enabled: boolean;
}

export type ConfigOptions = { readonly [K in keyof Config]?: Config[K] | undefined };

export const DEFAULTS: Config = Object.freeze({
  apiKey: undefined,
  otlpEndpoint: "http://localhost:4318",
  apiUrl: undefined,
  project: undefined,
  serviceName: undefined,
  environment: "development",
  source: "production",
  releaseId: undefined,
  commitSha: undefined,
  contentMode: "off",
  redaction: Object.freeze({}),
  sampleRatio: 1,
  maxQueueSize: 2048,
  maxExportBatchSize: 512,
  scheduleDelayMs: 500,
  exportTimeoutMs: 5000,
  maxContentBytes: 8192,
  enabled: true,
});

/** A validated configuration: the defaults with `options` on top. */
export function makeConfig(options: ConfigOptions = {}): Config {
  const defined = Object.fromEntries(Object.entries(options).filter(([, v]) => v !== undefined));
  const config: Config = { ...DEFAULTS, ...defined };
  if (!CONTENT_MODES.includes(config.contentMode)) {
    throw new RangeError(`contentMode must be one of ${CONTENT_MODES.join(", ")}, got ${config.contentMode}`);
  }
  if (!SOURCES.includes(config.source)) {
    throw new RangeError(`source must be one of ${SOURCES.join(", ")}, got ${config.source}`);
  }
  if (!(config.sampleRatio >= 0 && config.sampleRatio <= 1)) {
    throw new RangeError("sampleRatio must be between 0 and 1");
  }
  for (const key of ["maxQueueSize", "maxExportBatchSize"] as const) {
    if (!Number.isInteger(config[key]) || config[key] < 1) {
      throw new RangeError(`${key} must be a positive integer`);
    }
  }
  const strategy = config.redaction.strategy;
  if (strategy !== undefined && !STRATEGIES.includes(strategy)) {
    throw new RangeError(`redaction strategy must be one of ${STRATEGIES.join(", ")}`);
  }
  return Object.freeze(config);
}

/**
 * A configuration from `AGENTTWIN_*` variables; `options` win over the
 * environment. The variables and their meaning are those of the Python SDK.
 */
export function configFromEnv(
  options: ConfigOptions = {},
  env: Readonly<Record<string, string | undefined>> = process.env,
): Config {
  const get = (name: string): string | undefined => {
    const v = env[name];
    return v === undefined || v === "" ? undefined : v;
  };
  const values: Record<string, unknown> = {
    apiKey: get("AGENTTWIN_API_KEY"),
    otlpEndpoint: get("AGENTTWIN_OTLP_ENDPOINT") ?? get("OTEL_EXPORTER_OTLP_ENDPOINT"),
    apiUrl: get("AGENTTWIN_API_URL"),
    project: get("AGENTTWIN_PROJECT"),
    serviceName: get("AGENTTWIN_SERVICE_NAME"),
    environment: get("AGENTTWIN_ENVIRONMENT"),
    releaseId: get("AGENTTWIN_RELEASE_ID"),
    commitSha: get("AGENTTWIN_COMMIT_SHA"),
    source: get("AGENTTWIN_SOURCE"),
    contentMode: get("AGENTTWIN_CONTENT_MODE"),
  };
  const ratio = get("AGENTTWIN_SAMPLE_RATIO");
  if (ratio !== undefined) {
    const n = Number(ratio);
    if (ratio.trim() === "" || Number.isNaN(n))
      throw new RangeError(`AGENTTWIN_SAMPLE_RATIO is not a number: ${ratio}`);
    values.sampleRatio = n;
  }
  const disabled = get("AGENTTWIN_DISABLED");
  if (disabled !== undefined) values.enabled = !["1", "true", "yes"].includes(disabled.toLowerCase());
  const strategy = get("AGENTTWIN_REDACTION_STRATEGY");
  const custom = get("AGENTTWIN_REDACTION_PATTERNS");
  const paths = get("AGENTTWIN_REDACTION_JSON_PATHS");
  if (strategy !== undefined || custom !== undefined || paths !== undefined) {
    if (strategy !== undefined && !STRATEGIES.includes(strategy as Strategy)) {
      throw new RangeError(`AGENTTWIN_REDACTION_STRATEGY must be one of ${STRATEGIES.join(", ")}`);
    }
    values.redaction = {
      strategy: (strategy as Strategy | undefined) ?? "mask",
      customPatterns: (custom ?? "").split("\n").filter((p) => p !== ""),
      jsonPaths: (paths ?? "")
        .split(",")
        .map((p) => p.trim())
        .filter((p) => p !== ""),
    } satisfies RedactionConfig;
  }
  const defined = Object.fromEntries(Object.entries(options).filter(([, v]) => v !== undefined));
  return makeConfig({ ...values, ...defined } as ConfigOptions);
}
