package openapicheck

import (
	"bytes"
	"compress/gzip"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"regexp"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"testing/fstest"

	"github.com/santhosh-tekuri/jsonschema/v6"
)

const uuid = "0190f3b4-0000-7000-8000-000000000001"

// The document of the Python checker's tests (packages/core-py), so that the
// two checkers are held to the same cases.
const document = `{
  "openapi": "3.1.0",
  "info": {"title": "t", "version": "1"},
  "paths": {
    "/things": {
      "get": {
        "operationId": "listThings",
        "parameters": [
          {"name": "limit", "in": "query", "schema": {"type": "integer", "minimum": 1}},
          {"name": "X-Org", "in": "header", "schema": {"type": "string", "format": "uuid"}}
        ],
        "responses": {
          "200": {"$ref": "#/components/responses/Things"},
          "4XX": {"$ref": "#/components/responses/Error"}
        }
      },
      "post": {
        "operationId": "createThing",
        "requestBody": {
          "required": true,
          "content": {"application/json": {"schema": {"$ref": "#/components/schemas/NewThing"}}}
        },
        "responses": {
          "201": {
            "description": "created",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Thing"}}}
          },
          "default": {"$ref": "#/components/responses/Error"}
        }
      }
    },
    "/things/special": {
      "get": {"operationId": "specialThing", "responses": {"204": {"description": "nothing"}}}
    },
    "/things/{thing_id}": {
      "parameters": [
        {"name": "thing_id", "in": "path", "required": true, "schema": {"type": "string", "format": "uuid"}}
      ],
      "get": {
        "operationId": "getThing",
        "responses": {
          "200": {
            "description": "one",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ThingDetail"}}}
          }
        }
      }
    },
    "/raw": {
      "post": {
        "operationId": "raw",
        "responses": {"default": {"description": "any", "content": {"application/json": {"schema": {}}}}}
      }
    }
  },
  "webhooks": {
    "ping": {
      "post": {
        "operationId": "ping",
        "requestBody": {
          "required": true,
          "content": {
            "application/json": {
              "schema": {"type": "object", "required": ["n"], "properties": {"n": {"type": "integer"}, "run": {"type": "string", "format": "uuid"}}}
            }
          }
        },
        "responses": {
          "200": {
            "description": "pong",
            "content": {
              "application/json": {"schema": {"type": "object", "properties": {"pong": {"type": "boolean"}, "by": {"type": "string", "format": "uuid"}}}}
            }
          }
        }
      }
    }
  },
  "components": {
    "responses": {
      "Things": {
        "description": "things",
        "content": {
          "application/json": {
            "schema": {
              "type": "object",
              "required": ["items"],
              "properties": {"items": {"type": "array", "items": {"$ref": "#/components/schemas/Thing"}}}
            }
          }
        }
      },
      "Error": {
        "description": "error",
        "content": {
          "application/json": {
            "schema": {
              "type": "object",
              "required": ["error"],
              "properties": {
                "error": {"type": "object", "properties": {"code": {"type": "string"}, "details": {"type": "object"}}}
              }
            }
          }
        }
      }
    },
    "schemas": {
      "Thing": {
        "type": "object",
        "required": ["id", "at"],
        "properties": {
          "id": {"type": "string", "format": "uuid"},
          "at": {"type": "string", "format": "date-time"},
          "note": {"type": ["string", "null"]}
        }
      },
      "ThingDetail": {
        "allOf": [
          {"$ref": "#/components/schemas/Thing"},
          {
            "type": "object",
            "properties": {
              "kind": {"anyOf": [{"$ref": "#/components/schemas/Kind"}, {"type": "null"}]},
              "scenario": {"type": "object", "x-agenttwin-schema": "scenario.v1"},
              "faults": {
                "type": "array",
                "items": {"type": "object", "x-agenttwin-schema": "scenario.v1#/$defs/fault"}
              },
              "meta": {"type": "object", "additionalProperties": true, "properties": {"a": {}}},
              "shape": {
                "oneOf": [
                  {"type": "object", "required": ["r"], "properties": {"r": {"type": "number"}}},
                  {"type": "object", "required": ["w"], "properties": {"w": {"type": "number"}, "h": {"type": "number"}}}
                ]
              }
            }
          }
        ]
      },
      "Kind": {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}},
      "NewThing": {
        "type": "object",
        "additionalProperties": false,
        "required": ["name"],
        "properties": {"name": {"type": "string"}}
      }
    }
  }
}`

func parse(t testing.TB, raw string) map[string]any {
	t.Helper()
	v, err := jsonschema.UnmarshalJSON(strings.NewReader(raw))
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	return v.(map[string]any)
}

func contract(t testing.TB) *Contract {
	t.Helper()
	c, err := New(parse(t, document), "things")
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	return c
}

func thing(extra map[string]any) map[string]any {
	out := map[string]any{"id": uuid, "at": "2026-09-25T10:00:00.5Z", "note": nil}
	for k, v := range extra {
		out[k] = v
	}
	return out
}

func encode(t testing.TB, v any) []byte {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func respond(t testing.TB, c *Contract, method, path string, status int, v any) error {
	t.Helper()
	return c.CheckResponse(method, path, status, "application/json", encode(t, v))
}

// fails asserts err is a violation whose message matches every pattern.
func fails(t *testing.T, err error, patterns ...string) {
	t.Helper()
	if err == nil {
		t.Fatalf("no violation; want one matching %q", patterns)
	}
	var v *Violation
	if !errors.As(err, &v) {
		t.Fatalf("%T is not a *Violation: %v", err, err)
	}
	for _, p := range patterns {
		if !regexp.MustCompile(p).MatchString(err.Error()) {
			t.Fatalf("violation does not match %q:\n%v", p, err)
		}
	}
}

func passes(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Fatalf("unexpected violation:\n%v", err)
	}
}

// problemLines are the lines after the violation's first.
func problemLines(err error) []string {
	return strings.Split(err.Error(), "\n")[1:]
}

func TestStrictifyClosesDeclaredObjectsButNotAllOfMembers(t *testing.T) {
	schema := parse(t, `{
		"type": "object",
		"properties": {"a": {"type": "object", "properties": {"b": {}}}, "free": {"type": "object"}},
		"allOf": [{"properties": {"c": {}}}],
		"anyOf": [{"properties": {"d": {}}}, {"type": "null"}]
	}`)
	out := Strictify(schema).(map[string]any)
	if out["unevaluatedProperties"] != false {
		t.Fatal("the object is not closed")
	}
	props := out["properties"].(map[string]any)
	if props["a"].(map[string]any)["unevaluatedProperties"] != false {
		t.Fatal("a nested object is not closed")
	}
	if _, ok := props["free"].(map[string]any)["unevaluatedProperties"]; ok {
		t.Fatal("a free-form object was closed")
	}
	if _, ok := out["allOf"].([]any)[0].(map[string]any)["unevaluatedProperties"]; ok {
		t.Fatal("an allOf member was closed at its top level")
	}
	if out["anyOf"].([]any)[0].(map[string]any)["unevaluatedProperties"] != false {
		t.Fatal("an anyOf branch is not closed")
	}
	explicit := Strictify(parse(t, `{"properties": {"a": {}}, "additionalProperties": true}`)).(map[string]any)
	if _, ok := explicit["unevaluatedProperties"]; ok {
		t.Fatal("an object that says it is open was closed")
	}
	if _, ok := schema["properties"].(map[string]any)["a"].(map[string]any)["unevaluatedProperties"]; ok {
		t.Fatal("the input was modified")
	}
}

// `then`, `else` and `dependentSchemas` add to the object they sit in: their
// properties count as declared by it. Closed on their own, they would reject
// every other property of that object; a closed `if` or `not` would invert
// its condition.
func TestConditionalAndNegatedSubschemasKeepTheirMeaning(t *testing.T) {
	c := jsonschema.NewCompiler()
	if err := c.AddResource("mem:///s.json", Strictify(parse(t, `{
		"type": "object",
		"properties": {"kind": {"type": "string"}, "done": {"type": "boolean"}, "detail": {"type": "object"}},
		"if": {"properties": {"done": {"const": true}}},
		"then": {"properties": {"detail": {"required": ["at"], "properties": {"at": {"type": "string"}}}}},
		"else": {"properties": {"detail": {"maxProperties": 0}}},
		"dependentSchemas": {"kind": {"properties": {"kind": {"minLength": 1}}}},
		"not": {"properties": {"kind": {"const": "forbidden"}}, "required": ["kind"]}
	}`))); err != nil {
		t.Fatal(err)
	}
	s, err := c.Compile("mem:///s.json")
	if err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		instance string
		valid    bool
		why      string
	}{
		{`{"kind": "a", "done": true, "detail": {"at": "x"}}`, true, "a complete object"},
		{`{"kind": "a", "done": false, "detail": {}}`, true, "the else branch"},
		{`{"kind": "a", "done": true, "detail": {}}`, false, "then applies"},
		{`{"kind": "a", "done": false, "detail": {"at": "x"}}`, false, "else applies"},
		{`{"kind": "", "done": false}`, false, "dependentSchemas applies"},
		{`{"kind": "forbidden", "done": false}`, false, "not keeps its meaning"},
		{`{"kind": "a", "done": true, "detail": {"at": "x", "zz": 1}}`, false, "nested objects stay closed"},
		{`{"kind": "a", "extra": 1}`, false, "the object itself stays closed"},
	} {
		if err := s.Validate(parse(t, tc.instance)); (err == nil) != tc.valid {
			t.Errorf("%s: %s valid=%v, want %v (%v)", tc.why, tc.instance, err == nil, tc.valid, err)
		}
	}
}

func TestResponsesAreCheckedStrictlyThroughRefsAndAllOf(t *testing.T) {
	c := contract(t)
	passes(t, respond(t, c, "GET", "/things", 200, map[string]any{"items": []any{thing(nil)}}))
	passes(t, respond(t, c, "GET", "/things/"+uuid, 200, thing(map[string]any{
		"kind": map[string]any{"name": "k"}, "meta": map[string]any{"a": 1, "z": 2},
	})))
	fails(t, respond(t, c, "GET", "/things", 200, map[string]any{"items": []any{thing(map[string]any{"extra": 1})}}),
		`listThings 200 response does not match the contract`, `at /items/0/extra: undocumented field \(not in the contract\)`)
	fails(t, respond(t, c, "GET", "/things/"+uuid, 200, thing(map[string]any{"extra": 1})),
		`at /extra: undocumented field`)
	fails(t, respond(t, c, "GET", "/things", 200, map[string]any{"items": []any{map[string]any{"at": "2026-09-25T10:00:00Z"}}}),
		`missing property 'id'`)
}

func TestOnlyTheCauseIsReportedForAFailingDeclaringSubschema(t *testing.T) {
	c := contract(t)
	// kind carries an undocumented field: the allOf member that declares kind
	// and meta fails, so both are left unevaluated at the top level too. They
	// are documented, so only the cause is reported.
	err := respond(t, c, "GET", "/things/"+uuid, 200, thing(map[string]any{
		"kind": map[string]any{"name": "k", "x": 1}, "meta": map[string]any{"a": 1},
	}))
	fails(t, err, `at /kind/x: undocumented field`)
	for _, line := range problemLines(err) {
		if strings.Contains(line, "/meta") || strings.HasSuffix(line, "at /kind: undocumented field (not in the contract)") {
			t.Fatalf("a documented field is reported as undocumented:\n%v", err)
		}
	}
}

func TestAFieldOfAnotherVariantIsReportedAsSuch(t *testing.T) {
	c := contract(t)
	passes(t, respond(t, c, "GET", "/things/"+uuid, 200, thing(map[string]any{"shape": map[string]any{"w": 1, "h": 2}})))
	// r belongs to the other variant: documented, but not for this one.
	err := respond(t, c, "GET", "/things/"+uuid, 200, thing(map[string]any{"shape": map[string]any{"w": 1, "r": 2}}))
	fails(t, err, `at /shape/r: field of a variant this value does not match`, `at /shape/w: field of a variant`)
	if lines := problemLines(err); len(lines) != 2 {
		t.Fatalf("the object of the fields is reported too:\n%v", err)
	}
	fails(t, respond(t, c, "GET", "/things/"+uuid, 200, thing(map[string]any{"shape": map[string]any{"w": 1, "q": 2}})),
		`at /shape/q: undocumented field`)
}

func TestFormatsAreEnforced(t *testing.T) {
	c := contract(t)
	for _, bad := range []map[string]any{
		{"id": strings.ToUpper(uuid)},
		{"id": "not-a-uuid"},
		{"at": "2026-09-25 10:00:00"},
		{"at": "2026-02-30T10:00:00Z"},
		{"at": "2026-09-25T10:00:00"},
	} {
		fails(t, respond(t, c, "GET", "/things", 200, map[string]any{"items": []any{thing(bad)}}), `at /items/0/(id|at)`)
	}
	passes(t, respond(t, c, "GET", "/things", 200, map[string]any{"items": []any{thing(map[string]any{"at": "2026-09-25T10:00:00+03:00"})}}))
}

var scenario = map[string]any{
	"apiVersion": "agenttwin.dev/v1",
	"kind":       "Scenario",
	"metadata":   map[string]any{"name": "s", "severity": "low"},
	"spec": map[string]any{
		"input":        map[string]any{"message": "hi"},
		"expectations": []any{map[string]any{"type": "toolCalled", "tool": "t"}},
	},
}

func TestEmbeddedDocumentsAreCheckedAgainstTheirSchema(t *testing.T) {
	c := contract(t)
	passes(t, respond(t, c, "GET", "/things/"+uuid, 200, thing(map[string]any{"scenario": scenario})))
	broken := map[string]any{}
	for k, v := range scenario {
		broken[k] = v
	}
	broken["metadata"] = map[string]any{"name": "s", "severity": "urgent"}
	fails(t, respond(t, c, "GET", "/things/"+uuid, 200, thing(map[string]any{"scenario": broken})),
		`at /scenario: is not a valid scenario\.v1 document: metadata/severity: `)
}

func TestEmbeddedDefinitionsAreCheckedAgainstTheirPartOfTheSchema(t *testing.T) {
	c := contract(t)
	fault := func(over map[string]any) map[string]any {
		out := map[string]any{"target": "refund", "when": map[string]any{"callNumber": 2}, "behavior": map[string]any{"type": "timeout_after_mutation"}}
		for k, v := range over {
			out[k] = v
		}
		return out
	}
	passes(t, respond(t, c, "GET", "/things/"+uuid, 200, thing(map[string]any{"faults": []any{fault(nil)}})))
	for _, tc := range []struct {
		bad   map[string]any
		where string
	}{
		{map[string]any{"behavior": map[string]any{"type": "delay"}}, `\(root\): missing property 'target'`},
		{fault(map[string]any{"behavior": map[string]any{"type": "meteor_strike"}}), `behavior/type`},
		{fault(map[string]any{"when": map[string]any{"callNumber": 0}}), `when/callNumber`},
	} {
		err := respond(t, c, "GET", "/things/"+uuid, 200, thing(map[string]any{"faults": []any{tc.bad}}))
		fails(t, err, `at /faults/0: is not a valid scenario\.v1 fault: `+tc.where)
		for _, line := range problemLines(err) {
			if !strings.HasPrefix(line, "  at /faults/0: is not a valid scenario.v1 fault: ") {
				t.Fatalf("not only the cause is reported:\n%v", err)
			}
		}
	}
	// An undocumented field next to a failing one is still reported.
	fails(t, respond(t, c, "GET", "/things/"+uuid, 200, thing(map[string]any{"faults": []any{map[string]any{}}, "surprise": 1})),
		`at /surprise: undocumented field`, `at /faults/0: is not a valid scenario\.v1 fault`)
}

func TestEmbeddedSchemaNamesAreChecked(t *testing.T) {
	s, err := Embedded("scenario.v1#/$defs/fault")
	if err != nil {
		t.Fatal(err)
	}
	if err := s.Validate(map[string]any{"target": "t", "behavior": map[string]any{"type": "delay", "delayMs": json.Number("5")}}); err != nil {
		t.Fatalf("a valid fault is rejected: %v", err)
	}
	if _, err := Embedded("agent-manifest.v1"); err != nil {
		t.Fatal(err)
	}
	if _, err := Embedded("scenario.v1#/$defs/meteor"); err == nil || !strings.Contains(err.Error(), "scenario.v1 has no definition meteor") {
		t.Fatalf("an unknown definition: %v", err)
	}
	for _, bad := range []string{"scenario", "scenario.v1#/properties/spec", "../x.v1", "Scenario.v1"} {
		if _, err := Embedded(bad); err == nil || !strings.Contains(err.Error(), "invalid x-agenttwin-schema") {
			t.Fatalf("%q: %v", bad, err)
		}
	}
	if _, err := Embedded("nothing.v1"); err == nil || !strings.Contains(err.Error(), "no canonical schema nothing.v1") {
		t.Fatalf("an unknown document: %v", err)
	}
	// A contract that names what the checker cannot validate does not load.
	doc := parse(t, `{"openapi": "3.1.0", "paths": {}, "components": {"schemas": {"X": {"x-agenttwin-schema": "scenario.v1#/$defs/meteor"}}}}`)
	if _, err := New(doc, "x"); err == nil || !strings.Contains(err.Error(), "no definition meteor") {
		t.Fatalf("a broken marker loaded: %v", err)
	}
}

func TestLiteralPathsWinAndStatusesFallBackToRangesAndDefault(t *testing.T) {
	c := contract(t)
	if op, _, ok := c.Find("GET", "/things/special"); !ok || op.ID != "specialThing" {
		t.Fatalf("literal path: %v", op)
	}
	if op, values, ok := c.Find("get", "/things/"+uuid); !ok || op.ID != "getThing" || values["thing_id"] != uuid {
		t.Fatalf("templated path: %v %v", op, values)
	}
	if _, values, _ := c.Find("GET", "/things/a%2Fb"); values["thing_id"] != "a/b" {
		t.Fatalf("path values are unescaped: %v", values)
	}
	for _, p := range []string{"/things//", "/things/", "/things/" + uuid + "/x"} {
		if _, _, ok := c.Find("GET", p); ok {
			t.Fatalf("%s matched", p)
		}
	}
	if _, _, ok := c.Find("DELETE", "/things"); ok {
		t.Fatal("an undocumented method matched")
	}
	passes(t, c.CheckResponse("GET", "/things/special", 204, "", nil))
	fails(t, c.CheckResponse("GET", "/things/special", 204, "application/json", []byte("{}")), `no body is documented`)
	passes(t, respond(t, c, "GET", "/things", 404, map[string]any{"error": map[string]any{"code": "NOT_FOUND"}}))    // 4XX
	passes(t, respond(t, c, "POST", "/things", 503, map[string]any{"error": map[string]any{"code": "UNAVAILABLE"}})) // default
	fails(t, respond(t, c, "GET", "/things", 500, map[string]any{"error": map[string]any{}}), `answered 500, which is not documented`)
	fails(t, respond(t, c, "GET", "/nothing", 200, map[string]any{}), `GET /nothing is not a documented operation`)
	fails(t, c.CheckResponse("GET", "/things", 200, "text/plain", []byte("x")), `content type text/plain is not documented`)
	fails(t, c.CheckResponse("GET", "/things", 200, "", []byte("x")), `content type \(none\) is not documented`)
	fails(t, c.CheckResponse("GET", "/things", 200, "application/json; charset=utf-8", []byte("{nope")), `the body is not JSON`)
	// An "any" schema accepts any body, even one that is not JSON.
	passes(t, c.CheckResponse("POST", "/raw", 502, "application/json", []byte("{nope")))
}

func TestAcceptedRequestsMustMatchTheDocumentedParametersAndBody(t *testing.T) {
	c := contract(t)
	header := http.Header{"X-Org": {uuid}, "Authorization": {"x"}}
	passes(t, c.CheckRequest("GET", "/things", url.Values{"limit": {"5"}}, header, "", nil))
	passes(t, c.CheckRequest("POST", "/things", nil, nil, "application/json", encode(t, map[string]any{"name": "n"})))
	fails(t, c.CheckRequest("GET", "/things", url.Values{"q": {"x"}}, nil, "", nil), `undocumented query parameter "q"`)
	fails(t, c.CheckRequest("GET", "/things", url.Values{"limit": {"0"}}, nil, "", nil), `query parameter "limit"="0"`)
	fails(t, c.CheckRequest("GET", "/things", url.Values{"limit": {"five"}}, nil, "", nil), `query parameter "limit"="five"`)
	fails(t, c.CheckRequest("GET", "/things", nil, http.Header{"X-Org": {"nope"}}, "", nil), `header parameter "x-org"`)
	fails(t, c.CheckRequest("GET", "/things/nope", nil, nil, "", nil), `path parameter "thing_id"`)
	fails(t, c.CheckRequest("POST", "/things", nil, nil, "", nil), `requires a request body`)
	fails(t, c.CheckRequest("POST", "/things", nil, nil, "application/json", encode(t, map[string]any{"name": "n", "x": 1})),
		`createThing request does not match the contract`, `additional properties 'x' not allowed`)
	fails(t, c.CheckRequest("POST", "/things", nil, nil, "text/plain", []byte("n")), `content type text/plain is not documented`)
	fails(t, c.CheckRequest("GET", "/things", nil, nil, "application/json", []byte("{}")), `documents no request body`)
}

func TestCoverageCountsSuccessfulAnswersOnly(t *testing.T) {
	c := contract(t)
	want := []string{"createThing", "getThing", "listThings", "ping", "raw", "specialThing"}
	if got := c.Uncovered(); strings.Join(got, ",") != strings.Join(want, ",") {
		t.Fatalf("uncovered: %v", got)
	}
	passes(t, respond(t, c, "GET", "/things", 200, map[string]any{"items": []any{}}))
	passes(t, respond(t, c, "POST", "/things", 400, map[string]any{"error": map[string]any{"code": "INVALID_REQUEST"}}))
	_ = respond(t, c, "GET", "/things/"+uuid, 200, thing(map[string]any{"extra": 1})) // a failing check counts for nothing
	got := strings.Join(c.Uncovered(), ",")
	if strings.Contains(got, "listThings") || !strings.Contains(got, "createThing") || !strings.Contains(got, "getThing") {
		t.Fatalf("uncovered: %v", got)
	}
	if routes := strings.Join(c.Routes(), ","); routes != "GET /things,GET /things/special,GET /things/{thing_id},POST /raw,POST /things" {
		t.Fatalf("routes: %s", routes)
	}
}

func TestCheckSchemaAndLoad(t *testing.T) {
	c := contract(t)
	passes(t, c.CheckSchema("Thing", thing(nil)))
	fails(t, c.CheckSchema("Thing", map[string]any{"id": uuid}), `things: not a valid Thing`, `missing property 'at'`)
	// Go values are checked as their JSON: a struct with a stray field fails.
	type wire struct {
		ID    string `json:"id"`
		At    string `json:"at"`
		Stray int    `json:"stray"`
	}
	fails(t, c.CheckSchema("Thing", wire{ID: uuid, At: "2026-09-25T10:00:00Z"}), `at /stray: undocumented field`)

	fsys := fstest.MapFS{
		"openapi/things.openapi.yaml": {Data: []byte("openapi: 3.1.0\ninfo: {title: t, version: '1'}\npaths:\n  /x:\n    get:\n      operationId: x\n      responses:\n        '200': {description: ok}\n")},
		"openapi/old.openapi.yaml":    {Data: []byte("openapi: 3.0.3\npaths: {}\n")},
		"openapi/bad.openapi.yaml":    {Data: []byte("openapi: [\n")},
		"openapi/noid.openapi.yaml":   {Data: []byte("openapi: 3.1.0\npaths:\n  /x:\n    get:\n      responses: {}\n")},
	}
	loaded, err := Load(fsys, "openapi/things.openapi.yaml")
	if err != nil || loaded.Name != "things" || len(loaded.Operations()) != 1 {
		t.Fatalf("load: %v %v", loaded, err)
	}
	for name, want := range map[string]string{
		"openapi/old.openapi.yaml":    `not an OpenAPI 3\.1 document`,
		"openapi/bad.openapi.yaml":    `parse openapi/bad`,
		"openapi/noid.openapi.yaml":   `GET /x has no operationId`,
		"openapi/absent.openapi.yaml": `read openapi/absent`,
	} {
		if _, err := Load(fsys, name); err == nil || !regexp.MustCompile(want).MatchString(err.Error()) {
			t.Fatalf("%s: %v", name, err)
		}
	}
	cyclic := parse(t, document)
	cyclic["components"].(map[string]any)["schemas"].(map[string]any)["Thing"] = map[string]any{"$ref": "#/components/schemas/Thing"}
	cc, err := New(cyclic, "cyclic")
	if err != nil {
		t.Fatal(err)
	}
	fails(t, cc.CheckSchema("Thing", map[string]any{}), `does not compile: cyclic \$ref`)
}

func TestWebhookCallsAreChecked(t *testing.T) {
	c := contract(t)
	passes(t, c.CheckWebhook("ping", "application/json", []byte(`{"n":1}`), 200, "application/json", []byte(`{"pong":true}`)))
	if strings.Join(c.Uncovered(), ",") != "createThing,getThing,listThings,raw,specialThing" {
		t.Fatalf("uncovered: %v", c.Uncovered())
	}
	fails(t, c.CheckWebhook("ping", "application/json", []byte(`{"n":"one"}`), 0, "", nil), `ping request does not match`)
	fails(t, c.CheckWebhook("ping", "application/json", []byte(`{"n":2}`), 200, "application/json", []byte(`{"pong":"yes"}`)), `ping 200 response`)
	fails(t, c.CheckWebhook("pong", "application/json", nil, 0, "", nil), `no webhook "pong" is documented`)
	// A request the agent never answered (status 0) is checked on its own.
	passes(t, c.CheckWebhook("ping", "application/json", []byte(`{"n":3}`), 0, "", nil))
}

func TestCheckExchangeChecksAcceptedRequestsOnly(t *testing.T) {
	c := contract(t)
	req := httptest.NewRequest("POST", "/things", bytes.NewReader([]byte(`{"name":"n","x":1}`)))
	req.Header.Set("Content-Type", "application/json")
	json400 := http.Header{"Content-Type": {"application/json"}}
	// The service rejected the request: only its answer is held to the contract.
	passes(t, c.CheckExchange(req, []byte(`{"name":"n","x":1}`), 400, json400, []byte(`{"error":{"code":"INVALID_REQUEST"}}`)))
	// Accepting it would be a violation: the contract does not allow x.
	fails(t, c.CheckExchange(req, []byte(`{"name":"n","x":1}`), 201, json400, encode(t, thing(nil))), `additional properties 'x' not allowed`)
	if strings.Contains(strings.Join(c.Uncovered(), ","), "createThing") == false {
		t.Fatal("a failed exchange counted as coverage")
	}
	ok := httptest.NewRequest("POST", "/things", nil)
	ok.Header.Set("Content-Type", "application/json")
	passes(t, c.CheckExchange(ok, []byte(`{"name":"n"}`), 201, json400, encode(t, thing(nil))))
	if strings.Contains(strings.Join(c.Uncovered(), ","), "createThing") {
		t.Fatal("a checked exchange did not count as coverage")
	}
}

func TestAnExchangeReportsTheRequestAndTheResponseTogether(t *testing.T) {
	// A fake that echoes what it was sent answers with the request's mistake:
	// both sides are reported, the request (the cause) first.
	c := contract(t)
	req := httptest.NewRequest("POST", "/things", nil)
	req.Header.Set("Content-Type", "application/json")
	json201 := http.Header{"Content-Type": {"application/json"}}
	err := c.CheckExchange(req, []byte(`{"name":"n","x":1}`), 201, json201, encode(t, thing(map[string]any{"x": 1})))
	fails(t, err, `(?s)createThing request does not match.*createThing 201 response does not match`)
	if strings.Contains(strings.Join(c.Uncovered(), ","), "createThing") == false {
		t.Fatal("a failed exchange counted as coverage")
	}
	// A body that cannot be decoded is reported on the side it came from.
	req.Header.Set("Content-Encoding", "gzip")
	fails(t, c.CheckExchange(req, []byte(`{"name":"n"}`), 201, json201, encode(t, thing(nil))),
		`^things: createThing request: the body is not valid gzip`)
}

func gzipped(t testing.TB, b []byte) []byte {
	t.Helper()
	var buf bytes.Buffer
	zw := gzip.NewWriter(&buf)
	if _, err := zw.Write(b); err != nil {
		t.Fatal(err)
	}
	if err := zw.Close(); err != nil {
		t.Fatal(err)
	}
	return buf.Bytes()
}

// A schema describes the representation, not its coding on the wire: gzip
// bodies are checked decoded, and a coding the checker cannot undo is a
// violation, not a pass.
func TestContentCodingsAreUndoneBeforeTheCheck(t *testing.T) {
	c := contract(t)
	json201 := http.Header{"Content-Type": {"application/json"}}
	req := httptest.NewRequest("POST", "/things", nil)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Content-Encoding", "gzip")
	passes(t, c.CheckExchange(req, gzipped(t, []byte(`{"name":"n"}`)), 201, json201, encode(t, thing(nil))))
	fails(t, c.CheckExchange(req, gzipped(t, []byte(`{"name":"n","x":1}`)), 201, json201, encode(t, thing(nil))),
		`additional properties 'x' not allowed`)
	fails(t, c.CheckExchange(req, []byte(`{"name":"n"}`), 201, json201, encode(t, thing(nil))), `createThing request: the body is not valid gzip`)
	req.Header.Set("Content-Encoding", "br")
	fails(t, c.CheckExchange(req, []byte(`{"name":"n"}`), 201, json201, encode(t, thing(nil))), `content coding "br" cannot be checked`)
	gz := http.Header{"Content-Type": {"application/json"}, "Content-Encoding": {"gzip"}}
	ok := httptest.NewRequest("GET", "/things", nil)
	passes(t, c.CheckExchange(ok, nil, 200, gz, gzipped(t, encode(t, map[string]any{"items": []any{thing(nil)}}))))
	fails(t, c.CheckExchange(ok, nil, 200, gz, gzipped(t, encode(t, map[string]any{}))), `listThings 200 response`, `'items'`)
}

// Checking holds every exchange a server makes on a documented operation to
// the contract — including a request body the handler never read — and
// leaves undocumented paths alone.
func TestCheckingHoldsEveryServedExchangeToTheContract(t *testing.T) {
	c := contract(t)
	var mu sync.Mutex
	var reported []string
	var answer atomic.Value
	answer.Store(`{"id":"` + uuid + `","at":"2026-09-25T10:00:00Z","note":null}`)
	srv := httptest.NewServer(c.Checking(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/things":
			// Answers without reading the body: Checking reads the rest itself.
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusCreated)
			_, _ = io.WriteString(w, answer.Load().(string))
		default:
			w.WriteHeader(http.StatusTeapot)
		}
	}), func(err error) {
		mu.Lock()
		defer mu.Unlock()
		reported = append(reported, err.Error())
	}))
	defer srv.Close()
	post := func(body string) {
		t.Helper()
		res, err := srv.Client().Post(srv.URL+"/things", "application/json", strings.NewReader(body))
		if err != nil {
			t.Fatal(err)
		}
		_, _ = io.Copy(io.Discard, res.Body)
		_ = res.Body.Close()
	}
	take := func() []string {
		mu.Lock()
		defer mu.Unlock()
		out := reported
		reported = nil
		return out
	}
	post(`{"name":"n"}`)
	if got := take(); len(got) != 0 || strings.Contains(strings.Join(c.Uncovered(), ","), "createThing") {
		t.Fatalf("a valid exchange was reported or not recorded: %v", got)
	}
	post(`{"name":"n","x":1}`)
	if got := take(); len(got) != 1 || !strings.Contains(got[0], `additional properties 'x' not allowed`) {
		t.Fatalf("an accepted request the contract forbids: %v", got)
	}
	answer.Store(`{"id":"` + uuid + `"}`)
	post(`{"name":"n"}`)
	if got := take(); len(got) != 1 || !strings.Contains(got[0], "createThing 201 response") {
		t.Fatalf("a response the contract forbids: %v", got)
	}
	res, err := srv.Client().Get(srv.URL + "/undocumented")
	if err != nil {
		t.Fatal(err)
	}
	_ = res.Body.Close()
	if got := take(); res.StatusCode != http.StatusTeapot || len(got) != 0 {
		t.Fatalf("an undocumented path was checked or changed: %d %v", res.StatusCode, got)
	}
}

// UUIDs are case-insensitive on input and lower-case on output (RFC 9562): a
// value the service reads may be in either case, one it writes must be
// canonical. For a webhook the service writes the request and reads the answer.
func TestUUIDsAreCaseInsensitiveOnlyWhereTheServiceReadsThem(t *testing.T) {
	c := contract(t)
	upper := strings.ToUpper(uuid)
	passes(t, c.CheckRequest("GET", "/things/"+upper, nil, nil, "", nil))
	passes(t, c.CheckRequest("GET", "/things", nil, http.Header{"X-Org": {upper}}, "", nil))
	fails(t, c.CheckRequest("GET", "/things/"+uuid[:35], nil, nil, "", nil), `not a hyphenated UUID`)
	fails(t, c.CheckResponse("GET", "/things/"+uuid, 200, "application/json", encode(t, thing(map[string]any{"id": upper}))),
		`at /id: .* is not valid uuid: not a canonical \(lower-case\) UUID`)
	passes(t, c.CheckWebhook("ping", "application/json", encode(t, map[string]any{"n": 1, "run": uuid}), 200, "application/json",
		encode(t, map[string]any{"by": upper})))
	fails(t, c.CheckWebhook("ping", "application/json", encode(t, map[string]any{"n": 1, "run": upper}), 0, "", nil),
		`ping request does not match the contract`, `not a canonical \(lower-case\) UUID`)
}
