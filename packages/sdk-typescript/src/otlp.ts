/**
 * Finished spans and their OTLP/HTTP JSON encoding (the protobuf JSON mapping
 * of `ExportTraceServiceRequest`: hex ids, 64-bit integers as strings).
 */

/** An attribute value as it is recorded. Doubles are marked so that an
 * integral value (a temperature of 0) is still sent as a double. */
export type AttributeValue = string | number | boolean | Double | readonly string[] | readonly number[];

export class Double {
  constructor(readonly value: number) {}
}

export const SpanKind = { INTERNAL: 1, SERVER: 2, CLIENT: 3, PRODUCER: 4, CONSUMER: 5 } as const;
export type SpanKind = (typeof SpanKind)[keyof typeof SpanKind];

export const StatusCode = { UNSET: 0, OK: 1, ERROR: 2 } as const;
export type StatusCode = (typeof StatusCode)[keyof typeof StatusCode];

export interface SpanEvent {
  readonly name: string;
  readonly timeUnixNano: bigint;
  readonly attributes: Readonly<Record<string, AttributeValue>>;
}

/** A finished span, as the exporter sees it. */
export interface ReadableSpan {
  readonly traceId: string;
  readonly spanId: string;
  readonly parentSpanId: string | undefined;
  readonly name: string;
  readonly kind: SpanKind;
  readonly startTimeUnixNano: bigint;
  readonly endTimeUnixNano: bigint;
  readonly attributes: Readonly<Record<string, AttributeValue>>;
  readonly droppedAttributesCount: number;
  readonly events: readonly SpanEvent[];
  readonly status: { readonly code: StatusCode; readonly message?: string };
}

export interface Resource {
  readonly attributes: Readonly<Record<string, AttributeValue>>;
}

export interface Scope {
  readonly name: string;
  readonly version: string;
}

type AnyValue =
  | { stringValue: string }
  | { boolValue: boolean }
  | { intValue: string }
  | { doubleValue: number }
  | { arrayValue: { values: AnyValue[] } };

function anyValue(v: AttributeValue): AnyValue {
  if (v instanceof Double) return { doubleValue: v.value };
  if (typeof v === "string") return { stringValue: v };
  if (typeof v === "boolean") return { boolValue: v };
  if (typeof v === "number") return Number.isSafeInteger(v) ? { intValue: String(v) } : { doubleValue: v };
  return { arrayValue: { values: (v as readonly (string | number)[]).map(anyValue) } };
}

function keyValues(attrs: Readonly<Record<string, AttributeValue>>): { key: string; value: AnyValue }[] {
  return Object.entries(attrs).map(([key, value]) => ({ key, value: anyValue(value) }));
}

/** The OTLP/HTTP JSON request body for spans of one resource and scope. */
export function encodeRequest(resource: Resource, scope: Scope, spans: readonly ReadableSpan[]): string {
  return JSON.stringify({
    resourceSpans: [
      {
        resource: { attributes: keyValues(resource.attributes) },
        scopeSpans: [
          {
            scope: { name: scope.name, version: scope.version },
            spans: spans.map((s) => ({
              traceId: s.traceId,
              spanId: s.spanId,
              ...(s.parentSpanId === undefined ? {} : { parentSpanId: s.parentSpanId }),
              name: s.name,
              kind: s.kind,
              startTimeUnixNano: s.startTimeUnixNano.toString(),
              endTimeUnixNano: s.endTimeUnixNano.toString(),
              attributes: keyValues(s.attributes),
              ...(s.droppedAttributesCount > 0 ? { droppedAttributesCount: s.droppedAttributesCount } : {}),
              events: s.events.map((e) => ({
                timeUnixNano: e.timeUnixNano.toString(),
                name: e.name,
                attributes: keyValues(e.attributes),
              })),
              status:
                s.status.message === undefined
                  ? { code: s.status.code }
                  : { code: s.status.code, message: s.status.message },
            })),
          },
        ],
      },
    ],
  });
}
