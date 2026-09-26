import { AgentTwin, type AgentTwinOptions } from "../src/tracing.js";
import { InMemorySpanExporter } from "../src/export.js";
import { Double, type AttributeValue, type ReadableSpan } from "../src/otlp.js";
import type { ConfigOptions } from "../src/config.js";

export const CONTENT_KEYS = [
  "agenttwin.input",
  "agenttwin.input.context",
  "agenttwin.output",
  "gen_ai.input.messages",
  "gen_ai.output.messages",
  "gen_ai.system_instructions",
  "gen_ai.tool.call.arguments",
  "gen_ai.tool.call.result",
];

export function attrs(span: ReadableSpan): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(span.attributes).map(([k, v]: [string, AttributeValue]) => [
      k,
      v instanceof Double ? v.value : v,
    ]),
  );
}

export function byName(spans: readonly ReadableSpan[]): Record<string, ReadableSpan> {
  return Object.fromEntries(spans.map((s) => [s.name, s]));
}

const clients: AgentTwin[] = [];

/** A client exporting into memory; shut down by `closeClients()`. */
export function makeClient(
  options: ConfigOptions = {},
  clientOptions: AgentTwinOptions = {},
): { client: AgentTwin; exporter: InMemorySpanExporter } {
  const exporter = new InMemorySpanExporter();
  const client = new AgentTwin(
    { serviceName: "test-agent", environment: "test", scheduleDelayMs: 50, ...options },
    { exporter, ...clientOptions },
  );
  clients.push(client);
  return { client, exporter };
}

export async function closeClients(): Promise<void> {
  await Promise.all(clients.splice(0).map((c) => c.shutdown()));
}
