package api_test

import (
	"bytes"
	"compress/gzip"
	"context"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/prometheus/client_golang/prometheus"
	promtest "github.com/prometheus/client_golang/prometheus/testutil"
	coltracepb "go.opentelemetry.io/proto/otlp/collector/trace/v1"
	commonpb "go.opentelemetry.io/proto/otlp/common/v1"
	tracepb "go.opentelemetry.io/proto/otlp/trace/v1"
	spb "google.golang.org/genproto/googleapis/rpc/status"
	"google.golang.org/grpc/codes"
	"google.golang.org/protobuf/proto"

	"github.com/Ozgurisikdamar/AgentTwin/packages/contracts"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/authn"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/db"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/events"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/httpx"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/ids"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/openapicheck"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/testutil"
	"github.com/Ozgurisikdamar/AgentTwin/services/trace-service/internal/api"
	"github.com/Ozgurisikdamar/AgentTwin/services/trace-service/internal/finalizer"
	"github.com/Ozgurisikdamar/AgentTwin/services/trace-service/internal/keys"
	"github.com/Ozgurisikdamar/AgentTwin/services/trace-service/internal/otlp"
	"github.com/Ozgurisikdamar/AgentTwin/services/trace-service/internal/store"
	"github.com/Ozgurisikdamar/AgentTwin/services/trace-service/migrations"
)

const internalSecret = "trace-test-internal-secret-0123456789abcdef"

// contract is the trace API document every exchange of these tests is held
// to (ADR-0021).
var contract = func() *openapicheck.Contract {
	c, err := openapicheck.Load(contracts.OpenAPI, "openapi/trace-service.openapi.yaml")
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
			fmt.Fprintf(os.Stderr, "trace-service contract: no checked successful exchange for %s\n", strings.Join(missing, ", "))
			code = 1
		}
	}
	os.Exit(code)
}

var (
	orgA     = ids.New()
	orgB     = ids.New()
	projA    = ids.New()
	projA2   = ids.New()
	projB    = ids.New()
	baseTime = time.Now().UTC().Add(-time.Hour).Truncate(time.Second)
)

type harness struct {
	t       *testing.T
	srv     *httptest.Server
	pool    *pgxpool.Pool
	fin     *finalizer.Finalizer
	tokens  *authn.TokenService
	keys    map[string]keys.Info
	lookups atomic.Int64
	down    atomic.Bool
	api     *api.Server
}

func newHarness(t *testing.T) *harness {
	t.Helper()
	ctx := context.Background()
	pool, err := db.Connect(ctx, nil, db.PoolConfig{URL: testutil.NewDatabase(t), Schema: migrations.Schema, MaxConns: 20})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(pool.Close)
	migs, _ := db.LoadMigrations(migrations.FS, ".")
	if _, err := (&db.Migrator{Pool: pool, Schema: migrations.Schema, Migrations: migs}).Up(ctx); err != nil {
		t.Fatal(err)
	}
	h := &harness{t: t, pool: pool, keys: map[string]keys.Info{
		"atk_aaaaaaaa_ingest":   {Valid: true, KeyID: "k1", OrganizationID: orgA, ProjectID: projA, Scopes: []authn.Scope{authn.ScopeTracesWrite}, ContentMode: "redacted", TraceRetentionDays: 30, ContentRetentionDays: 7},
		"atk_aaaaaaa2_ingest":   {Valid: true, KeyID: "k2", OrganizationID: orgA, ProjectID: projA2, Scopes: []authn.Scope{authn.ScopeTracesWrite}, ContentMode: "off", TraceRetentionDays: 30, ContentRetentionDays: 7},
		"atk_bbbbbbbb_ingest":   {Valid: true, KeyID: "k3", OrganizationID: orgB, ProjectID: projB, Scopes: []authn.Scope{authn.ScopeTracesWrite}, ContentMode: "full", TraceRetentionDays: 30, ContentRetentionDays: 7},
		"atk_cccccccc_readonly": {Valid: true, KeyID: "k4", OrganizationID: orgA, ProjectID: projA, Scopes: []authn.Scope{authn.ScopeRead}, ContentMode: "off", TraceRetentionDays: 30, ContentRetentionDays: 7},
	}}
	lookup := func(ctx context.Context, key string) (keys.Info, error) {
		h.lookups.Add(1)
		if h.down.Load() {
			return keys.Info{}, errors.New("control plane unreachable")
		}
		if info, ok := h.keys[key]; ok {
			return info, nil
		}
		return keys.Info{Valid: false}, nil
	}
	h.tokens, _ = authn.NewTokenService(internalSecret)
	st := store.New(pool)
	h.api = &api.Server{Store: st, Keys: keys.NewVerifier(lookup, time.Minute, time.Second),
		Log: slog.New(slog.NewJSONHandler(testWriter{t}, nil)), Metrics: api.NewMetrics(prometheus.NewRegistry()),
		Limits: otlp.DefaultLimits, Tokens: h.tokens}
	h.api.Init(4)
	mux := http.NewServeMux()
	h.api.Routes(mux)
	h.srv = httptest.NewServer(contract.Checking(httpx.Chain(mux, httpx.RequestID()), func(err error) {
		t.Errorf("the trace service broke its contract:\n%v", err)
	}))
	t.Cleanup(h.srv.Close)
	h.fin = &finalizer.Finalizer{Pool: pool, Store: st, Settle: 0, IncompleteAfter: time.Hour}
	return h
}

// ---- OTLP builders

type spec struct {
	id, parent, name string
	offsetMS, durMS  int
	status           int
	attrs            map[string]any
}

func hexID(n int, seed string) string {
	s := hex.EncodeToString([]byte(seed))
	for len(s) < n {
		s += "0"
	}
	return s[:n]
}

func kv(attrs map[string]any) []map[string]any {
	out := []map[string]any{}
	for k, v := range attrs {
		var val map[string]any
		switch x := v.(type) {
		case string:
			val = map[string]any{"stringValue": x}
		case int:
			val = map[string]any{"intValue": fmt.Sprint(x)}
		case float64:
			val = map[string]any{"doubleValue": x}
		case bool:
			val = map[string]any{"boolValue": x}
		}
		out = append(out, map[string]any{"key": k, "value": val})
	}
	return out
}

func otlpJSON(traceID string, resource map[string]any, spans ...spec) []byte {
	js := []map[string]any{}
	for _, s := range spans {
		start := baseTime.Add(time.Duration(s.offsetMS) * time.Millisecond)
		end := start.Add(time.Duration(s.durMS) * time.Millisecond)
		m := map[string]any{"traceId": traceID, "spanId": s.id, "name": s.name, "kind": 1,
			"startTimeUnixNano": fmt.Sprint(start.UnixNano()), "endTimeUnixNano": fmt.Sprint(end.UnixNano()),
			"attributes": kv(s.attrs), "status": map[string]any{"code": s.status}}
		if s.parent != "" {
			m["parentSpanId"] = s.parent
		}
		js = append(js, m)
	}
	b, _ := json.Marshal(map[string]any{"resourceSpans": []any{map[string]any{
		"resource":   map[string]any{"attributes": kv(resource)},
		"scopeSpans": []any{map[string]any{"scope": map[string]any{"name": "agenttwin"}, "spans": js}},
	}}})
	return b
}

var demoResource = map[string]any{"service.name": "support-refund-agent", "deployment.environment.name": "production",
	"agenttwin.agent.name": "support-refund-agent", "agenttwin.agent.version": "1.3.0", "agenttwin.sdk.name": "agenttwin-python",
	"agenttwin.content.redacted": true}

func candidateSpans(traceSeed string) (string, []spec, spec) {
	tid := hexID(32, traceSeed)
	root := hexID(16, traceSeed+"r")
	children := []spec{
		{id: hexID(16, traceSeed+"m"), parent: root, name: "chat scripted-planner-v1", offsetMS: 10, durMS: 40, status: 1, attrs: map[string]any{
			"gen_ai.operation.name": "chat", "gen_ai.provider.name": "scripted", "gen_ai.request.model": "scripted-planner-v1",
			"gen_ai.usage.input_tokens": 900, "gen_ai.usage.output_tokens": 40, "agenttwin.cost.usd": 0.0021,
			"gen_ai.input.messages": "customer jane@example.com wants a refund"}},
		{id: hexID(16, traceSeed+"o"), parent: root, name: "execute_tool lookup_order", offsetMS: 100, durMS: 20, status: 1, attrs: map[string]any{
			"gen_ai.tool.name": "lookup_order", "agenttwin.tool.risk": "READ", "agenttwin.tool.args_hash": "h-o"}},
		{id: hexID(16, traceSeed+"p"), parent: root, name: "execute_tool refund_payment", offsetMS: 200, durMS: 100, status: 2, attrs: map[string]any{
			"gen_ai.tool.name": "refund_payment", "agenttwin.tool.risk": "WRITE_IRREVERSIBLE", "agenttwin.tool.args_hash": "h-r",
			"agenttwin.tool.result_status": "timeout", "error.type": "timeout"}},
		{id: hexID(16, traceSeed+"q"), parent: root, name: "execute_tool refund_payment", offsetMS: 400, durMS: 50, status: 1, attrs: map[string]any{
			"gen_ai.tool.name": "refund_payment", "agenttwin.tool.risk": "WRITE_IRREVERSIBLE", "agenttwin.tool.args_hash": "h-r",
			"agenttwin.tool.result_status": "ok"}},
		{id: hexID(16, traceSeed+"v"), parent: root, name: "outcome.verify", offsetMS: 1900, durMS: 5, status: 1, attrs: map[string]any{
			"agenttwin.outcome.status": "FAILURE", "agenttwin.outcome.claimed": "SUCCESS", "agenttwin.outcome.verified": true,
			"agenttwin.outcome.verification_source": "tool_twin_state", "agenttwin.state.diff": `{"refund_count":[0,2]}`}},
	}
	rootSpan := spec{id: root, name: "agent.run", offsetMS: 0, durMS: 2000, status: 1, attrs: map[string]any{"agenttwin.span.kind": "agent", "agenttwin.session.id": "sess-" + traceSeed}}
	return tid, children, rootSpan
}

func (h *harness) post(path string, body []byte, hdr map[string]string) *http.Response {
	h.t.Helper()
	req, _ := http.NewRequest("POST", h.srv.URL+path, bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	for k, v := range hdr {
		req.Header.Set(k, v)
	}
	res, err := h.srv.Client().Do(req)
	if err != nil {
		h.t.Fatal(err)
	}
	return res
}

func (h *harness) ingest(key string, body []byte) (int, map[string]any) {
	res := h.post("/v1/traces", body, map[string]string{"X-AgentTwin-Api-Key": key})
	defer func() { _ = res.Body.Close() }()
	var out map[string]any
	_ = json.NewDecoder(res.Body).Decode(&out)
	return res.StatusCode, out
}

func (h *harness) mint(p authn.Principal) string {
	tok, err := h.tokens.Mint(p, "trace-service", "rid-trace-test")
	if err != nil {
		h.t.Fatal(err)
	}
	return tok
}

func (h *harness) as(p authn.Principal, method, path string, body any) (int, map[string]any) {
	h.t.Helper()
	var rdr io.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		rdr = bytes.NewReader(b)
	}
	req, _ := http.NewRequest(method, h.srv.URL+path, rdr)
	req.Header.Set("Authorization", "Bearer "+h.mint(p))
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	res, err := h.srv.Client().Do(req)
	if err != nil {
		h.t.Fatal(err)
	}
	defer func() { _ = res.Body.Close() }()
	var out map[string]any
	_ = json.NewDecoder(res.Body).Decode(&out)
	return res.StatusCode, out
}

func (h *harness) finalizeAll() int {
	h.t.Helper()
	total := 0
	for {
		n, err := h.fin.Tick(context.Background())
		if err != nil {
			h.t.Fatal(err)
		}
		total += n
		if n == 0 {
			return total
		}
	}
}

func (h *harness) count(q string, args ...any) int {
	h.t.Helper()
	var n int
	if err := h.pool.QueryRow(context.Background(), q, args...).Scan(&n); err != nil {
		h.t.Fatalf("%s: %v", q, err)
	}
	return n
}

var engineerA = authn.Principal{OrgID: orgA, Actor: "user:eng", Role: authn.RoleEngineer, AllProjects: true}

// ---- tests

func TestIngestSettleFinalizeAndPublishOnce(t *testing.T) {
	h := newHarness(t)
	tid, children, root := candidateSpans("cand-1")

	// Children arrive first (typical BatchSpanProcessor order); not finalizable yet.
	if code, body := h.ingest("atk_aaaaaaaa_ingest", otlpJSON(tid, demoResource, children...)); code != 200 || len(body) != 0 {
		t.Fatalf("ingest children: %d %v", code, body)
	}
	if n := h.finalizeAll(); n != 0 {
		t.Fatalf("trace without root finalized early (%d)", n)
	}
	if code, _ := h.ingest("atk_aaaaaaaa_ingest", otlpJSON(tid, demoResource, root)); code != 200 {
		t.Fatalf("ingest root: %d", code)
	}
	// Retried export (at-least-once) must not duplicate spans.
	if code, _ := h.ingest("atk_aaaaaaaa_ingest", otlpJSON(tid, demoResource, children...)); code != 200 {
		t.Fatal("retry failed")
	}
	if n := h.count(`SELECT count(*) FROM trace.span WHERE trace_id = $1`, tid); n != 6 {
		t.Fatalf("spans stored = %d, want 6", n)
	}
	if n := h.finalizeAll(); n != 1 {
		t.Fatalf("finalized %d traces, want 1", n)
	}

	code, detail := h.as(engineerA, "GET", "/api/v1/traces/"+tid, nil)
	if code != 200 {
		t.Fatalf("detail: %d %v", code, detail)
	}
	tr := detail["trace"].(map[string]any)
	if tr["agent_version"] != "1.3.0" || tr["tool_call_count"].(float64) != 3 || tr["retry_count"].(float64) != 1 ||
		tr["outcome_status"] != "FAILURE" || tr["environment"] != "production" || tr["semconv_version"] != "genai-v1.37" {
		t.Fatalf("trace row: %v", tr)
	}
	signals := fmt.Sprint(tr["signals"])
	for _, want := range []string{"duplicate_side_effect", "contradiction", "timeout_after_mutation", "outcome_failure"} {
		if !strings.Contains(signals, want) {
			t.Errorf("signal %s missing: %s", want, signals)
		}
	}
	if len(detail["spans"].([]any)) != 6 || detail["outcome"].(map[string]any)["contradiction"] != true {
		t.Fatalf("detail spans/outcome: %v", detail["outcome"])
	}

	// A late span re-finalizes the summary but never re-publishes ingestion.
	late := spec{id: hexID(16, "cand-1late"), parent: root.id, name: "execute_tool send_email", offsetMS: 1950, durMS: 5, status: 1,
		attrs: map[string]any{"gen_ai.tool.name": "send_email", "agenttwin.tool.risk": "WRITE_REVERSIBLE"}}
	if code, _ := h.ingest("atk_aaaaaaaa_ingest", otlpJSON(tid, demoResource, late)); code != 200 {
		t.Fatal("late span")
	}
	if n := h.finalizeAll(); n != 1 {
		t.Fatalf("late span must re-finalize (%d)", n)
	}
	if n := h.count(`SELECT count(*) FROM trace.outbox WHERE event_type = 'trace.ingested.v1'`); n != 1 {
		t.Fatalf("trace.ingested.v1 published %d times, want exactly 1", n)
	}
	if n := h.count(`SELECT tool_call_count FROM trace.trace WHERE trace_id = $1`, tid); n != 4 {
		t.Fatalf("late span not reflected: tool_call_count=%d", n)
	}
	// The published payload satisfies the event contract.
	var raw []byte
	if err := h.pool.QueryRow(context.Background(), `SELECT envelope::text FROM trace.outbox WHERE event_type = 'trace.ingested.v1'`).Scan(&raw); err != nil {
		t.Fatal(err)
	}
	v, _ := events.DefaultValidator()
	if _, err := v.ValidateRaw(raw); err != nil {
		t.Fatalf("event violates contract: %v\n%s", err, raw)
	}
	var env events.Envelope
	_ = json.Unmarshal(raw, &env)
	var payload map[string]any
	_ = json.Unmarshal(env.Payload, &payload)
	sum := payload["summary"].(map[string]any)
	if fmt.Sprint(sum["violations"]) != "[retry_without_idempotency_key duplicate_irreversible_action]" || env.OrganizationID != orgA {
		t.Fatalf("payload summary: %v", sum)
	}
	if n := h.count(`SELECT count(*) FROM trace.outbox WHERE event_type = 'trace.outcome_recorded.v1'`); n != 1 {
		t.Fatalf("span outcome events = %d", n)
	}
}

func TestContentPolicyIsEnforcedPerProject(t *testing.T) {
	h := newHarness(t)
	contentOf := func(key, seed string, resource map[string]any) string {
		tid, children, root := candidateSpans(seed)
		if code, _ := h.ingest(key, otlpJSON(tid, resource, append(children, root)...)); code != 200 {
			t.Fatalf("ingest %s", seed)
		}
		var c *string
		_ = h.pool.QueryRow(context.Background(), `SELECT content::text FROM trace.span WHERE trace_id = $1 AND kind = 'model'`, tid).Scan(&c)
		if c == nil {
			return ""
		}
		return *c
	}
	// Project in "off" mode stores no content at all.
	if c := contentOf("atk_aaaaaaa2_ingest", "off-1", demoResource); c != "" {
		t.Fatalf("off mode stored content: %s", c)
	}
	// "redacted" without the SDK's client-side redaction marker drops content.
	plain := map[string]any{"service.name": "x", "agenttwin.agent.name": "a"}
	if c := contentOf("atk_aaaaaaaa_ingest", "red-1", plain); c != "" {
		t.Fatalf("unredacted content accepted in redacted mode: %s", c)
	}
	// With the marker, content is kept and server-side masking still applies.
	if c := contentOf("atk_aaaaaaaa_ingest", "red-2", demoResource); c == "" || strings.Contains(c, "jane@example.com") || !strings.Contains(c, "[REDACTED:email]") {
		t.Fatalf("redacted mode content: %q", c)
	}
	// "full" keeps content as captured.
	if c := contentOf("atk_bbbbbbbb_ingest", "full-1", plain); !strings.Contains(c, "jane@example.com") {
		t.Fatalf("full mode content: %q", c)
	}
	if n := h.count(`SELECT count(*) FROM trace.trace WHERE content_dropped`); n != 2 {
		t.Fatalf("content_dropped traces = %d, want 2", n)
	}
}

func TestIngestAuthentication(t *testing.T) {
	h := newHarness(t)
	tid, children, _ := candidateSpans("auth")
	body := otlpJSON(tid, demoResource, children...)
	cases := []struct {
		name string
		key  string
		want int
	}{
		{"missing", "", 401}, {"unknown", "atk_zzzzzzzz_nope", 401}, {"read-only scope", "atk_cccccccc_readonly", 403},
	}
	for _, c := range cases {
		if code, _ := h.ingest(c.key, body); code != c.want {
			t.Errorf("%s: %d, want %d", c.name, code, c.want)
		}
	}
	// Bearer form of the key is accepted too.
	res := h.post("/v1/traces", body, map[string]string{"Authorization": "Bearer atk_aaaaaaaa_ingest"})
	if res.StatusCode != 200 {
		t.Fatalf("bearer key: %d", res.StatusCode)
	}
	_ = res.Body.Close()
	// Verification is cached: many requests, one lookup per key.
	before := h.lookups.Load()
	for i := 0; i < 5; i++ {
		h.ingest("atk_aaaaaaaa_ingest", body)
	}
	if h.lookups.Load() != before {
		t.Fatalf("key verification not cached (%d extra lookups)", h.lookups.Load()-before)
	}
	// If the control plane is down, unknown keys get a retryable 503.
	h.down.Store(true)
	if code, _ := h.ingest("atk_dddddddd_new", body); code != 503 {
		t.Fatalf("control plane down: %d, want 503", code)
	}
}

func TestExplorerFiltersPaginationAndIsolation(t *testing.T) {
	h := newHarness(t)
	for i := 0; i < 25; i++ {
		tid := hexID(32, fmt.Sprintf("list-%02d", i))
		root := hexID(16, fmt.Sprintf("list-%02dr", i))
		version := "1.2.4"
		status := 1
		if i%5 == 0 {
			version, status = "1.3.0", 2
		}
		res := map[string]any{"agenttwin.agent.name": "support-refund-agent", "agenttwin.agent.version": version, "deployment.environment.name": "staging"}
		spans := []spec{
			{id: root, name: "agent.run", offsetMS: i * 1000, durMS: 100 * (i + 1), status: status, attrs: map[string]any{}},
			{id: hexID(16, fmt.Sprintf("list-%02dt", i)), parent: root, name: "tool", offsetMS: i*1000 + 1, durMS: 5, status: 1,
				attrs: map[string]any{"gen_ai.tool.name": []string{"lookup_order", "send_email"}[i%2]}},
		}
		if code, _ := h.ingest("atk_aaaaaaaa_ingest", otlpJSON(tid, res, spans...)); code != 200 {
			t.Fatal("ingest")
		}
	}
	// A trace in another organization must never appear.
	tid, children, root := candidateSpans("org-b")
	h.ingest("atk_bbbbbbbb_ingest", otlpJSON(tid, demoResource, append(children, root)...))
	h.finalizeAll()

	collect := func(p authn.Principal, query string) []string {
		var got []string
		cursor := ""
		for page := 0; page < 10; page++ {
			q := query + "&limit=10"
			if cursor != "" {
				q += "&cursor=" + cursor
			}
			code, body := h.as(p, "GET", "/api/v1/traces?"+strings.TrimPrefix(q, "&"), nil)
			if code != 200 {
				t.Fatalf("list %s: %d %v", q, code, body)
			}
			for _, it := range body["items"].([]any) {
				got = append(got, it.(map[string]any)["trace_id"].(string))
			}
			cursor, _ = body["next_cursor"].(string)
			if cursor == "" {
				break
			}
		}
		return got
	}
	all := collect(engineerA, "")
	seen := map[string]bool{}
	for _, id := range all {
		if seen[id] {
			t.Fatalf("pagination returned %s twice", id)
		}
		seen[id] = true
	}
	if len(all) != 25 {
		t.Fatalf("paginated %d traces, want 25", len(all))
	}
	checks := map[string]int{
		"&agent_version=1.3.0": 5, "&status=ERROR": 5, "&tool=send_email": 12, "&environment=staging": 25,
		"&min_duration_ms=2000": 6, "&project_id=" + projA: 25, "&project_id=" + projA2: 0, "&signal=error": 5,
	}
	for q, want := range checks {
		if got := collect(engineerA, q); len(got) != want {
			t.Errorf("filter %s: %d traces, want %d", q, len(got), want)
		}
	}
	// Other organization's principal sees only its own trace.
	otherOrg := authn.Principal{OrgID: orgB, Actor: "user:b", Role: authn.RoleViewer, AllProjects: true}
	if got := collect(otherOrg, ""); len(got) != 1 {
		t.Fatalf("org B sees %d traces, want 1", len(got))
	}
	if code, _ := h.as(otherOrg, "GET", "/api/v1/traces/"+all[0], nil); code != 404 {
		t.Fatalf("cross-tenant detail: %d, want 404", code)
	}
	// A key scoped to another project cannot read project A.
	scoped := authn.Principal{OrgID: orgA, Actor: "apikey:x", Role: authn.RoleAPIKey, ProjectIDs: []string{projA2}, Scopes: []authn.Scope{authn.ScopeRead}}
	if got := collect(scoped, ""); len(got) != 0 {
		t.Fatalf("project-scoped key sees %d traces of another project", len(got))
	}
	if code, _ := h.as(engineerA, "GET", "/api/v1/traces?status=BROKEN", nil); code != 400 {
		t.Fatalf("invalid filter accepted: %d", code)
	}
	code, stats := h.as(engineerA, "GET", "/api/v1/trace-stats?project_id="+projA+"&from="+baseTime.Add(-time.Hour).Format(time.RFC3339), nil)
	if code != 200 {
		t.Fatalf("stats: %d %v", code, stats)
	}
	st := stats["stats"].(map[string]any)
	if st["total"].(float64) != 25 || st["errors"].(float64) != 5 {
		t.Fatalf("stats: %v", st)
	}
	// Filter options (trace explorer facets): counts, ordering and isolation.
	facetsOf := func(p authn.Principal, q string) map[string][]string {
		t.Helper()
		code, body := h.as(p, "GET", "/api/v1/traces/facets"+q, nil)
		if code != 200 {
			t.Fatalf("facets%s: %d %v", q, code, body)
		}
		out := map[string][]string{}
		for dim, vals := range body["facets"].(map[string]any) {
			out[dim] = []string{}
			for _, v := range vals.([]any) {
				f := v.(map[string]any)
				out[dim] = append(out[dim], fmt.Sprintf("%s=%v", f["value"], f["count"]))
			}
		}
		return out
	}
	fa := facetsOf(engineerA, "")
	for dim, want := range map[string][]string{
		"agent_version": {"1.2.4=20", "1.3.0=5"}, "tool": {"lookup_order=13", "send_email=12"},
		"environment": {"staging=25"}, "signal": {"error=5"}, "status": {"OK=20", "ERROR=5"}, "release": {},
	} {
		if fmt.Sprint(fa[dim]) != fmt.Sprint(want) {
			t.Errorf("facet %s = %v, want %v", dim, fa[dim], want)
		}
	}
	if got := facetsOf(otherOrg, "")["agent_version"]; fmt.Sprint(got) != "[1.3.0=1]" {
		t.Errorf("org B facets leak or miss: %v", got)
	}
	if got := facetsOf(scoped, "")["agent_version"]; len(got) != 0 {
		t.Errorf("project-scoped key sees facets of another project: %v", got)
	}
	if got := facetsOf(engineerA, "?project_id="+projA2)["agent_version"]; len(got) != 0 {
		t.Errorf("project filter ignored: %v", got)
	}
	if code, _ := h.as(scoped, "GET", "/api/v1/traces/facets?project_id="+projA, nil); code != 404 {
		t.Errorf("facets of an inaccessible project: %d, want 404", code)
	}
	// Without a token the query API is closed.
	res, _ := http.Get(h.srv.URL + "/api/v1/traces")
	if res.StatusCode != 401 {
		t.Fatalf("unauthenticated query: %d", res.StatusCode)
	}
}

func TestOutcomeFlagAndDeletion(t *testing.T) {
	h := newHarness(t)
	tid, children, root := candidateSpans("out-1")
	children = children[:4] // no outcome span
	h.ingest("atk_aaaaaaaa_ingest", otlpJSON(tid, demoResource, append(children, root)...))
	h.finalizeAll()
	path := "/api/v1/traces/" + tid

	viewer := authn.Principal{OrgID: orgA, Actor: "user:v", Role: authn.RoleViewer, AllProjects: true}
	if code, _ := h.as(viewer, "POST", path+"/outcome", map[string]any{"status": "SUCCESS"}); code != 403 {
		t.Fatalf("viewer outcome: %d", code)
	}
	if code, body := h.as(engineerA, "POST", path+"/outcome", map[string]any{"status": "SUCCESS", "verified": true, "verification_source": "unavailable"}); code != 400 {
		t.Fatalf("verified without source accepted: %d %v", code, body)
	}
	code, out := h.as(engineerA, "POST", path+"/outcome", map[string]any{"status": "FAILURE", "claimed_status": "SUCCESS", "verified": true,
		"expected_state": map[string]any{"refund_count": 1}, "actual_state": map[string]any{"refund_count": 2}, "notes": "double refund"})
	if code != 200 || out["verification_source"] != "human_review" || out["contradiction"] != true {
		t.Fatalf("human outcome: %d %v", code, out)
	}
	_, detail := h.as(engineerA, "GET", path, nil)
	tr := detail["trace"].(map[string]any)
	if tr["human_reviewed"] != true || tr["outcome_status"] != "FAILURE" || !strings.Contains(fmt.Sprint(tr["signals"]), "contradiction") {
		t.Fatalf("trace after human outcome: %v", tr)
	}
	// API keys with traces:write may report outcomes (SDK callbacks).
	sdk := authn.Principal{OrgID: orgA, Actor: "apikey:k1", Role: authn.RoleAPIKey, ProjectIDs: []string{projA}, Scopes: []authn.Scope{authn.ScopeTracesWrite}}
	if code, body := h.as(sdk, "POST", path+"/outcome", map[string]any{"status": "SUCCESS", "verified": true, "verification_source": "external_callback"}); code != 200 {
		t.Fatalf("sdk outcome: %d %v", code, body)
	}
	if code, _ := h.as(engineerA, "POST", path+"/flag", map[string]any{"kind": "incident", "reason": "customer refunded twice"}); code != 201 {
		t.Fatalf("flag: %d", code)
	}
	if code, _ := h.as(viewer, "POST", path+"/flag", map[string]any{"reason": "x"}); code != 403 {
		t.Fatalf("viewer flag: %d", code)
	}
	for _, et := range []string{"trace.outcome_recorded.v1", "trace.flagged.v1"} {
		if n := h.count(`SELECT count(*) FROM trace.outbox WHERE event_type = $1`, et); n < 1 {
			t.Fatalf("no %s event", et)
		}
	}
	// Deletion requires settings.write, cascades and is audited.
	if code, _ := h.as(engineerA, "DELETE", path, nil); code != 403 {
		t.Fatalf("engineer delete: %d", code)
	}
	admin := authn.Principal{OrgID: orgA, Actor: "user:admin", Role: authn.RoleAdmin, AllProjects: true}
	if code, _ := h.as(admin, "DELETE", path, nil); code != 204 {
		t.Fatalf("admin delete: %d", code)
	}
	if n := h.count(`SELECT count(*) FROM trace.span WHERE trace_id = $1`, tid); n != 0 {
		t.Fatalf("spans not deleted: %d", n)
	}
	if n := h.count(`SELECT count(*) FROM trace.outbox WHERE event_type = 'audit.recorded.v1'`); n != 1 {
		t.Fatalf("deletion not audited")
	}
}

func TestBackpressureAndProtobufAndGzip(t *testing.T) {
	h := newHarness(t)
	tid, children, root := candidateSpans("proto")
	// Protobuf request -> protobuf response with partial success.
	req := &coltracepb.ExportTraceServiceRequest{ResourceSpans: []*tracepb.ResourceSpans{{ScopeSpans: []*tracepb.ScopeSpans{{Spans: []*tracepb.Span{
		{TraceId: mustHex(tid), SpanId: mustHex(root.id), Name: "agent.run", StartTimeUnixNano: uint64(baseTime.UnixNano()), EndTimeUnixNano: uint64(baseTime.Add(time.Second).UnixNano()),
			Attributes: []*commonpb.KeyValue{{Key: "agenttwin.agent.name", Value: &commonpb.AnyValue{Value: &commonpb.AnyValue_StringValue{StringValue: "a"}}}}},
		{TraceId: mustHex(tid), SpanId: make([]byte, 8), Name: "bad"},
	}}}}}}
	b, _ := proto.Marshal(req)
	res := h.post("/v1/traces", b, map[string]string{"X-AgentTwin-Api-Key": "atk_aaaaaaaa_ingest", "Content-Type": "application/x-protobuf"})
	raw, _ := io.ReadAll(res.Body)
	_ = res.Body.Close()
	var pr coltracepb.ExportTraceServiceResponse
	if res.StatusCode != 200 || proto.Unmarshal(raw, &pr) != nil || pr.GetPartialSuccess().GetRejectedSpans() != 1 {
		t.Fatalf("protobuf export: %d %v", res.StatusCode, pr.String())
	}
	// Gzip-compressed JSON (the collector's default compression).
	var buf bytes.Buffer
	gz := gzip.NewWriter(&buf)
	_, _ = gz.Write(otlpJSON(tid, demoResource, children...))
	_ = gz.Close()
	res = h.post("/v1/traces", buf.Bytes(), map[string]string{"X-AgentTwin-Api-Key": "atk_aaaaaaaa_ingest", "Content-Encoding": "gzip"})
	_ = res.Body.Close()
	if res.StatusCode != 200 || h.count(`SELECT count(*) FROM trace.span WHERE trace_id = $1`, tid) != 6 {
		t.Fatalf("gzip export: %d", res.StatusCode)
	}
	// When all ingest slots are busy, excess requests are refused with a
	// retryable 503. The blocked request sends 16 KiB of leading whitespace
	// (valid JSON) so its headers leave the client buffer, then stalls.
	h.api.Init(1)
	release := make(chan struct{})
	pipeR, pipeW := io.Pipe()
	go func() {
		_, _ = pipeW.Write(bytes.Repeat([]byte(" "), 16<<10))
		<-release
		_, _ = pipeW.Write(otlpJSON(tid, demoResource, root))
		_ = pipeW.Close()
	}()
	done := make(chan int)
	go func() {
		req, _ := http.NewRequest("POST", h.srv.URL+"/v1/traces", pipeR)
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("X-AgentTwin-Api-Key", "atk_aaaaaaaa_ingest")
		r, err := h.srv.Client().Do(req)
		if err != nil {
			done <- 0
			return
		}
		_ = r.Body.Close()
		done <- r.StatusCode
	}()
	// Wait until the stalled request holds the only slot.
	deadline := time.Now().Add(5 * time.Second)
	for promtest.ToFloat64(h.api.Metrics.InFlight) < 1 {
		if time.Now().After(deadline) {
			close(release)
			t.Fatal("stalled request never reached the ingest handler")
		}
		time.Sleep(5 * time.Millisecond)
	}
	for {
		res := h.post("/v1/traces", otlpJSON(tid, demoResource, root), map[string]string{"X-AgentTwin-Api-Key": "atk_aaaaaaaa_ingest"})
		_ = res.Body.Close()
		if res.StatusCode == 503 {
			if res.Header.Get("Retry-After") == "" {
				t.Fatal("503 without Retry-After")
			}
			break
		}
		if time.Now().After(deadline) {
			close(release)
			t.Fatal("backpressure never engaged")
		}
		time.Sleep(20 * time.Millisecond)
	}
	close(release)
	if code := <-done; code != 200 {
		t.Fatalf("blocked request finished with %d", code)
	}
}

func mustHex(s string) []byte { b, _ := hex.DecodeString(s); return b }

func TestRetentionPurges(t *testing.T) {
	h := newHarness(t)
	tid, children, root := candidateSpans("ret")
	h.ingest("atk_aaaaaaaa_ingest", otlpJSON(tid, demoResource, append(children, root)...))
	ctx := context.Background()
	st := store.New(h.pool)
	if _, err := h.pool.Exec(ctx, `UPDATE trace.trace SET content_expires_at = now() - interval '1 minute'`); err != nil {
		t.Fatal(err)
	}
	if n, err := st.PurgeContent(ctx, 10); err != nil || n != 1 {
		t.Fatalf("purge content: %d %v", n, err)
	}
	if n := h.count(`SELECT count(*) FROM trace.span WHERE content IS NOT NULL`); n != 0 {
		t.Fatalf("content remains on %d spans", n)
	}
	if n := h.count(`SELECT count(*) FROM trace.span`); n != 6 {
		t.Fatal("metadata must survive content purge")
	}
	if _, err := h.pool.Exec(ctx, `UPDATE trace.trace SET expires_at = now() - interval '1 minute'`); err != nil {
		t.Fatal(err)
	}
	if n, err := st.PurgeExpired(ctx, 10); err != nil || n != 1 {
		t.Fatalf("purge expired: %d %v", n, err)
	}
	if n := h.count(`SELECT count(*) FROM trace.span`); n != 0 {
		t.Fatalf("spans survive trace expiry: %d", n)
	}
}

// testWriter routes service logs into the test log (visible on failure).
type testWriter struct{ t *testing.T }

func (w testWriter) Write(p []byte) (int, error) {
	w.t.Helper()
	w.t.Log(strings.TrimSpace(string(p)))
	return len(p), nil
}

// One process may run several agents, versions and run sources (the demo
// agent serves production traffic and simulations). Span-level context set by
// the SDK must win over the process resource, even when the spans carrying it
// arrive in a later batch position than spans without it.
func TestSpanLevelContextOverridesResource(t *testing.T) {
	h := newHarness(t)
	sim := map[string]any{"agenttwin.agent.version": "1.3.1", "agenttwin.source": "simulation",
		"agenttwin.environment": "staging", "agenttwin.release.id": "rel-42", "agenttwin.simulation.run_id": "run-7"}
	tid := hexID(32, "ctx-sim")
	rootID := hexID(16, "ctx-simr")
	plain := spec{id: hexID(16, "ctx-simp"), parent: rootID, name: "execute_tool lookup_order", offsetMS: 5, durMS: 5, status: 1,
		attrs: map[string]any{"gen_ai.tool.name": "lookup_order"}}
	withCtx := spec{id: hexID(16, "ctx-simc"), parent: rootID, name: "execute_tool get_refund_policy", offsetMS: 15, durMS: 5, status: 1,
		attrs: map[string]any{"gen_ai.tool.name": "get_refund_policy"}}
	for k, v := range sim {
		withCtx.attrs[k] = v
	}
	root := spec{id: rootID, name: "agent.run", durMS: 50, status: 1, attrs: map[string]any{"agenttwin.span.kind": "agent"}}
	for k, v := range sim {
		root.attrs[k] = v
	}
	// demoResource says production / 1.3.0; the first span has no span-level context.
	if code, _ := h.ingest("atk_aaaaaaaa_ingest", otlpJSON(tid, demoResource, plain, withCtx, root)); code != 200 {
		t.Fatalf("ingest: %d", code)
	}
	// A trace with resource context only keeps the resource values.
	tidRes, children, rootRes := candidateSpans("ctx-res")
	if code, _ := h.ingest("atk_aaaaaaaa_ingest", otlpJSON(tidRes, demoResource, append(children, rootRes)...)); code != 200 {
		t.Fatal("ingest resource-only trace")
	}
	h.finalizeAll()

	for _, c := range []struct {
		tid                                  string
		version, source, env, release, simID string
	}{
		{tid, "1.3.1", "simulation", "staging", "rel-42", "run-7"},
		{tidRes, "1.3.0", "production", "production", "", ""},
	} {
		var version, source, env, release, simID string
		if err := h.pool.QueryRow(context.Background(), `
			SELECT agent_version, source, environment, COALESCE(release_id, ''), COALESCE(simulation_run_id, '')
			FROM trace.trace WHERE trace_id = $1`, c.tid).Scan(&version, &source, &env, &release, &simID); err != nil {
			t.Fatal(err)
		}
		if version != c.version || source != c.source || env != c.env || release != c.release || simID != c.simID {
			t.Errorf("trace %s: got version=%s source=%s env=%s release=%s sim=%s, want %+v", c.tid, version, source, env, release, simID, c)
		}
	}
	// The published event carries the span-level context too.
	var payload []byte
	if err := h.pool.QueryRow(context.Background(), `SELECT envelope::text FROM trace.outbox
		WHERE event_type = 'trace.ingested.v1' AND envelope->'payload'->>'trace_id' = $1`, tid).Scan(&payload); err != nil {
		t.Fatal(err)
	}
	var env struct {
		Payload map[string]any `json:"payload"`
	}
	if err := json.Unmarshal(payload, &env); err != nil {
		t.Fatal(err)
	}
	if env.Payload["source"] != "simulation" || env.Payload["environment"] != "staging" || env.Payload["agent_version"] != "1.3.1" ||
		env.Payload["simulation_run_id"] != "run-7" {
		t.Fatalf("event payload context: %v", env.Payload)
	}
}

// Span attributes are untrusted input. Oversized identifiers must not fail a
// batch (index entry limits) and out-of-vocabulary outcome values must not
// violate constraints and wedge the finalizer.
func TestHostileAttributesAreBoundedAndNormalized(t *testing.T) {
	h := newHarness(t)
	huge := strings.Repeat("ş", 60000) // 120 KB of 2-byte runes
	tid := hexID(32, "hostile")
	root := hexID(16, "hostiler")
	spans := []spec{
		{id: hexID(16, "hostilet"), parent: root, name: "execute_tool " + huge, offsetMS: 5, durMS: 5, status: 2, attrs: map[string]any{
			"gen_ai.tool.name": huge, "agenttwin.tool.risk": huge, "gen_ai.request.model": huge, "agenttwin.policy.decision": huge,
			"error.type": huge}},
		{id: hexID(16, "hostileo"), parent: root, name: "outcome.verify", offsetMS: 20, durMS: 1, status: 1, attrs: map[string]any{
			"agenttwin.outcome.status": "DONE", "agenttwin.outcome.claimed": "GREAT", "agenttwin.outcome.verified": true,
			"agenttwin.outcome.verification_source": "self_report", "agenttwin.outcome.business": huge}},
		{id: root, name: "agent.run", durMS: 50, status: 1, attrs: map[string]any{"agenttwin.agent.name": huge, "agenttwin.session.id": huge}},
	}
	if code, body := h.ingest("atk_aaaaaaaa_ingest", otlpJSON(tid, demoResource, spans...)); code != 200 {
		t.Fatalf("hostile batch rejected: %d %v", code, body)
	}
	if n := h.finalizeAll(); n != 1 {
		t.Fatalf("finalized %d, want 1 (finalizer must not wedge on bad outcome values)", n)
	}
	var maxTool, maxAgent int
	if err := h.pool.QueryRow(context.Background(), `SELECT max(octet_length(tool_name)), max(octet_length(t.agent_name))
		FROM trace.span s JOIN trace.trace t USING (project_id, trace_id) WHERE s.trace_id = $1`, tid).Scan(&maxTool, &maxAgent); err != nil {
		t.Fatal(err)
	}
	if maxTool > 256 || maxAgent > 256 || maxTool == 0 {
		t.Fatalf("identifiers not bounded: tool=%d agent=%d bytes", maxTool, maxAgent)
	}
	var status, source string
	var verified bool
	var claimed *string
	if err := h.pool.QueryRow(context.Background(), `SELECT status, verification_source, verified, claimed_status
		FROM trace.outcome WHERE trace_id = $1`, tid).Scan(&status, &source, &verified, &claimed); err != nil {
		t.Fatal(err)
	}
	if status != "UNKNOWN" || source != "unavailable" || verified || claimed != nil {
		t.Fatalf("outcome not normalized: status=%s source=%s verified=%v claimed=%v", status, source, verified, claimed)
	}
	// A following, well-formed trace is still processed.
	tid2, children, root2 := candidateSpans("after-hostile")
	if code, _ := h.ingest("atk_aaaaaaaa_ingest", otlpJSON(tid2, demoResource, append(children, root2)...)); code != 200 {
		t.Fatal("ingest after hostile batch")
	}
	if n := h.finalizeAll(); n != 1 {
		t.Fatalf("finalizer stuck after hostile trace: %d", n)
	}
}

// OTLP/HTTP answers in the encoding of the request, errors included, and an
// encoding the endpoint does not take is 415, not 400 (ADR-0021).
func TestOTLPEncodingsAndErrors(t *testing.T) {
	h := newHarness(t)
	with := func(extra map[string]string) map[string]string {
		out := map[string]string{"X-AgentTwin-Api-Key": "atk_aaaaaaaa_ingest"}
		for k, v := range extra {
			out[k] = v
		}
		return out
	}
	read := func(res *http.Response) []byte {
		defer func() { _ = res.Body.Close() }()
		b, _ := io.ReadAll(res.Body)
		return b
	}
	// application/protobuf is an alias: the answer is protobuf, not JSON.
	tid, _, root := candidateSpans("alias")
	req := &coltracepb.ExportTraceServiceRequest{ResourceSpans: []*tracepb.ResourceSpans{{ScopeSpans: []*tracepb.ScopeSpans{{Spans: []*tracepb.Span{
		{TraceId: mustHex(tid), SpanId: mustHex(root.id), Name: "agent.run", StartTimeUnixNano: uint64(baseTime.UnixNano()), EndTimeUnixNano: uint64(baseTime.Add(time.Second).UnixNano())},
	}}}}}}
	b, _ := proto.Marshal(req)
	res := h.post("/v1/traces", b, with(map[string]string{"Content-Type": "application/protobuf"}))
	body := read(res)
	var ok coltracepb.ExportTraceServiceResponse
	if res.StatusCode != 200 || res.Header.Get("Content-Type") != "application/x-protobuf" || proto.Unmarshal(body, &ok) != nil {
		t.Fatalf("application/protobuf export: %d %s %q", res.StatusCode, res.Header.Get("Content-Type"), body)
	}
	// A protobuf request gets a protobuf google.rpc.Status.
	res = h.post("/v1/traces", []byte("\xff\xff not protobuf"), with(map[string]string{"Content-Type": "application/x-protobuf"}))
	body = read(res)
	var st spb.Status
	if res.StatusCode != 400 || res.Header.Get("Content-Type") != "application/x-protobuf" || proto.Unmarshal(body, &st) != nil ||
		codes.Code(st.GetCode()) != codes.InvalidArgument || st.GetMessage() == "" {
		t.Fatalf("protobuf error: %d %s %v", res.StatusCode, res.Header.Get("Content-Type"), st.String())
	}
	res = h.post("/v1/traces", b, map[string]string{"Content-Type": "application/x-protobuf"})
	body = read(res)
	if res.StatusCode != 401 || proto.Unmarshal(body, &st) != nil || codes.Code(st.GetCode()) != codes.Unauthenticated {
		t.Fatalf("protobuf 401: %d %v", res.StatusCode, st.String())
	}
	// Encodings the endpoint does not take are 415, in the request's encoding.
	for _, c := range []struct {
		name string
		hdr  map[string]string
	}{
		{"text/plain", with(map[string]string{"Content-Type": "text/plain"})},
		{"brotli", with(map[string]string{"Content-Encoding": "br"})},
	} {
		res := h.post("/v1/traces", []byte("{}"), c.hdr)
		var out map[string]any
		_ = json.Unmarshal(read(res), &out)
		if res.StatusCode != 415 || out["code"] != float64(codes.InvalidArgument) {
			t.Errorf("%s: %d %v", c.name, res.StatusCode, out)
		}
	}
	// Invalid gzip is a bad request, not an unsupported encoding.
	res = h.post("/v1/traces", []byte("{}"), with(map[string]string{"Content-Encoding": "gzip"}))
	if _ = read(res); res.StatusCode != 400 {
		t.Fatalf("invalid gzip: %d", res.StatusCode)
	}
}

// Malformed ids and filters are 400 with the field, whatever the caller may
// access; a well-formed id outside the caller's access is 404.
func TestQueryValidation(t *testing.T) {
	h := newHarness(t)
	tid, children, root := candidateSpans("valid")
	if code, _ := h.ingest("atk_aaaaaaaa_ingest", otlpJSON(tid, demoResource, append(children, root)...)); code != 200 {
		t.Fatal("ingest")
	}
	h.finalizeAll()
	scoped := authn.Principal{OrgID: orgA, Actor: "apikey:x", Role: authn.RoleAPIKey, ProjectIDs: []string{projA2}, Scopes: []authn.Scope{authn.ScopeRead}}
	for _, c := range []struct {
		p           authn.Principal
		path        string
		status      int
		code, field string
	}{
		{engineerA, "/api/v1/traces/not-hex", 400, "INVALID_PARAMETER", "trace_id"},
		{engineerA, "/api/v1/traces/" + tid + "?project_id=nope", 400, "INVALID_PARAMETER", "project_id"},
		{scoped, "/api/v1/traces/" + tid + "?project_id=nope", 400, "INVALID_PARAMETER", "project_id"},
		{scoped, "/api/v1/traces/" + tid + "?project_id=" + projA, 404, "NOT_FOUND", ""},
		{engineerA, "/api/v1/traces?project_id=nope", 400, "INVALID_FILTER", "project_id"},
		{scoped, "/api/v1/traces?project_id=nope", 400, "INVALID_FILTER", "project_id"},
		{scoped, "/api/v1/traces?project_id=" + projA, 404, "NOT_FOUND", ""},
		{scoped, "/api/v1/trace-stats?project_id=nope", 400, "INVALID_FILTER", "project_id"},
		{scoped, "/api/v1/traces/facets?project_id=nope", 400, "INVALID_FILTER", "project_id"},
		{engineerA, "/api/v1/traces?outcome=GREAT", 400, "INVALID_FILTER", "outcome"},
		{engineerA, "/api/v1/traces?source=prod", 400, "INVALID_FILTER", "source"},
		{engineerA, "/api/v1/traces?min_cost_usd=-1&max_duration_ms=x", 400, "INVALID_FILTER", "max_duration_ms"},
		{engineerA, "/api/v1/trace-stats?from=" + baseTime.Format(time.RFC3339) + "&to=" + baseTime.Add(-time.Hour).Format(time.RFC3339), 400, "INVALID_FILTER", "from"},
	} {
		code, body := h.as(c.p, "GET", c.path, nil)
		e, _ := body["error"].(map[string]any)
		details, _ := e["details"].(map[string]any)
		field, _ := details["field"].(string)
		if code != c.status || e["code"] != c.code || field != c.field {
			t.Errorf("%s: %d %v %q, want %d %s %q", c.path, code, e["code"], field, c.status, c.code, c.field)
		}
	}
	// A project id is a UUID whatever its case: a key scoped to the project
	// reads it in upper case too.
	own := authn.Principal{OrgID: orgA, Actor: "apikey:own", Role: authn.RoleAPIKey, ProjectIDs: []string{projA}, Scopes: []authn.Scope{authn.ScopeRead}}
	for _, path := range []string{"/api/v1/traces/" + tid + "?project_id=", "/api/v1/traces?project_id="} {
		if code, body := h.as(own, "GET", path+strings.ToUpper(projA), nil); code != 200 {
			t.Errorf("%s<upper-case id>: %d %v", path, code, body)
		}
	}
	// Enumerated filters take the documented values, exactly.
	_, detail := h.as(engineerA, "GET", "/api/v1/traces/"+tid, nil)
	tr := detail["trace"].(map[string]any)
	for _, f := range []struct{ name, field string }{{"source", "source"}, {"outcome", "outcome_status"}, {"status", "status"}} {
		name, value := f.name, tr[f.field].(string)
		if code, body := h.as(engineerA, "GET", "/api/v1/traces?"+name+"="+value, nil); code != 200 || len(body["items"].([]any)) != 1 {
			t.Errorf("%s=%s: %d %v", name, value, code, body)
		}
		other := strings.ToLower(value)
		if other == value {
			other = strings.ToUpper(value)
		}
		if code, _ := h.as(engineerA, "GET", "/api/v1/traces?"+name+"="+other, nil); code != 400 {
			t.Errorf("%s=%s: %d, want 400", name, other, code)
		}
	}
	if code, _ := h.as(engineerA, "POST", "/api/v1/traces/"+tid+"/outcome", map[string]any{"status": "failure"}); code != 400 {
		t.Errorf("a lower-case outcome status: %d, want 400", code)
	}
	// The last page has a null cursor, not an empty one.
	if code, body := h.as(engineerA, "GET", "/api/v1/traces", nil); code != 200 || body["next_cursor"] != nil {
		t.Fatalf("last page: %d next_cursor=%#v", code, body["next_cursor"])
	}
}

// Outcome and flag limits count characters, as the document (JSON Schema)
// and the database do, not bytes; recorded states are objects.
func TestOutcomeAndFlagLimits(t *testing.T) {
	h := newHarness(t)
	tid, children, root := candidateSpans("limits")
	children = children[:4] // no outcome span
	if code, _ := h.ingest("atk_aaaaaaaa_ingest", otlpJSON(tid, demoResource, append(children, root)...)); code != 200 {
		t.Fatal("ingest")
	}
	path := "/api/v1/traces/" + tid
	for _, c := range []struct {
		body   map[string]any
		status int
		field  string
	}{
		{map[string]any{"status": "FAILURE", "expected_state": []any{1}}, 400, "expected_state"},
		{map[string]any{"status": "FAILURE", "actual_state": "state"}, 400, "actual_state"},
		{map[string]any{"status": "FAILURE", "business_outcome": strings.Repeat("x", 201)}, 400, "business_outcome"},
		{map[string]any{"status": "FAILURE", "notes": strings.Repeat("ş", 4001)}, 400, "notes"},
		// 4000 two-byte characters are 8000 bytes: within the limit.
		{map[string]any{"status": "FAILURE", "notes": strings.Repeat("ş", 4000), "business_outcome": strings.Repeat("ğ", 200),
			"expected_state": nil, "actual_state": map[string]any{"refund_count": 2}}, 200, ""},
	} {
		code, body := h.as(engineerA, "POST", path+"/outcome", c.body)
		e, _ := body["error"].(map[string]any)
		details, _ := e["details"].(map[string]any)
		if field, _ := details["field"].(string); code != c.status || field != c.field {
			t.Errorf("outcome %v: %d %v, want %d %q", c.body["status"], code, body, c.status, c.field)
		}
	}
	if code, body := h.as(engineerA, "POST", path+"/flag", map[string]any{"reason": strings.Repeat("ş", 2000)}); code != 201 || body["kind"] != "manual" {
		t.Fatalf("a 2000-character reason: %d %v", code, body)
	}
	if code, _ := h.as(engineerA, "POST", path+"/flag", map[string]any{"reason": strings.Repeat("ş", 2001)}); code != 400 {
		t.Fatalf("a 2001-character reason: %d", code)
	}
	if code, _ := h.as(engineerA, "POST", path+"/flag", map[string]any{"reason": "   "}); code != 400 {
		t.Fatalf("a blank reason: %d", code)
	}
}

// Numeric facts arrive as untrusted attributes, strings included. A
// non-finite number cannot be stored as JSON: it must not fail the export —
// a retryable 503 the collector would repeat for the whole batch — and
// negative counts and costs must not reach the aggregates.
func TestNonFiniteAndNegativeNumbersAreDropped(t *testing.T) {
	h := newHarness(t)
	tid := hexID(32, "numbers")
	root := hexID(16, "numbersr")
	spans := []spec{
		{id: hexID(16, "numbersm"), parent: root, name: "chat m", offsetMS: 5, durMS: 5, status: 1, attrs: map[string]any{
			"gen_ai.operation.name": "chat", "gen_ai.request.model": "m", "gen_ai.usage.input_tokens": -900,
			"gen_ai.usage.output_tokens": "-40", "agenttwin.cost.usd": "NaN", "gen_ai.request.temperature": "Infinity",
			"gen_ai.request.max_tokens": -1}},
		{id: hexID(16, "numberst"), parent: root, name: "execute_tool t", offsetMS: 20, durMS: 5, status: 1, attrs: map[string]any{
			"gen_ai.tool.name": "t", "agenttwin.cost.usd": -3.5, "agenttwin.tool.attempt": -2}},
		{id: root, name: "agent.run", durMS: 50, status: 1, attrs: map[string]any{"agenttwin.span.kind": "agent"}},
	}
	if code, body := h.ingest("atk_aaaaaaaa_ingest", otlpJSON(tid, demoResource, spans...)); code != 200 {
		t.Fatalf("export with non-finite and negative numbers: %d %v", code, body)
	}
	if n := h.finalizeAll(); n != 1 {
		t.Fatalf("finalized %d, want 1", n)
	}
	code, detail := h.as(engineerA, "GET", "/api/v1/traces/"+tid, nil)
	if code != 200 {
		t.Fatalf("detail: %d %v", code, detail)
	}
	tr := detail["trace"].(map[string]any)
	if tr["input_tokens"].(float64) != 0 || tr["output_tokens"].(float64) != 0 || tr["cost_usd"] != nil {
		t.Fatalf("negative or non-finite numbers reached the aggregates: tokens=%v/%v cost=%v", tr["input_tokens"], tr["output_tokens"], tr["cost_usd"])
	}
	for _, sp := range detail["spans"].([]any) {
		attrs := sp.(map[string]any)["attributes"].(map[string]any)
		for _, k := range []string{"input_tokens", "output_tokens", "cost_usd", "temperature", "max_tokens", "attempt"} {
			if v, ok := attrs[k]; ok {
				t.Errorf("span %v kept %s=%v", sp.(map[string]any)["name"], k, v)
			}
		}
	}
}

// An error while claiming a trace is reported (and counted as a failed
// attempt), never mistaken for "another replica took it". A row the finalizer
// cannot decode fails on the client side without aborting the transaction, so
// the commit would succeed and the trace would silently stay dirty forever.
func TestFinalizerReportsClaimErrors(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()
	var failures atomic.Int64
	h.fin.OnError = func() { failures.Add(1) }
	tid, children, root := candidateSpans("claim-error")
	if code, _ := h.ingest("atk_aaaaaaaa_ingest", otlpJSON(tid, demoResource, append(children, root)...)); code != 200 {
		t.Fatal("ingest")
	}
	// A manual edit leaves a value the claim cannot decode.
	if _, err := h.pool.Exec(ctx, `ALTER TABLE trace.trace ALTER COLUMN environment DROP NOT NULL;
		UPDATE trace.trace SET environment = NULL WHERE trace_id = '`+tid+`'`); err != nil {
		t.Fatal(err)
	}
	n, err := h.fin.Tick(ctx)
	if n != 0 || err == nil || !strings.Contains(err.Error(), tid) {
		t.Fatalf("claim failure must be reported: finalized=%d err=%v", n, err)
	}
	if failures.Load() != 1 || h.count(`SELECT finalize_attempts FROM trace.trace WHERE trace_id = $1`, tid) != 1 {
		t.Fatalf("failed attempt not recorded: failures=%d", failures.Load())
	}
	if _, err := h.pool.Exec(ctx, `UPDATE trace.trace SET environment = 'staging' WHERE trace_id = '`+tid+`';
		ALTER TABLE trace.trace ALTER COLUMN environment SET NOT NULL`); err != nil {
		t.Fatal(err)
	}
	if n := h.finalizeAll(); n != 1 {
		t.Fatalf("trace not finalized after recovery: %d", n)
	}
}

// A trace that cannot be finalized (simulated with a failing trigger) must not
// block other traces, is retried a bounded number of times, and is retried
// again once new spans arrive.
func TestFinalizerIsolatesPoisonTraces(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()
	var failures atomic.Int64
	h.fin.OnError = func() { failures.Add(1) }
	poison, pc, pr := candidateSpans("poison")
	healthy, hc, hr := candidateSpans("healthy")
	for _, b := range [][]byte{otlpJSON(poison, demoResource, append(pc, pr)...), otlpJSON(healthy, demoResource, append(hc, hr)...)} {
		if code, _ := h.ingest("atk_aaaaaaaa_ingest", b); code != 200 {
			t.Fatal("ingest")
		}
	}
	if _, err := h.pool.Exec(ctx, fmt.Sprintf(`
		CREATE FUNCTION trace.fail_poison() RETURNS trigger LANGUAGE plpgsql AS $$
		BEGIN IF NEW.trace_id = '%s' AND NOT NEW.dirty THEN RAISE EXCEPTION 'simulated finalize bug'; END IF; RETURN NEW; END $$;
		CREATE TRIGGER fail_poison BEFORE UPDATE ON trace.trace FOR EACH ROW EXECUTE FUNCTION trace.fail_poison();`, poison)); err != nil {
		t.Fatal(err)
	}
	n, err := h.fin.Tick(ctx)
	if n != 1 || err == nil || !strings.Contains(err.Error(), poison) {
		t.Fatalf("first tick: finalized=%d err=%v (healthy trace must finalize, poison must report)", n, err)
	}
	if h.count(`SELECT count(*) FROM trace.trace WHERE trace_id = $1 AND NOT dirty`, healthy) != 1 {
		t.Fatal("healthy trace not finalized")
	}
	for i := 0; i < 3*finalizer.MaxAttempts; i++ {
		_, _ = h.fin.Tick(ctx)
	}
	if got := failures.Load(); got != finalizer.MaxAttempts {
		t.Fatalf("poison trace attempted %d times, want exactly %d", got, finalizer.MaxAttempts)
	}
	if n, err := h.fin.Tick(ctx); n != 0 || err != nil {
		t.Fatalf("exhausted poison trace must no longer be a candidate: n=%d err=%v", n, err)
	}
	// New data resets the budget; with the bug fixed the trace finalizes.
	if _, err := h.pool.Exec(ctx, `DROP TRIGGER fail_poison ON trace.trace`); err != nil {
		t.Fatal(err)
	}
	late := spec{id: hexID(16, "poisonlate"), parent: pr.id, name: "execute_tool send_email", offsetMS: 1950, durMS: 5, status: 1,
		attrs: map[string]any{"gen_ai.tool.name": "send_email"}}
	if code, _ := h.ingest("atk_aaaaaaaa_ingest", otlpJSON(poison, demoResource, late)); code != 200 {
		t.Fatal("late span")
	}
	if n := h.finalizeAll(); n != 1 {
		t.Fatalf("poison trace not retried after new data: %d", n)
	}
}
