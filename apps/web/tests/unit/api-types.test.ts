// @vitest-environment node
/**
 * The TypeScript types of the API are generated from the OpenAPI documents
 * (ADR-0021). A contract change without regenerated types fails here; the
 * compile-time checks in src/lib/api/simulation.ts then hold the UI's types to
 * the new contract.
 */
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { queryOf } from "@/lib/api/simulation";
import { API_TYPES, generateApiTypes, outputPath, withCanonicalSchemas } from "../../scripts/api-types";

type Schemas = Record<string, { properties?: Record<string, Record<string, unknown>>; [k: string]: unknown }>;

describe("generated API types", () => {
  it.each(API_TYPES)("$output is what $document generates", async ({ document, output }) => {
    const generated = await generateApiTypes(document);
    const committed = readFileSync(outputPath(output), "utf8");
    expect(committed === generated, `${output} is stale: run \`pnpm --filter @agenttwin/web gen:api\``).toBe(
      true,
    );
  });

  it("types embedded documents by the canonical schema the contract check validates them against", () => {
    const doc = withCanonicalSchemas({
      openapi: "3.1.0",
      paths: {},
      components: {
        schemas: {
          Case: {
            type: "object",
            properties: {
              scenario: { type: "object", description: "The scenario.", "x-agenttwin-schema": "scenario.v1" },
              faults: {
                type: "array",
                items: { type: "object", "x-agenttwin-schema": "scenario.v1#/$defs/fault" },
              },
            },
          },
        },
      },
    });
    const schemas = (doc.components as { schemas: Schemas }).schemas;
    expect(schemas.Case!.properties!.scenario).toEqual({
      $ref: "#/components/schemas/ScenarioV1",
      description: "The scenario.",
    });
    expect(schemas.Case!.properties!.faults!.items).toEqual({ $ref: "#/components/schemas/ScenarioV1Fault" });
    // The canonical schema comes along with its definitions; its own
    // references point at them, and its `$id` stays behind.
    const spec = schemas.ScenarioV1!.properties!.spec as { properties: Record<string, { items?: unknown }> };
    expect(spec.properties.faults!.items).toEqual({ $ref: "#/components/schemas/ScenarioV1Fault" });
    expect(schemas.ScenarioV1).not.toHaveProperty("$id");
    expect(Object.keys(schemas)).toEqual(
      expect.arrayContaining(["ScenarioV1", "ScenarioV1Fault", "ScenarioV1Expectation"]),
    );
  });

  it("refuses what it cannot type faithfully", () => {
    const marked = (value: unknown) => ({ components: { schemas: { X: { "x-agenttwin-schema": value } } } });
    expect(() => withCanonicalSchemas(marked("scenario.v1#/$defs/meteor"))).toThrow(
      "scenario.v1 has no definition meteor",
    );
    expect(() => withCanonicalSchemas(marked("../secrets"))).toThrow("invalid x-agenttwin-schema");
    expect(() => withCanonicalSchemas(marked(42))).toThrow("invalid x-agenttwin-schema");
    expect(() =>
      withCanonicalSchemas({
        components: { schemas: { ScenarioV1: {}, X: { "x-agenttwin-schema": "scenario.v1" } } },
      }),
    ).toThrow("component ScenarioV1 already exists");
  });
});

describe("queryOf", () => {
  it("writes documented parameters and leaves out unset ones", () => {
    const q = queryOf<"listScenarios">({
      project_id: "0190f3b4-0000-7000-8000-000000000001",
      limit: 200,
      severity: undefined,
      tag: "",
      include_archived: "1",
    });
    expect(q.toString()).toBe("project_id=0190f3b4-0000-7000-8000-000000000001&limit=200&include_archived=1");
  });
});
