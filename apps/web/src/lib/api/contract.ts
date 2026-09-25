/**
 * Helpers over the operations openapi-typescript generates from a service
 * contract (packages/contracts/openapi, `*.gen.ts`). The contracts are checked
 * against the services on every test run (ADR-0021), so a type derived here is
 * what the service does.
 *
 * `Ops` is a generated `operations` interface and `Op` one of its operation ids.
 */

/** The query parameters an operation documents. */
export type QueryOf<Ops, Op extends keyof Ops> = Ops[Op] extends { parameters: { query?: infer Q } }
  ? NonNullable<Q>
  : never;

/** The JSON body an operation accepts. */
export type BodyOf<Ops, Op extends keyof Ops> = Ops[Op] extends { requestBody?: infer R }
  ? NonNullable<R> extends { content: { "application/json": infer B } }
    ? B
    : never
  : never;

/** The documented responses of an operation, by status. */
export type ResponsesOf<Ops, Op extends keyof Ops> = Ops[Op] extends { responses: infer R } ? R : never;

/** The JSON body of one documented response of an operation. */
export type ResponseOf<Ops, Op extends keyof Ops, Status extends keyof ResponsesOf<Ops, Op>> = ResponsesOf<
  Ops,
  Op
>[Status] extends { content: { "application/json": infer B } }
  ? B
  : { missing: "this response has no JSON body" };

/** A query parameter value as it travels in a URL (`true`, `12.5`). */
type Wire<T> = T extends boolean ? `${T}` : T extends number ? `${number}` : T;

/** Query parameters in their URL form: what a client may put in a query string. */
export type WireQuery<Q> = { [K in keyof Q]?: Wire<NonNullable<Q[K]>> };

/**
 * Compiles only when a client type that reads `Server` accepts every value of
 * it; otherwise the compiler reports the first field that disagrees ("does
 * not satisfy the constraint", then the path to the field).
 */
export type Accepts<Client, Server extends Client> = Server;

/**
 * Compiles only when every value of `Sent` (what a client sends) is one the
 * service accepts (`Accepted`): an undocumented field or parameter, or a value
 * the service would reject, does not compile. (Assignability alone ignores a
 * property `Accepted` does not declare, so undocumented keys are refused
 * explicitly.)
 */
export type Sends<
  Accepted,
  Sent extends Accepted & { [K in Exclude<keyof Sent, keyof Accepted>]: never },
> = Sent;

/** URL query parameters from documented ones; unset and empty values are left out. */
export function toQuery(params: unknown): URLSearchParams {
  const out = new URLSearchParams();
  for (const [key, value] of Object.entries(params as Record<string, unknown>)) {
    if (value === undefined || value === null || value === "") continue;
    out.set(key, String(value));
  }
  return out;
}
