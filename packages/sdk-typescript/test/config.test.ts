import { describe, expect, it } from "vitest";

import { configFromEnv, makeConfig } from "../src/config.js";

describe("config", () => {
  it("defaults are safe: content off, production source, full sampling", () => {
    const c = makeConfig();
    expect(c.contentMode).toBe("off");
    expect(c.source).toBe("production");
    expect(c.environment).toBe("development");
    expect(c.otlpEndpoint).toBe("http://localhost:4318");
    expect(c.sampleRatio).toBe(1);
    expect(c.enabled).toBe(true);
    expect(c.maxQueueSize).toBe(2048);
    expect(Object.isFrozen(c)).toBe(true);
  });

  it("reads AGENTTWIN_* variables; options win; empty variables are unset", () => {
    const env = {
      AGENTTWIN_API_KEY: "atk_x",
      OTEL_EXPORTER_OTLP_ENDPOINT: "http://otel:4318",
      AGENTTWIN_API_URL: "http://cp:8080",
      AGENTTWIN_PROJECT: "support",
      AGENTTWIN_SERVICE_NAME: "svc",
      AGENTTWIN_ENVIRONMENT: "staging",
      AGENTTWIN_RELEASE_ID: "rel",
      AGENTTWIN_COMMIT_SHA: "abc",
      AGENTTWIN_SOURCE: "simulation",
      AGENTTWIN_CONTENT_MODE: "redacted",
      AGENTTWIN_SAMPLE_RATIO: "0.25",
      AGENTTWIN_DISABLED: "yes",
      AGENTTWIN_REDACTION_STRATEGY: "hash",
      AGENTTWIN_REDACTION_PATTERNS: "ACME-\\d+\n\nORD-\\d+",
      AGENTTWIN_REDACTION_JSON_PATHS: " $.a , ,$.b[*]",
    };
    const c = configFromEnv({ environment: "prod", project: undefined }, env);
    expect(c).toMatchObject({
      apiKey: "atk_x",
      otlpEndpoint: "http://otel:4318",
      apiUrl: "http://cp:8080",
      project: "support",
      serviceName: "svc",
      environment: "prod",
      releaseId: "rel",
      commitSha: "abc",
      source: "simulation",
      contentMode: "redacted",
      sampleRatio: 0.25,
      enabled: false,
      redaction: {
        strategy: "hash",
        customPatterns: ["ACME-\\d+", "ORD-\\d+"],
        jsonPaths: ["$.a", "$.b[*]"],
      },
    });
    expect(
      configFromEnv({}, { AGENTTWIN_OTLP_ENDPOINT: "http://a", OTEL_EXPORTER_OTLP_ENDPOINT: "http://b" })
        .otlpEndpoint,
    ).toBe("http://a");
    expect(configFromEnv({}, { AGENTTWIN_API_KEY: "", AGENTTWIN_DISABLED: "no" })).toMatchObject({
      apiKey: undefined,
      enabled: true,
    });
  });

  it.each([
    [{ contentMode: "everything" }],
    [{ source: "prod" }],
    [{ sampleRatio: 1.5 }],
    [{ sampleRatio: Number.NaN }],
    [{ maxQueueSize: 0 }],
    [{ maxExportBatchSize: 1.5 }],
    [{ redaction: { strategy: "shred" } }],
  ])("rejects %o", (options) => {
    expect(() => makeConfig(options as never)).toThrow(RangeError);
  });

  it("rejects invalid environment values", () => {
    expect(() => configFromEnv({}, { AGENTTWIN_SAMPLE_RATIO: "half" })).toThrow(RangeError);
    expect(() => configFromEnv({}, { AGENTTWIN_REDACTION_STRATEGY: "shred" })).toThrow(RangeError);
    expect(() => configFromEnv({}, { AGENTTWIN_CONTENT_MODE: "all" })).toThrow(RangeError);
  });
});
