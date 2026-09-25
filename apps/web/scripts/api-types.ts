/**
 * TypeScript types of the service APIs, generated from their OpenAPI documents
 * (packages/contracts/openapi, ADR-0021).
 *
 *   node scripts/api-types.ts           write the files
 *   node scripts/api-types.ts --check   fail when a committed file is stale
 *
 * A unit test runs the same check, so a document change without regenerated
 * types fails `make test`.
 *
 * A node marked `x-agenttwin-schema: scenario.v1` (or `scenario.v1#/$defs/fault`)
 * is an embedded document: the contract check validates it against that
 * canonical JSON Schema, so its type is generated from the same schema.
 */
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import openapiTS, { astToString, COMMENT_HEADER, type OpenAPI3 } from "openapi-typescript";
import { parse } from "yaml";

const WEB = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const CONTRACTS = path.resolve(WEB, "../../packages/contracts/openapi");
const DOCUMENT_SCHEMAS = path.resolve(WEB, "../../packages/scenario-schema/schemas");
const EMBEDDED = /^([a-z][a-z0-9-]*\.v[0-9]+)(?:#\/\$defs\/([A-Za-z][A-Za-z0-9_]*))?$/;

/** Each document and the file its types go to (relative to apps/web). */
export const API_TYPES = [
  { document: "simulation-service.openapi.yaml", output: "src/lib/api/simulation.gen.ts" },
] as const;

type Json = Record<string, unknown>;

function pascal(text: string): string {
  return text
    .split(/[^A-Za-z0-9]+/)
    .filter(Boolean)
    .map((w) => w[0]!.toUpperCase() + w.slice(1))
    .join("");
}

/** The component a canonical schema (or one of its definitions) becomes: `ScenarioV1`, `ScenarioV1Fault`. */
export function canonicalComponent(schema: string, definition?: string): string {
  return pascal(schema) + (definition ? pascal(definition) : "");
}

/** A canonical document schema and its definitions as OpenAPI components. */
function canonicalComponents(schema: string): Record<string, unknown> {
  const raw = JSON.parse(readFileSync(path.join(DOCUMENT_SCHEMAS, `${schema}.schema.json`), "utf8")) as Json;
  // The dialect and the id stay behind: inside an OpenAPI document an `$id`
  // would change what the references resolve against.
  const root: Json = { ...raw };
  const $defs = (root.$defs ?? {}) as Json;
  for (const key of ["$defs", "$schema", "$id"]) delete root[key];
  const rewrite = (node: unknown): unknown => {
    if (Array.isArray(node)) return node.map(rewrite);
    if (!node || typeof node !== "object") return node;
    return Object.fromEntries(
      Object.entries(node).map(([key, value]) => {
        if (key !== "$ref") return [key, rewrite(value)];
        const def = typeof value === "string" ? /^#\/\$defs\/([A-Za-z][A-Za-z0-9_]*)$/.exec(value) : null;
        if (value === "#") return [key, `#/components/schemas/${canonicalComponent(schema)}`];
        if (!def) throw new Error(`${schema}: unsupported $ref ${String(value)}`);
        return [key, `#/components/schemas/${canonicalComponent(schema, def[1])}`];
      }),
    );
  };
  const out: Record<string, unknown> = { [canonicalComponent(schema)]: rewrite(root) };
  for (const [name, def] of Object.entries($defs)) out[canonicalComponent(schema, name)] = rewrite(def);
  return out;
}

/**
 * The document with every `x-agenttwin-schema` node replaced by a reference to
 * its canonical schema, which is added to the components.
 */
export function withCanonicalSchemas(document: Json): Json {
  const added: Record<string, unknown> = {};
  const visit = (node: unknown): unknown => {
    if (Array.isArray(node)) return node.map(visit);
    if (!node || typeof node !== "object") return node;
    const marker = (node as Json)["x-agenttwin-schema"];
    if (marker === undefined) {
      return Object.fromEntries(Object.entries(node).map(([k, v]) => [k, visit(v)]));
    }
    const m = typeof marker === "string" ? EMBEDDED.exec(marker) : null;
    if (!m) throw new Error(`invalid x-agenttwin-schema ${JSON.stringify(marker)}`);
    const [, schema, definition] = m as unknown as [string, string, string | undefined];
    if (!(canonicalComponent(schema) in added)) Object.assign(added, canonicalComponents(schema));
    const name = canonicalComponent(schema, definition);
    if (!(name in added)) throw new Error(`${schema} has no definition ${definition}`);
    const { description } = node as Json;
    return { $ref: `#/components/schemas/${name}`, ...(description ? { description } : {}) };
  };
  const out = visit(document) as Json;
  const components = (out.components ?? {}) as Json;
  const schemas = (components.schemas ?? {}) as Json;
  for (const name of Object.keys(added)) {
    if (name in schemas) throw new Error(`component ${name} already exists in the document`);
  }
  return { ...out, components: { ...components, schemas: { ...schemas, ...added } } };
}

/** The generated source for one document. */
export async function generateApiTypes(document: string): Promise<string> {
  const parsed = parse(readFileSync(path.join(CONTRACTS, document), "utf8")) as Json;
  const ast = await openapiTS(withCanonicalSchemas(parsed) as unknown as OpenAPI3, {
    // `type: object` without properties is any object (JSON Schema), not an
    // empty one: tool arguments, error details, twin state.
    emptyObjectsUnknown: true,
    // A default is what the reader assumes when a field is absent, not a
    // promise that it is present: stored documents keep what their author wrote.
    defaultNonNullable: false,
    silent: true,
  });
  return COMMENT_HEADER + astToString(ast);
}

/** Absolute path of a generated file. */
export function outputPath(output: string): string {
  return path.join(WEB, output);
}

async function main(check: boolean): Promise<number> {
  let stale = 0;
  for (const { document, output } of API_TYPES) {
    const source = await generateApiTypes(document);
    const file = outputPath(output);
    let current = "";
    try {
      current = readFileSync(file, "utf8");
    } catch {
      current = "";
    }
    if (current === source) {
      console.log(`${output}: up to date`);
    } else if (check) {
      console.error(`${output}: stale — run \`pnpm --filter @agenttwin/web gen:api\``);
      stale += 1;
    } else {
      writeFileSync(file, source);
      console.log(`${output}: written from ${document}`);
    }
  }
  return stale ? 1 : 0;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exitCode = await main(process.argv.includes("--check"));
}
