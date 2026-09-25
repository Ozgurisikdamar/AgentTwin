package server_test

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"slices"
	"strings"
	"sync"
	"testing"
)

const paymentsAPI = `
openapi: 3.1.0
info: {title: Payments API, version: 3.0.0}
servers: [{url: 'https://svc:hunter2@payments.internal/v3?key=abc'}]
paths:
  /refunds:
    post:
      operationId: refundPayment
      x-agenttwin-tool: refund_payment
      summary: Refund a payment
      requestBody:
        required: true
        content:
          application/json:
            schema: {$ref: '#/components/schemas/Refund'}
      responses: {'201': {description: refunded}}
  /payments/{payment_id}:
    get:
      operationId: getPayment
      parameters: [{$ref: '#/components/parameters/PaymentId'}]
      responses: {'200': {description: ok}}
  /payments/{payment_id}/capture:
    post:
      operationId: capturePayment
      parameters: [{$ref: '#/components/parameters/PaymentId'}]
      responses: {'200': {description: captured}}
components:
  parameters:
    PaymentId: {name: payment_id, in: path, required: true, schema: {type: string}}
  schemas:
    Refund:
      type: object
      required: [order_id, amount]
      properties:
        order_id: {type: string}
        amount: {type: number, exclusiveMinimum: 0}
`

func (h *harness) importOpenAPI(tok, projectID string, body map[string]any) resp {
	h.t.Helper()
	return h.request("POST", "/api/v1/projects/"+projectID+"/imports/openapi", body, bearer(tok))
}

func (h *harness) importMCP(tok, projectID string, body map[string]any) resp {
	h.t.Helper()
	return h.request("POST", "/api/v1/projects/"+projectID+"/imports/mcp", body, bearer(tok))
}

type catalogEntry struct {
	Name, Risk, RiskSource, Registry, RegistryNote, HintRisk string
	ToolVersion                                              int
	Mutating                                                 bool
}

func entriesOf(t *testing.T, r resp) map[string]catalogEntry {
	t.Helper()
	var body struct {
		Entries []struct {
			Name         string `json:"name"`
			Risk         string `json:"risk"`
			RiskSource   string `json:"risk_source"`
			Registry     string `json:"registry"`
			RegistryNote string `json:"registry_note"`
			HintRisk     string `json:"hint_risk"`
			ToolVersion  int    `json:"tool_version"`
			Mutating     bool   `json:"mutating"`
		} `json:"entries"`
	}
	if err := json.Unmarshal(r.Raw, &body); err != nil || body.Entries == nil {
		t.Fatalf("no entries: %d %s", r.Status, r.Raw)
	}
	out := map[string]catalogEntry{}
	for _, e := range body.Entries {
		out[e.Name] = catalogEntry{e.Name, e.Risk, e.RiskSource, e.Registry, e.RegistryNote, e.HintRisk, e.ToolVersion, e.Mutating}
	}
	return out
}

func (h *harness) tool(tok, projectID, name string) map[string]any {
	h.t.Helper()
	for _, it := range items(h.t, h.request("GET", "/api/v1/projects/"+projectID+"/tools", nil, bearer(tok))) {
		if it["name"] == name {
			return it
		}
	}
	return nil
}

func TestAnOpenAPIImportFillsTheRegistryWithoutOverridingManifests(t *testing.T) {
	h := newHarness(t, nil)
	pid := h.s.Demo.ProjectID
	eng := h.login("engineer@demo.agenttwin.dev")
	h.registerDemo(eng, pid, "1.3.1")
	refundBefore := h.tool(eng, pid, "refund_payment")

	r := h.importOpenAPI(eng, pid, map[string]any{"name": "payments-api", "service": "payments-api", "document": paymentsAPI})
	if r.Status != http.StatusCreated || r.Body["created"] != true || r.Body["revision"] != 1.0 {
		t.Fatalf("import: %d %s", r.Status, r.Raw)
	}
	first := r.Body
	got := entriesOf(t, r)
	want := map[string]catalogEntry{
		"capture_payment": {Name: "capture_payment", Risk: "WRITE_IRREVERSIBLE", RiskSource: "inferred", Registry: "created", ToolVersion: 1, Mutating: true},
		"get_payment":     {Name: "get_payment", Risk: "READ", RiskSource: "inferred", Registry: "created", ToolVersion: 1},
		"refund_payment": {Name: "refund_payment", Risk: "WRITE_IRREVERSIBLE", RiskSource: "inferred", Registry: "kept_manifest",
			RegistryNote: "an agent manifest declares this tool", Mutating: true},
	}
	for name, w := range want {
		if got[name] != w {
			t.Errorf("%s = %+v, want %+v", name, got[name], w)
		}
	}
	if len(got) != 3 {
		t.Errorf("entries = %v", got)
	}
	if s := strs(first["servers"]); !slices.Equal(s, []string{"https://payments.internal/v3"}) {
		t.Errorf("servers keep no credentials: %v", s)
	}
	if strings.Contains(string(r.Raw), "hunter2") {
		t.Fatal("a credential of the document reached the catalog")
	}

	// The registry: new tools from the API, the manifest's tool untouched.
	capture := h.tool(eng, pid, "capture_payment")
	if capture["source"] != "OPENAPI" || capture["risk"] != "WRITE_IRREVERSIBLE" || capture["latest_version"] != 1.0 {
		t.Errorf("capture_payment = %v", capture)
	}
	if refund := h.tool(eng, pid, "refund_payment"); refund["latest_version"] != refundBefore["latest_version"] || refund["source"] != "MANIFEST" {
		t.Errorf("the manifest's refund_payment changed: %v → %v", refundBefore, refund)
	}

	// The graph hears of every tool, the kept one too: the call is a fact.
	var payload map[string]any
	if err := h.pool.QueryRow(context.Background(), `SELECT envelope->'payload' FROM control.outbox
		WHERE event_type = 'tool.catalog_imported.v1'`).Scan(&payload); err != nil {
		t.Fatal(err)
	}
	if payload["source"] != "OPENAPI" || payload["source_name"] != "payments-api" || payload["service"] != "payments-api" ||
		payload["catalog_id"] != first["id"] || len(payload["tools"].([]any)) != 3 {
		t.Errorf("event = %v", payload)
	}

	// The same document again, as a JSON object this time: the same
	// catalog, nothing stored, no event.
	again := h.importOpenAPI(eng, pid, map[string]any{"name": "payments-api", "service": "payments-api", "document": openAPIAsObject(t)})
	if again.Status != http.StatusOK || again.Body["created"] != false || again.Body["id"] != first["id"] {
		t.Fatalf("same import: %d %s", again.Status, again.Raw)
	}

	// A decision of the importer is a new revision: the tool gets a new
	// version, the others are unchanged.
	second := h.importOpenAPI(eng, pid, map[string]any{"name": "payments-api", "service": "payments-api", "document": paymentsAPI,
		"risk_overrides": map[string]string{"capturePayment": "WRITE_REVERSIBLE"}})
	if second.Status != http.StatusCreated || second.Body["revision"] != 2.0 {
		t.Fatalf("second revision: %d %s", second.Status, second.Raw)
	}
	e2 := entriesOf(t, second)
	if c := e2["capture_payment"]; c.Registry != "updated" || c.ToolVersion != 2 || c.Risk != "WRITE_REVERSIBLE" || c.RiskSource != "override" {
		t.Errorf("capture_payment = %+v", c)
	}
	if g := e2["get_payment"]; g.Registry != "unchanged" || g.ToolVersion != 1 {
		t.Errorf("get_payment = %+v", g)
	}
	if n := h.count(`SELECT count(*) FROM control.outbox WHERE event_type = 'tool.catalog_imported.v1'`); n != 2 {
		t.Errorf("events = %d, want 2", n)
	}
	if n := h.count(`SELECT count(*) FROM control.audit_event WHERE action = 'tool_catalog.imported'`); n != 2 {
		t.Errorf("audit entries = %d, want 2", n)
	}

	// Reading them back.
	list := h.request("GET", "/api/v1/projects/"+pid+"/tool-catalogs", nil, bearer(eng))
	if ids := items(t, list); len(ids) != 2 || ids[0]["revision"] != 2.0 || ids[1]["revision"] != 1.0 {
		t.Fatalf("list: %s", list.Raw)
	}
	if r := h.request("GET", "/api/v1/projects/"+pid+"/tool-catalogs?source=MCP", nil, bearer(eng)); len(items(t, r)) != 0 {
		t.Errorf("source filter: %s", r.Raw)
	}
	if r := h.request("GET", "/api/v1/projects/"+pid+"/tool-catalogs?name=payments-api&limit=1", nil, bearer(eng)); len(items(t, r)) != 1 || r.Body["next_cursor"] == nil {
		t.Errorf("name filter and paging: %s", r.Raw)
	}
	one := h.request("GET", "/api/v1/tool-catalogs/"+first["id"].(string), nil, bearer(h.login("viewer@demo.agenttwin.dev")))
	if one.Status != 200 || len(entriesOf(t, one)) != 3 {
		t.Fatalf("get as viewer: %d %s", one.Status, one.Raw)
	}
	if _, has := one.Body["created"]; has {
		t.Error("a read catalog carries created")
	}
	if _, err := h.pool.Exec(context.Background(), `UPDATE control.tool_catalog SET title = 'x'`); err == nil || !strings.Contains(err.Error(), "append-only") {
		t.Fatalf("a catalog was rewritten: %v", err)
	}
}

func openAPIAsObject(t *testing.T) map[string]any {
	t.Helper()
	// The document above, as JSON: the same content in the other form.
	const asJSON = `{"openapi":"3.1.0","info":{"title":"Payments API","version":"3.0.0"},
	"servers":[{"url":"https://svc:hunter2@payments.internal/v3?key=abc"}],
	"paths":{
	 "/refunds":{"post":{"operationId":"refundPayment","x-agenttwin-tool":"refund_payment","summary":"Refund a payment",
	   "requestBody":{"required":true,"content":{"application/json":{"schema":{"$ref":"#/components/schemas/Refund"}}}},
	   "responses":{"201":{"description":"refunded"}}}},
	 "/payments/{payment_id}":{"get":{"operationId":"getPayment","parameters":[{"$ref":"#/components/parameters/PaymentId"}],"responses":{"200":{"description":"ok"}}}},
	 "/payments/{payment_id}/capture":{"post":{"operationId":"capturePayment","parameters":[{"$ref":"#/components/parameters/PaymentId"}],"responses":{"200":{"description":"captured"}}}}},
	"components":{"parameters":{"PaymentId":{"name":"payment_id","in":"path","required":true,"schema":{"type":"string"}}},
	 "schemas":{"Refund":{"type":"object","required":["order_id","amount"],"properties":{"order_id":{"type":"string"},"amount":{"type":"number","exclusiveMinimum":0}}}}}}`
	var m map[string]any
	if err := json.Unmarshal([]byte(asJSON), &m); err != nil {
		t.Fatal(err)
	}
	return m
}

var deskTools = []map[string]any{
	{"name": "getTicket", "description": "Reads a ticket.", "annotations": map[string]any{"readOnlyHint": true},
		"inputSchema": map[string]any{"type": "object", "properties": map[string]any{"id": map[string]any{"type": "string"}}}},
	{"name": "send_email", "description": "Sends an email.", "inputSchema": map[string]any{"type": "object"}},
	{"name": "closeTicket", "description": "Closes a ticket.", "annotations": map[string]any{"destructiveHint": false},
		"inputSchema": map[string]any{"type": "object"}},
}

func TestAnMCPImportRecordsHintsAndTrustsThemOnlyWhenAsked(t *testing.T) {
	h := newHarness(t, nil)
	pid := h.s.Demo.ProjectID
	eng := h.login("engineer@demo.agenttwin.dev")
	h.registerDemo(eng, pid, "1.3.1")
	server := map[string]any{"name": "support-desk", "url": "https://secret-token@mcp.example.com/mcp?key=k", "version": "2.1.0"}
	r := h.importMCP(eng, pid, map[string]any{"server": server, "protocol_version": "2026-07-28", "tools": deskTools})
	if r.Status != http.StatusCreated {
		t.Fatalf("import: %d %s", r.Status, r.Raw)
	}
	got := entriesOf(t, r)
	want := map[string]catalogEntry{
		"get_ticket":   {Name: "get_ticket", Risk: "WRITE_IRREVERSIBLE", RiskSource: "default", HintRisk: "READ", Registry: "created", ToolVersion: 1, Mutating: true},
		"close_ticket": {Name: "close_ticket", Risk: "WRITE_IRREVERSIBLE", RiskSource: "default", HintRisk: "WRITE_REVERSIBLE", Registry: "created", ToolVersion: 1, Mutating: true},
		"send_email": {Name: "send_email", Risk: "WRITE_IRREVERSIBLE", RiskSource: "default", Registry: "kept_manifest",
			RegistryNote: "an agent manifest declares this tool", Mutating: true},
	}
	for name, w := range want {
		if got[name] != w {
			t.Errorf("%s = %+v, want %+v", name, got[name], w)
		}
	}
	if s := strs(r.Body["servers"]); !slices.Equal(s, []string{"https://mcp.example.com/mcp"}) || strings.Contains(string(r.Raw), "secret-token") {
		t.Errorf("servers = %v", s)
	}
	if r.Body["spec_version"] != "2026-07-28" || r.Body["title"] != "support-desk" || len(r.Body["warnings"].([]any)) != 0 {
		t.Errorf("header = %s", r.Raw)
	}
	var payload map[string]any
	if err := h.pool.QueryRow(context.Background(), `SELECT envelope->'payload' FROM control.outbox
		WHERE event_type = 'tool.catalog_imported.v1'`).Scan(&payload); err != nil {
		t.Fatal(err)
	}
	if payload["source"] != "MCP" || payload["source_name"] != "support-desk" || payload["service"] != nil {
		t.Errorf("event = %v", payload)
	}

	trusted := h.importMCP(eng, pid, map[string]any{"server": server, "protocol_version": "2026-07-28", "tools": deskTools, "trust_annotations": true})
	if trusted.Status != http.StatusCreated || trusted.Body["revision"] != 2.0 {
		t.Fatalf("trusted: %d %s", trusted.Status, trusted.Raw)
	}
	e := entriesOf(t, trusted)
	if g := e["get_ticket"]; g.Risk != "READ" || g.RiskSource != "annotation" || g.Registry != "updated" || g.Mutating {
		t.Errorf("get_ticket = %+v", g)
	}
	if tl := h.tool(eng, pid, "get_ticket"); tl["risk"] != "READ" || tl["source"] != "MCP" {
		t.Errorf("registry get_ticket = %v", tl)
	}
}

// A tool another source defines is not taken over: first come, first
// served, and a mapping imports it under another name.
func TestAnImportDoesNotTakeAnotherSourcesTool(t *testing.T) {
	h := newHarness(t, nil)
	pid := h.s.Demo.ProjectID
	eng := h.login("engineer@demo.agenttwin.dev")
	notify := []map[string]any{{"name": "notify", "inputSchema": map[string]any{"type": "object"}}}
	if r := h.importMCP(eng, pid, map[string]any{"server": map[string]any{"name": "crm"}, "tools": notify}); r.Status != http.StatusCreated {
		t.Fatalf("mcp: %d %s", r.Status, r.Raw)
	}
	if r := h.request("POST", "/api/v1/projects/"+pid+"/tools", map[string]any{"name": "archive", "risk": "WRITE_REVERSIBLE"}, bearer(eng)); r.Status != http.StatusCreated {
		t.Fatalf("manual tool: %d %s", r.Status, r.Raw)
	}
	doc := `
openapi: 3.0.3
info: {title: Notifications, version: '1'}
paths:
  /notify: {post: {operationId: notify, responses: {}}}
  /archive: {post: {operationId: archive, responses: {}}}
`
	r := h.importOpenAPI(eng, pid, map[string]any{"name": "notifications", "document": doc})
	if r.Status != http.StatusCreated {
		t.Fatalf("openapi: %d %s", r.Status, r.Raw)
	}
	e := entriesOf(t, r)
	if n := e["notify"]; n.Registry != "kept_other_source" || n.RegistryNote != "defined by mcp mcp:crm" || n.ToolVersion != 0 {
		t.Errorf("notify = %+v", n)
	}
	if a := e["archive"]; a.Registry != "kept_other_source" || a.RegistryNote != "defined by manual" {
		t.Errorf("archive = %+v", a)
	}
	if tl := h.tool(eng, pid, "notify"); tl["source"] != "MCP" || tl["latest_version"] != 1.0 {
		t.Errorf("the MCP tool changed: %v", tl)
	}
	// A manifest's tool stays the manifest's, even when a person added a
	// version by hand since.
	h.registerDemo(eng, pid, "1.3.1")
	if r := h.request("POST", "/api/v1/projects/"+pid+"/tools", map[string]any{"name": "refund_payment", "risk": "ADMIN"}, bearer(eng)); r.Status != http.StatusCreated {
		t.Fatalf("manual version: %d %s", r.Status, r.Raw)
	}
	payments := h.importOpenAPI(eng, pid, map[string]any{"name": "payments-api", "document": paymentsAPI})
	if e := entriesOf(t, payments)["refund_payment"]; e.Registry != "kept_manifest" {
		t.Errorf("refund_payment = %+v", e)
	}
	mapped := h.importOpenAPI(eng, pid, map[string]any{"name": "notifications", "document": doc,
		"names": map[string]string{"notify": "notify_http", "archive": "archive_http"}})
	if e := entriesOf(t, mapped); e["notify_http"].Registry != "created" || e["archive_http"].Registry != "created" {
		t.Errorf("mapped: %s", mapped.Raw)
	}
}

func TestImportRequestsAreValidated(t *testing.T) {
	h := newHarness(t, nil)
	pid := h.s.Demo.ProjectID
	eng := h.login("engineer@demo.agenttwin.dev")
	swagger := "swagger: '2.0'\ninfo: {title: t, version: '1'}\npaths: {}"
	allSkipped := "openapi: 3.1.0\ninfo: {title: t, version: '1'}\npaths:\n  /x: {get: {operationId: 'Ö', responses: {}}}"
	huge := "openapi: 3.1.0\ninfo: {title: '" + strings.Repeat("x", 2<<20) + "'}\npaths: {}"
	tools := make([]map[string]any, 1001)
	for i := range tools {
		tools[i] = map[string]any{"name": fmt.Sprintf("t%d", i), "inputSchema": map[string]any{"type": "object"}}
	}
	for _, c := range []struct {
		name, path string
		body       map[string]any
		want       int
		code       string
	}{
		{"bad name", "openapi", map[string]any{"name": "Payments API", "document": paymentsAPI}, 400, "INVALID_IMPORT"},
		{"bad service", "openapi", map[string]any{"name": "p", "service": "Pay ments", "document": paymentsAPI}, 400, "INVALID_IMPORT"},
		{"no document", "openapi", map[string]any{"name": "p"}, 400, "INVALID_IMPORT"},
		{"array document", "openapi", map[string]any{"name": "p", "document": []any{1}}, 400, "INVALID_IMPORT"},
		{"swagger", "openapi", map[string]any{"name": "p", "document": swagger}, 400, "INVALID_DOCUMENT"},
		{"not yaml", "openapi", map[string]any{"name": "p", "document": "a: [b"}, 400, "INVALID_DOCUMENT"},
		{"too large", "openapi", map[string]any{"name": "p", "document": huge}, 400, "INVALID_DOCUMENT"},
		{"nothing imported", "openapi", map[string]any{"name": "p", "document": allSkipped}, 400, "INVALID_DOCUMENT"},
		{"bad override", "openapi", map[string]any{"name": "p", "document": paymentsAPI, "risk_overrides": map[string]string{"getPayment": "HARMLESS"}}, 400, "INVALID_DOCUMENT"},
		{"unknown field", "openapi", map[string]any{"name": "p", "document": paymentsAPI, "trust_annotations": true}, 400, "INVALID_JSON"},
		{"bad server", "mcp", map[string]any{"server": map[string]any{"name": "Desk"}, "tools": deskTools}, 400, "INVALID_IMPORT"},
		{"ftp server", "mcp", map[string]any{"server": map[string]any{"name": "desk", "url": "ftp://x"}, "tools": deskTools}, 400, "INVALID_IMPORT"},
		{"no tools", "mcp", map[string]any{"server": map[string]any{"name": "desk"}}, 400, "INVALID_IMPORT"},
		{"too many tools", "mcp", map[string]any{"server": map[string]any{"name": "desk"}, "tools": tools}, 400, "INVALID_DOCUMENT"},
	} {
		t.Run(c.name, func(t *testing.T) {
			r := h.request("POST", "/api/v1/projects/"+pid+"/imports/"+c.path, c.body, bearer(eng))
			if r.Status != c.want || errCode(r) != c.code {
				t.Fatalf("%d %.300s, want %d %s", r.Status, r.Raw, c.want, c.code)
			}
		})
	}
	// The size bound is what refuses the large document.
	r0 := h.importOpenAPI(eng, pid, map[string]any{"name": "p", "document": huge})
	if reason := fmt.Sprint(r0.Body["error"].(map[string]any)["details"].(map[string]any)["reason"]); !strings.Contains(reason, "exceeds") {
		t.Errorf("too large: %.300s", r0.Raw)
	}
	// A document of which nothing became a tool says why.
	r := h.importOpenAPI(eng, pid, map[string]any{"name": "p", "document": allSkipped})
	if skipped := r.Body["error"].(map[string]any)["details"].(map[string]any)["skipped"].([]any); len(skipped) != 1 {
		t.Errorf("details = %s", r.Raw)
	}
	big := map[string]any{"name": "p", "document": strings.Repeat("x", 7<<20)}
	if r := h.importOpenAPI(eng, pid, big); r.Status != http.StatusRequestEntityTooLarge {
		t.Errorf("oversized body = %d", r.Status)
	}
	if n := h.count(`SELECT count(*) FROM control.tool_catalog`); n != 0 {
		t.Fatalf("rejected imports stored %d catalogs", n)
	}
	if r := h.importOpenAPI(h.login("viewer@demo.agenttwin.dev"), pid, map[string]any{"name": "p", "document": paymentsAPI}); r.Status != 403 {
		t.Errorf("viewer imports: %d", r.Status)
	}
	for _, path := range []string{"/api/v1/tool-catalogs/nope", "/api/v1/projects/" + pid + "/tool-catalogs?source=GRAPHQL",
		"/api/v1/projects/" + pid + "/tool-catalogs?name=Bad", "/api/v1/projects/" + pid + "/tool-catalogs?cursor=%7E"} {
		if r := h.request("GET", path, nil, bearer(eng)); r.Status != 400 {
			t.Errorf("GET %s = %d %s", path, r.Status, r.Raw)
		}
	}
}

func TestCatalogsStayInTheirProject(t *testing.T) {
	h := newHarness(t, nil)
	pid := h.s.Demo.ProjectID
	owner := h.login("owner@demo.agenttwin.dev")
	r := h.importOpenAPI(owner, pid, map[string]any{"name": "payments-api", "document": paymentsAPI})
	if r.Status != http.StatusCreated {
		t.Fatalf("import: %d %s", r.Status, r.Raw)
	}
	id := r.Body["id"].(string)
	other := h.login("owner@other.agenttwin.dev")
	for _, c := range []struct{ method, path string }{
		{"GET", "/api/v1/tool-catalogs/" + id},
		{"GET", "/api/v1/projects/" + pid + "/tool-catalogs"},
		{"POST", "/api/v1/projects/" + pid + "/imports/openapi"},
	} {
		var body any
		if c.method == "POST" {
			body = map[string]any{"name": "payments-api", "document": paymentsAPI}
		}
		if r := h.request(c.method, c.path, body, bearer(other)); r.Status != 404 {
			t.Errorf("other org %s %s = %d", c.method, c.path, r.Status)
		}
	}
	proj := h.request("POST", "/api/v1/projects", map[string]any{"slug": "billing", "name": "Billing"}, bearer(owner))
	key := h.request("POST", "/api/v1/projects/"+proj.Body["id"].(string)+"/api-keys", map[string]any{"name": "r", "scopes": []string{"read"}}, bearer(owner))
	if r := h.request("GET", "/api/v1/tool-catalogs/"+id, nil, map[string]string{"X-AgentTwin-Api-Key": key.Body["key"].(string)}); r.Status != 404 {
		t.Errorf("another project's key reads the catalog: %d", r.Status)
	}
}

// Concurrent imports of one source are numbered without gaps; equal ones
// store one revision.
func TestConcurrentImportsNumberRevisions(t *testing.T) {
	h := newHarness(t, nil)
	pid := h.s.Demo.ProjectID
	eng := h.login("engineer@demo.agenttwin.dev")
	risks := []string{"READ", "WRITE_REVERSIBLE", "WRITE_IRREVERSIBLE", "EXECUTE", "ADMIN"}
	var wg sync.WaitGroup
	statuses := make([]int, 10)
	for i := range statuses {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			// Five distinct imports, each sent twice.
			statuses[i] = h.importOpenAPI(eng, pid, map[string]any{"name": "payments-api", "document": paymentsAPI,
				"risk_overrides": map[string]string{"capturePayment": risks[i%5]}}).Status
		}(i)
	}
	wg.Wait()
	for _, s := range statuses {
		if s != http.StatusCreated && s != http.StatusOK {
			t.Fatalf("statuses = %v", statuses)
		}
	}
	var revisions []int
	rows, err := h.pool.Query(context.Background(), `SELECT revision FROM control.tool_catalog ORDER BY revision`)
	if err != nil {
		t.Fatal(err)
	}
	for rows.Next() {
		var n int
		_ = rows.Scan(&n)
		revisions = append(revisions, n)
	}
	rows.Close()
	// Two equal imports in a row store one revision; in another order,
	// two. Either way the numbers have no gaps and no repeats.
	for i, n := range revisions {
		if n != i+1 {
			t.Fatalf("revisions = %v", revisions)
		}
	}
	if len(revisions) < 5 || len(revisions) > 10 {
		t.Fatalf("revisions = %v", revisions)
	}
}
