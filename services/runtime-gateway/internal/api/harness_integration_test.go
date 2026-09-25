package api_test

import (
	"bytes"
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/Ozgurisikdamar/AgentTwin/packages/contracts"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/authn"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/db"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/events"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/httpx"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/ids"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/netguard"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/openapicheck"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/testutil"
	"github.com/Ozgurisikdamar/AgentTwin/services/runtime-gateway/internal/api"
	"github.com/Ozgurisikdamar/AgentTwin/services/runtime-gateway/internal/store"
	"github.com/Ozgurisikdamar/AgentTwin/services/runtime-gateway/migrations"
)

const internalSecret = "runtime-test-internal-secret-0123456789abcdef"

// contract is the runtime gateway API document every exchange of these
// tests is held to (ADR-0021).
var contract = func() *openapicheck.Contract {
	c, err := openapicheck.Load(contracts.OpenAPI, "openapi/runtime-gateway.openapi.yaml")
	if err != nil {
		panic(err)
	}
	return c
}()

func TestMain(m *testing.M) {
	flag.Parse()
	code := m.Run()
	// Coverage is only meaningful for a full run against real infrastructure.
	full := os.Getenv("AGENTTWIN_TEST_DATABASE_URL") != "" && flag.Lookup("test.run").Value.String() == ""
	if code == 0 && full {
		if missing := contract.Uncovered(); len(missing) > 0 {
			fmt.Fprintf(os.Stderr, "runtime-gateway contract: no checked successful exchange for %s\n", strings.Join(missing, ", "))
			code = 1
		}
	}
	os.Exit(code)
}

var t0 = time.Date(2026, 9, 25, 12, 0, 0, 0, time.UTC)

// clock is the gateway's clock in a test.
type clock struct {
	mu sync.Mutex
	t  time.Time
}

func (c *clock) now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.t
}

func (c *clock) advance(d time.Duration) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.t = c.t.Add(d)
}

type harness struct {
	t       *testing.T
	srv     *httptest.Server
	pool    *pgxpool.Pool
	tokens  *authn.TokenService
	api     *api.Server
	clock   *clock
	tools   *toolStub
	org     string
	project string
}

func newHarness(t *testing.T) *harness {
	t.Helper()
	ctx := context.Background()
	pool, err := db.Connect(ctx, nil, db.PoolConfig{URL: testutil.NewDatabase(t), Schema: migrations.Schema, MaxConns: 30})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(pool.Close)
	migs, _ := db.LoadMigrations(migrations.FS, ".")
	if _, err := (&db.Migrator{Pool: pool, Schema: migrations.Schema, Migrations: migs}).Up(ctx); err != nil {
		t.Fatal(err)
	}
	h := &harness{t: t, pool: pool, org: ids.New(), project: ids.New(), clock: &clock{t: t0}}
	h.tokens, _ = authn.NewTokenService(internalSecret)
	h.tools = newToolStub(t)
	h.api = &api.Server{Store: store.New(pool), Tokens: h.tokens, Log: slog.New(slog.NewJSONHandler(io.Discard, nil)),
		Egress: netguard.Policy{AllowedHosts: []string{"127.0.0.1", "tools.example.com"}, AllowLoopback: true,
			AllowHTTP: true, Timeout: time.Minute},
		Now: h.clock.now}
	mux := http.NewServeMux()
	h.api.Routes(mux)
	h.srv = httptest.NewServer(contract.Checking(httpx.Chain(mux, httpx.RequestID()), func(err error) {
		t.Errorf("the runtime gateway broke its contract:\n%v", err)
	}))
	t.Cleanup(h.srv.Close)
	return h
}

// ---- principals

func (h *harness) person(role authn.Role, name string) authn.Principal {
	return authn.Principal{OrgID: h.org, Actor: "user:" + name, Role: role, ProjectIDs: []string{h.project}}
}

func (h *harness) owner() authn.Principal    { return h.person(authn.RoleOwner, "owner") }
func (h *harness) reviewer() authn.Principal { return h.person(authn.RoleReviewer, "reviewer") }
func (h *harness) engineer() authn.Principal { return h.person(authn.RoleEngineer, "engineer") }
func (h *harness) viewer() authn.Principal   { return h.person(authn.RoleViewer, "viewer") }

// key is an API key of the project with the runtime:invoke scope.
func (h *harness) key(name string) authn.Principal {
	return authn.Principal{OrgID: h.org, Actor: "apikey:" + name, Role: authn.RoleAPIKey,
		ProjectIDs: []string{h.project}, Scopes: []authn.Scope{authn.ScopeRuntimeInvoke}}
}

// ---- HTTP

type reply struct {
	status int
	header http.Header
	body   []byte
}

func (r reply) json(t *testing.T) map[string]any {
	t.Helper()
	var out map[string]any
	if err := json.Unmarshal(r.body, &out); err != nil {
		t.Fatalf("not a JSON object (%d): %s", r.status, r.body)
	}
	return out
}

func (r reply) code() string {
	var e struct {
		Error struct {
			Code string `json:"code"`
		} `json:"error"`
	}
	_ = json.Unmarshal(r.body, &e)
	return e.Error.Code
}

func (r reply) details(t *testing.T) map[string]any {
	t.Helper()
	d, _ := r.json(t)["error"].(map[string]any)["details"].(map[string]any)
	return d
}

func (h *harness) do(p authn.Principal, method, path string, body any, headers map[string]string) reply {
	h.t.Helper()
	var rdr io.Reader
	switch b := body.(type) {
	case nil:
	case []byte:
		rdr = bytes.NewReader(b)
	case string:
		rdr = strings.NewReader(b)
	default:
		raw, _ := json.Marshal(b)
		rdr = bytes.NewReader(raw)
	}
	req, _ := http.NewRequest(method, h.srv.URL+path, rdr)
	tok, err := h.tokens.Mint(p, api.Producer, "rid-runtime-test")
	if err != nil {
		h.t.Fatal(err)
	}
	req.Header.Set("Authorization", "Bearer "+tok)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	res, err := h.srv.Client().Do(req)
	if err != nil {
		h.t.Fatal(err)
	}
	defer func() { _ = res.Body.Close() }()
	b, _ := io.ReadAll(res.Body)
	return reply{status: res.StatusCode, header: res.Header, body: b}
}

// ok requires the status and returns the JSON body.
func (h *harness) ok(want int, p authn.Principal, method, path string, body any) map[string]any {
	h.t.Helper()
	r := h.do(p, method, path, body, nil)
	if r.status != want {
		h.t.Fatalf("%s %s: %d (want %d) %s", method, path, r.status, want, r.body)
	}
	if len(r.body) == 0 {
		return nil
	}
	return r.json(h.t)
}

// refused requires the status and error code.
func (h *harness) refused(want int, code string, p authn.Principal, method, path string, body any) reply {
	h.t.Helper()
	r := h.do(p, method, path, body, nil)
	if r.status != want || r.code() != code {
		h.t.Fatalf("%s %s: %d %s (want %d %s) %s", method, path, r.status, r.code(), want, code, r.body)
	}
	return r
}

func (h *harness) q(path string) string {
	sep := "?"
	if strings.Contains(path, "?") {
		sep = "&"
	}
	return path + sep + "project_id=" + h.project
}

// invoke calls a tool through the gateway as the key.
func (h *harness) invoke(p authn.Principal, tool string, args any, headers map[string]string) reply {
	h.t.Helper()
	return h.do(p, "POST", "/gateway/v1/tools/"+tool, args, headers)
}

// ---- setup

// register registers the stub's tool.
func (h *harness) register(tool, risk string, extra map[string]any) map[string]any {
	h.t.Helper()
	body := map[string]any{"project_id": h.project, "url": h.tools.url(tool), "risk": risk}
	for k, v := range extra {
		body[k] = v
	}
	r := h.do(h.owner(), "PUT", "/api/v1/tool-endpoints/"+tool, body, nil)
	if r.status != 201 && r.status != 200 {
		h.t.Fatalf("register %s: %d %s", tool, r.status, r.body)
	}
	return r.json(h.t)
}

// createPolicy stores a document and returns the policy id.
func (h *harness) createPolicy(doc string) string {
	h.t.Helper()
	out := h.ok(201, h.owner(), "POST", "/api/v1/policies", map[string]any{"project_id": h.project, "document": doc})
	return out["policy"].(map[string]any)["id"].(string)
}

func (h *harness) activate(policyID string, version int) map[string]any {
	h.t.Helper()
	return h.ok(200, h.owner(), "POST", "/api/v1/policies/"+policyID+"/activate",
		map[string]any{"project_id": h.project, "version": version})
}

// ---- events

var validator = func() *events.Validator {
	v, err := events.DefaultValidator()
	if err != nil {
		panic(err)
	}
	return v
}()

// outbox returns the payloads of the events of a type written so far, each
// checked against its contract.
func (h *harness) outbox(eventType string) []map[string]any {
	h.t.Helper()
	rows, err := h.pool.Query(context.Background(), `SELECT envelope FROM outbox WHERE event_type = $1 ORDER BY created_at, id`, eventType)
	if err != nil {
		h.t.Fatal(err)
	}
	defer rows.Close()
	var out []map[string]any
	for rows.Next() {
		var raw []byte
		if err := rows.Scan(&raw); err != nil {
			h.t.Fatal(err)
		}
		var env events.Envelope
		if err := json.Unmarshal(raw, &env); err != nil {
			h.t.Fatal(err)
		}
		if err := validator.Validate(env); err != nil {
			h.t.Fatalf("%s breaks its contract: %v", eventType, err)
		}
		var payload map[string]any
		_ = json.Unmarshal(env.Payload, &payload)
		out = append(out, payload)
	}
	return out
}

// audits lists the audit actions written so far, in order.
func (h *harness) audits() []string {
	var out []string
	for _, p := range h.outbox("audit.recorded.v1") {
		out = append(out, p["action"].(string))
	}
	return out
}

// ---- the tools

// toolCall is a call a tool received.
type toolCall struct {
	Tool   string
	Body   map[string]any
	Header http.Header
}

// toolStub stands in for the tools: /tools/{name} answers as each demo tool
// would; /mcp is an MCP server.
type toolStub struct {
	mu    sync.Mutex
	calls []toolCall
	srv   *httptest.Server
	// slow is how long the "slow" tool takes.
	slow time.Duration
}

func newToolStub(t *testing.T) *toolStub {
	s := &toolStub{slow: 3 * time.Second}
	mux := http.NewServeMux()
	mux.HandleFunc("POST /tools/{name}", s.tool)
	mux.HandleFunc("POST /mcp", s.mcp)
	mux.HandleFunc("POST /redirect", func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, "/tools/lookup_order", http.StatusTemporaryRedirect)
	})
	s.srv = httptest.NewServer(mux)
	t.Cleanup(s.srv.Close)
	return s
}

func (s *toolStub) url(tool string) string { return s.srv.URL + "/tools/" + tool }

func (s *toolStub) record(tool string, r *http.Request) map[string]any {
	var body map[string]any
	_ = json.NewDecoder(r.Body).Decode(&body)
	s.mu.Lock()
	s.calls = append(s.calls, toolCall{Tool: tool, Body: body, Header: r.Header.Clone()})
	s.mu.Unlock()
	return body
}

// received returns the calls a tool received.
func (s *toolStub) received(tool string) []toolCall {
	s.mu.Lock()
	defer s.mu.Unlock()
	var out []toolCall
	for _, c := range s.calls {
		if c.Tool == tool {
			out = append(out, c)
		}
	}
	return out
}

func (s *toolStub) tool(w http.ResponseWriter, r *http.Request) {
	name := r.PathValue("name")
	body := s.record(name, r)
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Set-Cookie", "session=never-forwarded")
	switch name {
	case "refund_payment":
		_ = json.NewEncoder(w).Encode(map[string]any{"refund_id": "RF-" + fmt.Sprint(len(s.received(name))),
			"order_id": body["order_id"], "amount": body["amount"], "status": "refunded"})
	case "lookup_order":
		_ = json.NewEncoder(w).Encode(map[string]any{"order_id": body["order_id"], "status": "delivered", "total": 150})
	case "fail":
		w.WriteHeader(http.StatusInternalServerError)
		_ = json.NewEncoder(w).Encode(map[string]any{"error": "payments are down"})
	case "reject":
		w.Header().Set("Content-Type", "application/problem+json")
		w.WriteHeader(http.StatusUnprocessableEntity)
		_ = json.NewEncoder(w).Encode(map[string]any{"error": "no such order"})
	case "slow":
		select {
		case <-time.After(s.slow):
		case <-r.Context().Done():
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"done": true})
	case "big":
		_ = json.NewEncoder(w).Encode(map[string]any{"blob": strings.Repeat("x", 64<<10)})
	case "huge":
		_ = json.NewEncoder(w).Encode(map[string]any{"blob": strings.Repeat("x", 5<<20)})
	case "html":
		w.Header().Set("Content-Type", "text/html")
		_, _ = io.WriteString(w, "<script>alert(1)</script>")
	default:
		w.WriteHeader(http.StatusNotFound)
	}
}

// mcp is a minimal MCP server (streamable HTTP): initialize, the initialized
// notification and tools/call, answering the last as a server-sent event.
func (s *toolStub) mcp(w http.ResponseWriter, r *http.Request) {
	var req struct {
		ID     any            `json:"id"`
		Method string         `json:"method"`
		Params map[string]any `json:"params"`
	}
	_ = json.NewDecoder(r.Body).Decode(&req)
	switch req.Method {
	case "initialize":
		w.Header().Set("Mcp-Session-Id", "session-1")
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{"jsonrpc": "2.0", "id": req.ID, "result": map[string]any{
			"protocolVersion": "2025-06-18", "capabilities": map[string]any{"tools": map[string]any{}},
			"serverInfo": map[string]any{"name": "stub", "version": "1"}}})
	case "notifications/initialized":
		w.WriteHeader(http.StatusAccepted)
	case "tools/call":
		args, _ := req.Params["arguments"].(map[string]any)
		s.mu.Lock()
		s.calls = append(s.calls, toolCall{Tool: "mcp:" + fmt.Sprint(req.Params["name"]), Body: args, Header: r.Header.Clone()})
		s.mu.Unlock()
		if r.Header.Get("Mcp-Session-Id") != "session-1" {
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		if req.Params["name"] == "confused" {
			// Not JSON-RPC: an answer is a result or an error, never both.
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(map[string]any{"jsonrpc": "2.0", "id": req.ID,
				"result": map[string]any{"content": []any{}}, "error": map[string]any{"code": -32000, "message": "failed"}})
			return
		}
		if req.Params["name"] == "broken" {
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(map[string]any{"jsonrpc": "2.0", "id": req.ID,
				"error": map[string]any{"code": -32602, "message": "unknown tool"}})
			return
		}
		result, _ := json.Marshal(map[string]any{"jsonrpc": "2.0", "id": req.ID, "result": map[string]any{
			"content": []map[string]any{{"type": "text", "text": fmt.Sprintf("looked up %v", args["order_id"])}},
			"isError": false}})
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = fmt.Fprintf(w, "event: message\ndata: %s\n\n", result)
	default:
		w.WriteHeader(http.StatusBadRequest)
	}
}

// ---- documents

// refundPolicy is the demo's refund policy (ADR-0033).
const refundPolicy = `
apiVersion: agenttwin.dev/v1
kind: Policy
metadata:
  name: refund-limits
  description: Refunds the support agent may issue on its own.
spec:
  tool: refund_payment
  rules:
    - name: valid-order
      when: "!has(args.order_id) || !string(args.order_id).matches('^ORD-[0-9]+$')"
      effect: deny
      message: A refund must name a valid order.
    - name: one-refund-per-conversation
      when: trace.calls >= 1
      effect: deny
      message: One refund per conversation.
    - name: never-above-500
      when: args.amount > 500
      effect: deny
      message: Refunds above 500 are never automatic.
    - name: approval-above-100
      when: args.amount > 100
      effect: require_approval
      message: Refunds above 100 need a person's approval.
  approval:
    expiresInSeconds: 1200
  tests:
    - name: small refund
      args: {order_id: ORD-1001, amount: 40}
      expect: allow
    - name: over the automatic limit
      args: {order_id: ORD-1003, amount: 150}
      expect: require_approval
      rule: approval-above-100
    - name: far above the limit
      args: {order_id: ORD-1003, amount: 900}
      expect: deny
      rule: never-above-500
    - name: a second refund
      args: {order_id: ORD-1001, amount: 40}
      context: {traceCalls: 1}
      expect: deny
      rule: one-refund-per-conversation
    - name: no order
      args: {amount: 40}
      expect: deny
      rule: valid-order
`

// trace is a trace id for a test conversation.
func trace(n int) string { return fmt.Sprintf("%032x", n+1) }
