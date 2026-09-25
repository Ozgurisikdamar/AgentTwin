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
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/Ozgurisikdamar/AgentTwin/packages/contracts"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/authn"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/db"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/events"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/httpx"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/ids"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/openapicheck"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/testutil"
	"github.com/Ozgurisikdamar/AgentTwin/services/graph-service/internal/api"
	"github.com/Ozgurisikdamar/AgentTwin/services/graph-service/internal/ingest"
	"github.com/Ozgurisikdamar/AgentTwin/services/graph-service/internal/store"
	"github.com/Ozgurisikdamar/AgentTwin/services/graph-service/migrations"
)

const internalSecret = "graph-test-internal-secret-0123456789abcdef"

// contract is the graph API document every exchange of these tests is held
// to (ADR-0021).
var contract = func() *openapicheck.Contract {
	c, err := openapicheck.Load(contracts.OpenAPI, "openapi/graph-service.openapi.yaml")
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
			fmt.Fprintf(os.Stderr, "graph-service contract: no checked successful exchange for %s\n", strings.Join(missing, ", "))
			code = 1
		}
	}
	os.Exit(code)
}

type harness struct {
	t        *testing.T
	srv      *httptest.Server
	pool     *pgxpool.Pool
	tokens   *authn.TokenService
	api      *api.Server
	handlers map[string]events.Handler
	org      string
	project  string
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
	h := &harness{t: t, pool: pool, org: ids.New(), project: ids.New()}
	h.tokens, _ = authn.NewTokenService(internalSecret)
	h.api = &api.Server{Store: store.New(pool), Tokens: h.tokens, Log: slog.New(slog.NewJSONHandler(io.Discard, nil))}
	h.handlers = h.api.Handlers()
	mux := http.NewServeMux()
	h.api.Routes(mux)
	h.srv = httptest.NewServer(contract.Checking(httpx.Chain(mux, httpx.RequestID()), func(err error) {
		t.Errorf("the graph service broke its contract:\n%v", err)
	}))
	t.Cleanup(h.srv.Close)
	return h
}

// ---- events

var validator = func() *events.Validator {
	v, err := events.DefaultValidator()
	if err != nil {
		panic(err)
	}
	return v
}()

// event builds an envelope that satisfies the event contract.
func (h *harness) event(typ string, at time.Time, payload any) events.Envelope {
	h.t.Helper()
	env, err := events.New(typ, "test", h.org, h.project, "", "", payload)
	if err != nil {
		h.t.Fatal(err)
	}
	env.OccurredAt = at
	if err := validator.Validate(env); err != nil {
		h.t.Fatalf("test event %s breaks its contract: %v", typ, err)
	}
	return env
}

func (h *harness) handle(env events.Envelope) error {
	return h.handlers[env.Type](context.Background(), env)
}

func (h *harness) apply(env events.Envelope) {
	h.t.Helper()
	if err := h.handle(env); err != nil {
		h.t.Fatalf("%s: %v", env.Type, err)
	}
}

var t0 = time.Date(2026, 9, 1, 9, 0, 0, 0, time.UTC)

func versionPayload(version, prompt string, tools map[string]string, deps []map[string]any) map[string]any {
	var list []map[string]any
	names := make([]string, 0, len(tools))
	for n := range tools {
		names = append(names, n)
	}
	slices.Sort(names)
	for _, n := range names {
		list = append(list, map[string]any{"name": n, "risk": tools[n]})
	}
	return map[string]any{
		"agent_id": ids.New(), "agent_name": "support-refund-agent", "version_id": ids.New(), "version": version,
		"manifest_hash": strings.Repeat("0", 63) + version[len(version)-1:], "prompt_hash": prompt,
		"model":             map[string]any{"provider": "scripted", "name": "scripted-planner-v1"},
		"tools":             list,
		"retrieval_sources": []string{"support-kb"},
		"dependencies":      deps,
	}
}

var demoTools = map[string]string{
	"lookup_order": "READ", "get_refund_policy": "READ", "refund_payment": "WRITE_IRREVERSIBLE",
	"send_email": "WRITE_REVERSIBLE", "export_customer_data": "ADMIN",
}

var demoDeps = []map[string]any{
	{"tool": "refund_payment", "kind": "SERVICE", "name": "payments-api", "relation": "CALLS", "criticality": "CRITICAL"},
	{"tool": "refund_payment", "kind": "DATABASE", "name": "payments-db", "relation": "WRITES", "criticality": "CRITICAL"},
	{"tool": "lookup_order", "kind": "SERVICE", "name": "orders-api", "relation": "CALLS", "criticality": "HIGH"},
	{"tool": "send_email", "kind": "EXTERNAL_SYSTEM", "name": "email-provider", "relation": "CALLS", "criticality": "MEDIUM"},
}

func scenarioPayload(name, severity string, covers ...string) map[string]any {
	return map[string]any{
		"scenario_id": ids.New(), "scenario_version_id": ids.New(), "name": name, "agent": "support-refund-agent",
		"severity": severity, "tags": []string{"refunds"}, "covers": covers, "spec_hash": "h-" + name,
	}
}

// demo loads Demo Co's graph: two versions differing in their prompt, the
// payments OpenAPI import, scenarios, a policy and one production trace.
func (h *harness) demo() {
	h.t.Helper()
	// 1.3.0 is registered after 1.2.4 but its event is handled first: latest
	// follows registration time, not arrival order.
	h.apply(h.event("agent.version_registered.v1", t0.Add(time.Hour), versionPayload("1.3.0", "sha256:bbbb", demoTools, demoDeps)))
	h.apply(h.event("agent.version_registered.v1", t0, versionPayload("1.2.4", "sha256:aaaa", demoTools, demoDeps)))
	h.apply(h.event("tool.catalog_imported.v1", t0, map[string]any{
		"source": "OPENAPI", "source_name": "payments-openapi", "service": "payments-api",
		"tools": []map[string]any{
			{"name": "refund_payment", "risk": "WRITE_IRREVERSIBLE", "method": "POST", "path": "/refunds", "mutating": true},
			{"name": "get_refund", "risk": "READ", "method": "GET", "path": "/refunds/{id}", "mutating": false},
		},
	}))
	for _, s := range []map[string]any{
		scenarioPayload("refund-happy-path", "high", "tool:lookup_order", "tool:get_refund_policy", "tool:refund_payment", "tool:send_email", "policy:refund-limit"),
		scenarioPayload("refund-timeout-after-mutation", "critical", "tool:refund_payment", "fault:timeout_after_mutation"),
		scenarioPayload("unauthorized-admin-tool", "critical", "tool:export_customer_data"),
		scenarioPayload("malicious-retrieved-content", "critical", "retrieval:support-kb", "threat:indirect-prompt-injection"),
		scenarioPayload("refund-policy-lookup", "medium", "tool:get_refund_policy"),
	} {
		h.apply(h.event("scenario.upserted.v1", t0, s))
	}
	h.apply(h.event("policy.activated.v1", t0, map[string]any{
		"policy_id": ids.New(), "policy_version_id": ids.New(), "name": "refund-limit", "version": 1,
		"target_tool": "refund_payment", "spec_hash": strings.Repeat("c", 64),
	}))
	h.apply(h.event("trace.ingested.v1", t0.Add(2*time.Hour), tracePayload("production", 3)))
}

func tracePayload(source string, count int) map[string]any {
	return map[string]any{
		"trace_id": strings.Repeat("d", 32), "agent": "support-refund-agent", "agent_version": "1.3.0",
		"environment": "production", "started_at": t0.Format(time.RFC3339), "signals": []string{}, "source": source,
		"summary": map[string]any{"tools": []string{"refund_payment"}, "errors": []string{}, "violations": []string{}},
		"observed_tools": []map[string]any{
			{"name": "refund_payment", "risk": "WRITE_IRREVERSIBLE", "http_host": "payments.internal", "count": count},
		},
	}
}

// ---- HTTP

var (
	engineer = func(org string) authn.Principal {
		return authn.Principal{OrgID: org, Actor: "user:eng", Role: authn.RoleEngineer, AllProjects: true}
	}
	viewer = func(org string) authn.Principal {
		return authn.Principal{OrgID: org, Actor: "user:view", Role: authn.RoleViewer, AllProjects: true}
	}
)

func (h *harness) as(p authn.Principal, method, path string, body any) (int, map[string]any) {
	h.t.Helper()
	var rdr io.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		rdr = bytes.NewReader(b)
	}
	req, _ := http.NewRequest(method, h.srv.URL+path, rdr)
	tok, err := h.tokens.Mint(p, "graph-service", "rid-graph-test")
	if err != nil {
		h.t.Fatal(err)
	}
	req.Header.Set("Authorization", "Bearer "+tok)
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

func (h *harness) get(path string) map[string]any {
	h.t.Helper()
	status, out := h.as(engineer(h.org), "GET", path, nil)
	if status != 200 {
		h.t.Fatalf("GET %s: %d %v", path, status, out)
	}
	return out
}

// componentID resolves a component through the list API.
func (h *harness) componentID(kind, key string) string {
	h.t.Helper()
	out := h.get("/api/v1/graph/components?project_id=" + h.project + "&kind=" + kind + "&limit=200")
	for _, it := range out["items"].([]any) {
		c := it.(map[string]any)
		if c["key"] == key {
			return c["id"].(string)
		}
	}
	h.t.Fatalf("no %s %s", kind, key)
	return ""
}

func (h *harness) detail(kind, key string) map[string]any {
	h.t.Helper()
	return h.get("/api/v1/graph/components/" + h.componentID(kind, key))
}

// relation finds the relation of type typ to or from the other component.
func relation(t *testing.T, detail map[string]any, typ, dir, otherKind, otherKey string) map[string]any {
	t.Helper()
	if r := findRelation(detail, typ, dir, otherKind, otherKey); r != nil {
		return r
	}
	t.Fatalf("no %s %s relation with %s:%s", dir, typ, otherKind, otherKey)
	return nil
}

func findRelation(detail map[string]any, typ, dir, otherKind, otherKey string) map[string]any {
	for _, r := range detail["relations"].([]any) {
		rel := r.(map[string]any)
		c := rel["component"].(map[string]any)
		if rel["type"] == typ && rel["direction"] == dir && c["kind"] == otherKind && c["key"] == otherKey {
			return rel
		}
	}
	return nil
}

func evidenceRefs(rel map[string]any) []string {
	var out []string
	for _, e := range rel["evidence"].([]any) {
		ev := e.(map[string]any)
		out = append(out, ev["source"].(string)+" "+ev["source_ref"].(string))
	}
	return out
}

func (h *harness) blast(p authn.Principal, body map[string]any) (int, map[string]any) {
	h.t.Helper()
	if _, ok := body["project_id"]; !ok {
		body["project_id"] = h.project
	}
	return h.as(p, "POST", "/api/v1/blast-radius", body)
}

func names(list any) []string {
	var out []string
	for _, it := range list.([]any) {
		c := it.(map[string]any)["component"].(map[string]any)
		out = append(out, c["key"].(string))
	}
	return out
}

// ---- tests

func TestEventsBuildTheGraphWithItsEvidence(t *testing.T) {
	h := newHarness(t)
	h.demo()

	// The latest version follows registration time.
	v130 := h.detail("AGENT_VERSION", "support-refund-agent@1.3.0")["component"].(map[string]any)
	v124 := h.detail("AGENT_VERSION", "support-refund-agent@1.2.4")["component"].(map[string]any)
	if v130["attributes"].(map[string]any)["latest"] != true || v124["attributes"].(map[string]any)["latest"] != false {
		t.Fatalf("latest: 1.3.0 %v, 1.2.4 %v", v130["attributes"], v124["attributes"])
	}

	// A system both manifests declare: one edge, two pieces of evidence.
	payments := h.detail("SERVICE", "payments-api")
	calls := relation(t, payments, "CALLS", "in", "TOOL", "refund_payment")
	if got := evidenceRefs(calls); !slices.Equal(got, []string{
		"MANIFEST manifest:support-refund-agent@1.2.4", "MANIFEST manifest:support-refund-agent@1.3.0",
	}) || calls["confidence"] != 1.0 || calls["certain"] != true {
		t.Errorf("payments-api CALLS evidence = %v (%v)", got, calls)
	}
	// The import: an HTTP API the tool can mutate, at the import's confidence.
	refund := h.detail("TOOL", "refund_payment")
	mut := relation(t, refund, "CAN_MUTATE", "out", "HTTP_API", "payments-openapi")
	if mut["confidence"] != 0.9 || !slices.Equal(evidenceRefs(mut), []string{"OPENAPI openapi:payments-openapi"}) {
		t.Errorf("imported mutation = %v", mut)
	}
	detail := mut["evidence"].([]any)[0].(map[string]any)["detail"].(map[string]any)
	if detail["method"] != "POST" || detail["path"] != "/refunds" {
		t.Errorf("operation detail = %v", detail)
	}
	attrs := refund["component"].(map[string]any)["attributes"].(map[string]any)
	if attrs["risk"] != "WRITE_IRREVERSIBLE" || attrs["imported_risk"] != "WRITE_IRREVERSIBLE" {
		t.Errorf("tool attributes = %v", attrs)
	}
	relation(t, refund, "GUARDED_BY", "out", "POLICY", "refund-limit")
	relation(t, refund, "TESTED_BY", "out", "SCENARIO", "refund-timeout-after-mutation")

	// Production usage is observed and counted.
	used := relation(t, refund, "USES", "in", "AGENT_VERSION", "support-refund-agent@1.3.0")
	if got := evidenceRefs(used); !slices.Equal(got, []string{"MANIFEST manifest:support-refund-agent@1.3.0", "OBSERVED traces"}) {
		t.Errorf("use evidence = %v", got)
	}
	for _, e := range used["evidence"].([]any) {
		if ev := e.(map[string]any); ev["source"] == "OBSERVED" && ev["observations"] != 3.0 {
			t.Errorf("observations = %v", ev["observations"])
		}
	}
	relation(t, refund, "CALLS", "out", "HTTP_API", "payments.internal")

	// The scenario keeps what does not name a component as attributes.
	s := h.detail("SCENARIO", "refund-timeout-after-mutation")["component"].(map[string]any)["attributes"].(map[string]any)
	if s["severity"] != "critical" || !slices.Equal(toStrings(s["covers_other"]), []string{"fault:timeout_after_mutation"}) {
		t.Errorf("scenario attributes = %v", s)
	}
}

func toStrings(v any) []string {
	var out []string
	for _, x := range v.([]any) {
		out = append(out, x.(string))
	}
	return out
}

func TestAnEventIsAppliedOnce(t *testing.T) {
	h := newHarness(t)
	h.demo()
	trace := h.event("trace.ingested.v1", t0.Add(3*time.Hour), tracePayload("production", 2))
	h.apply(trace)
	h.apply(trace) // redelivered
	used := relation(t, h.detail("TOOL", "refund_payment"), "USES", "in", "AGENT_VERSION", "support-refund-agent@1.3.0")
	for _, e := range used["evidence"].([]any) {
		if ev := e.(map[string]any); ev["source"] == "OBSERVED" && ev["observations"] != 5.0 {
			t.Errorf("3 + 2 observations, redelivery not counted: got %v", ev["observations"])
		}
	}
	// A simulation's trace calls twins, not the real systems.
	h.apply(h.event("trace.ingested.v1", t0, map[string]any{
		"trace_id": strings.Repeat("e", 32), "agent": "support-refund-agent", "agent_version": "1.3.0",
		"environment": "staging", "signals": []string{}, "source": "simulation",
		"summary":        map[string]any{"tools": []string{}, "errors": []string{}, "violations": []string{}},
		"observed_tools": []map[string]any{{"name": "twin_only_tool", "count": 1}},
	}))
	if out := h.get("/api/v1/graph/components?project_id=" + h.project + "&q=twin_only"); len(out["items"].([]any)) != 0 {
		t.Errorf("a simulation's tool entered the graph: %v", out)
	}
}

func TestNewerDeclarationsReplaceOlderOnes(t *testing.T) {
	h := newHarness(t)
	h.demo()
	// The scenario stops covering send_email: that edge had only this
	// evidence and goes; the others stay.
	happy := scenarioPayload("refund-happy-path", "high", "tool:lookup_order", "tool:refund_payment")
	h.apply(h.event("scenario.upserted.v1", t0.Add(time.Hour), happy))
	sc := h.detail("SCENARIO", "refund-happy-path")
	if findRelation(sc, "TESTED_BY", "in", "TOOL", "send_email") != nil {
		t.Error("a dropped cover is still an edge")
	}
	relation(t, sc, "TESTED_BY", "in", "TOOL", "refund_payment")
	relation(t, sc, "TESTED_BY", "in", "AGENT", "support-refund-agent")

	// Re-importing without an operation removes it; the manifest's evidence
	// on other edges of the same tool is untouched.
	h.apply(h.event("tool.catalog_imported.v1", t0.Add(time.Hour), map[string]any{
		"source": "OPENAPI", "source_name": "payments-openapi", "service": "payments-api",
		"tools": []map[string]any{{"name": "get_refund", "risk": "READ", "method": "GET", "path": "/refunds/{id}", "mutating": false}},
	}))
	refund := h.detail("TOOL", "refund_payment")
	if findRelation(refund, "CAN_MUTATE", "out", "HTTP_API", "payments-openapi") != nil {
		t.Error("an operation dropped from the document is still an edge")
	}
	relation(t, refund, "CALLS", "out", "SERVICE", "payments-api")

	// A policy retargeted guards its new tool only.
	h.apply(h.event("policy.activated.v1", t0.Add(time.Hour), map[string]any{
		"policy_id": ids.New(), "policy_version_id": ids.New(), "name": "refund-limit", "version": 2,
		"target_tool": "send_email", "spec_hash": strings.Repeat("d", 64),
	}))
	pol := h.detail("POLICY", "refund-limit")
	if findRelation(pol, "GUARDED_BY", "in", "TOOL", "refund_payment") != nil {
		t.Error("the old target is still guarded")
	}
	relation(t, pol, "GUARDED_BY", "in", "TOOL", "send_email")
	if v := pol["component"].(map[string]any)["attributes"].(map[string]any)["version"]; v != 2.0 {
		t.Errorf("policy version = %v", v)
	}
}

func TestMalformedEventsAreParkedNotRetried(t *testing.T) {
	h := newHarness(t)
	bad := events.Envelope{ID: ids.New(), Type: "agent.version_registered.v1", OccurredAt: t0, OrganizationID: h.org,
		ProjectID: &h.project, Payload: json.RawMessage(`{"agent_name":"","version":""}`)}
	if err := h.handle(bad); !events.IsPermanent(err) {
		t.Errorf("an unmappable payload: %v, want a permanent error", err)
	}
	for _, env := range []events.Envelope{
		{ID: ids.New(), Type: "policy.activated.v1", OrganizationID: h.org, Payload: json.RawMessage(`{}`)},
		{ID: ids.New(), Type: "policy.activated.v1", OrganizationID: "not-a-uuid", ProjectID: &h.project, Payload: json.RawMessage(`{}`)},
	} {
		if err := h.handle(env); err != nil {
			t.Errorf("an event outside any project is acknowledged, got %v", err)
		}
	}
	// A trace with nothing to graph writes nothing.
	h.apply(h.event("trace.ingested.v1", t0, tracePayload("replay", 1)))
	if n := h.count(`SELECT count(*) FROM graph.component`); n != 0 {
		t.Errorf("%d components after ignored events", n)
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

func TestConcurrentEventsOfOneProject(t *testing.T) {
	h := newHarness(t)
	// Many events naming the same components, handled at once as the
	// consumer's goroutines would: none may fail (no deadlock), and the
	// latest version is decided by registration time.
	var envs []events.Envelope
	for i := range 12 {
		envs = append(envs, h.event("scenario.upserted.v1", t0, scenarioPayload(fmt.Sprintf("s-%02d", i), "high",
			"tool:refund_payment", "tool:lookup_order", "tool:send_email", "retrieval:support-kb")))
	}
	for i, v := range []string{"1.0.0", "1.1.0", "1.2.0", "1.3.0"} {
		envs = append(envs, h.event("agent.version_registered.v1", t0.Add(time.Duration(i)*time.Minute), versionPayload(v, "sha256:"+v, demoTools, demoDeps)))
	}
	var wg sync.WaitGroup
	errs := make(chan error, len(envs))
	for _, env := range envs {
		wg.Add(1)
		go func() {
			defer wg.Done()
			errs <- h.handle(env)
		}()
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		if err != nil {
			t.Errorf("concurrent handling: %v", err)
		}
	}
	latest := h.count(`SELECT count(*) FROM graph.component WHERE kind = 'AGENT_VERSION' AND attributes->>'latest' = 'true'`)
	var key string
	if err := h.pool.QueryRow(context.Background(), `SELECT key FROM graph.component WHERE attributes->>'latest' = 'true'`).Scan(&key); err != nil || latest != 1 || key != "support-refund-agent@1.3.0" {
		t.Errorf("latest: %d versions, %q (%v)", latest, key, err)
	}
	if n := h.count(`SELECT count(*) FROM graph.dependency_edge WHERE type = 'TESTED_BY'`); n != 12*5 {
		t.Errorf("%d TESTED_BY edges, want 60", n)
	}
}

// Writes to one project's graph are serialized (a second waits for the
// first to commit, even when they share no row); another project's are not
// held up.
func TestWritesToOneProjectAreSerialized(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()
	st := store.New(h.pool)
	scope := store.Scope{OrgID: h.org, ProjectID: h.project}
	facts := func(tool string) ingest.Facts {
		f, err := ingest.FromMapping(tool, ingest.Mapping{})
		if err != nil {
			t.Fatal(err)
		}
		return f
	}
	first, err := h.pool.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = first.Rollback(ctx) }()
	if err := st.Apply(ctx, first, scope, facts("first_tool"), t0); err != nil {
		t.Fatal(err)
	}
	second := func(sc store.Scope) chan error {
		done := make(chan error, 1)
		go func() {
			done <- pgx.BeginFunc(ctx, h.pool, func(tx pgx.Tx) error { return st.Apply(ctx, tx, sc, facts("second_tool"), t0) })
		}()
		return done
	}
	elsewhere := second(store.Scope{OrgID: h.org, ProjectID: ids.New()})
	select {
	case err := <-elsewhere:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("a write to another project waited for this one")
	}
	same := second(scope)
	select {
	case err := <-same:
		t.Fatalf("a second write to the project ran while the first was open (%v)", err)
	case <-time.After(300 * time.Millisecond):
	}
	if err := first.Commit(ctx); err != nil {
		t.Fatal(err)
	}
	if err := <-same; err != nil {
		t.Fatal(err)
	}
}

func TestTheGraphView(t *testing.T) {
	h := newHarness(t)
	h.demo()
	// Without a focus: every agent's latest version.
	out := h.get("/api/v1/graph?project_id=" + h.project)
	focus := out["focus"].([]any)
	if len(focus) != 1 || focus[0].(map[string]any)["key"] != "support-refund-agent@1.3.0" {
		t.Fatalf("focus = %v", focus)
	}
	var keys []string
	for _, n := range out["nodes"].([]any) {
		keys = append(keys, n.(map[string]any)["key"].(string))
	}
	if slices.Contains(keys, "sha256:aaaa") || !slices.Contains(keys, "sha256:bbbb") || !slices.Contains(keys, "payments-api") {
		t.Errorf("depth-2 view of 1.3.0: %v", keys)
	}
	totals := out["totals"].(map[string]any)
	if totals["components"].(map[string]any)["AGENT_VERSION"] != 2.0 || totals["edges"].(float64) < 30 {
		t.Errorf("totals = %v", totals)
	}
	// Focused, filtered and bounded.
	id := h.componentID("TOOL", "refund_payment")
	out = h.get("/api/v1/graph?project_id=" + h.project + "&focus=" + id + "&depth=1&kinds=SERVICE,DATABASE")
	keys = nil
	for _, n := range out["nodes"].([]any) {
		keys = append(keys, n.(map[string]any)["kind"].(string)+":"+n.(map[string]any)["key"].(string))
	}
	if !slices.Equal(keys, []string{"TOOL:refund_payment", "SERVICE:payments-api", "DATABASE:payments-db"}) {
		t.Errorf("filtered view = %v", keys)
	}
	if out := h.get("/api/v1/graph?project_id=" + h.project + "&focus=" + id + "&limit=3"); len(out["nodes"].([]any)) != 3 || out["truncated"] != true {
		t.Errorf("limit 3: %d nodes, truncated %v", len(out["nodes"].([]any)), out["truncated"])
	}
	for _, bad := range []string{"&depth=5", "&limit=0", "&kinds=tool", "&kinds=GADGET", "&focus=nope"} {
		if status, _ := h.as(engineer(h.org), "GET", "/api/v1/graph?project_id="+h.project+bad, nil); status != 400 {
			t.Errorf("%s: %d, want 400", bad, status)
		}
	}
	if status, _ := h.as(engineer(h.org), "GET", "/api/v1/graph?project_id="+h.project+"&focus="+ids.New(), nil); status != 404 {
		t.Errorf("unknown focus: %d", status)
	}
	if status, _ := h.as(engineer(h.org), "GET", "/api/v1/graph", nil); status != 400 {
		t.Errorf("no project: %d", status)
	}
}

func TestListingComponents(t *testing.T) {
	h := newHarness(t)
	h.demo()
	var seen []string
	cursor := ""
	for page := 0; ; page++ {
		path := "/api/v1/graph/components?project_id=" + h.project + "&kind=TOOL&limit=2"
		if cursor != "" {
			path += "&cursor=" + cursor
		}
		out := h.get(path)
		for _, it := range out["items"].([]any) {
			seen = append(seen, it.(map[string]any)["key"].(string))
		}
		if out["next_cursor"] == nil {
			break
		}
		cursor = out["next_cursor"].(string)
		if page > 10 {
			t.Fatal("pagination does not end")
		}
	}
	want := []string{"export_customer_data", "get_refund", "get_refund_policy", "lookup_order", "refund_payment", "send_email"}
	if !slices.Equal(seen, want) {
		t.Errorf("paged tools = %v, want %v", seen, want)
	}
	// Case-insensitive, and "_" is a character, not a LIKE wildcard: the
	// hyphenated refund-limit and refund-happy-path do not match.
	out := h.get("/api/v1/graph/components?project_id=" + h.project + "&q=REFUND_")
	if got := names2(out["items"]); !slices.Equal(got, []string{"get_refund_policy", "refund_payment"}) {
		t.Errorf("q=REFUND_ matched %v", got)
	}
	for _, bad := range []string{"&kind=GADGET", "&cursor=not~base64", "&cursor=e30", "&limit=201", "&q=" + strings.Repeat("x", 201)} {
		if status, _ := h.as(engineer(h.org), "GET", "/api/v1/graph/components?project_id="+h.project+bad, nil); status != 400 {
			t.Errorf("%s: %d, want 400", bad, status)
		}
	}
}

func names2(list any) []string {
	var out []string
	for _, it := range list.([]any) {
		out = append(out, it.(map[string]any)["key"].(string))
	}
	return out
}

func TestManualMappings(t *testing.T) {
	h := newHarness(t)
	h.demo()
	path := "/api/v1/graph/mappings/refund_payment"
	body := map[string]any{"project_id": h.project, "depends_on": []map[string]any{
		{"kind": "QUEUE", "name": "refund-events", "relation": "PUBLISHES", "criticality": "HIGH"},
		{"kind": "SERVICE", "name": "payments-api", "relation": "CALLS"},
	}}
	if status, out := h.as(viewer(h.org), "PUT", path, body); status != 403 {
		t.Fatalf("a viewer mapped a tool: %d %v", status, out)
	}
	status, out := h.as(engineer(h.org), "PUT", path, body)
	if status != 200 || len(out["mapped"].([]any)) != 2 {
		t.Fatalf("mapping: %d %v", status, out)
	}
	refund := h.detail("TOOL", "refund_payment")
	if got := evidenceRefs(relation(t, refund, "CALLS", "out", "SERVICE", "payments-api")); !slices.Contains(got, "MANUAL mapping:refund_payment") {
		t.Errorf("mapping evidence = %v", got)
	}
	if n := h.count(`SELECT count(*) FROM graph.outbox WHERE event_type = 'audit.recorded.v1'`); n != 1 {
		t.Errorf("%d audit events", n)
	}
	var env events.Envelope
	var raw []byte
	_ = h.pool.QueryRow(context.Background(), `SELECT envelope FROM graph.outbox`).Scan(&raw)
	_ = json.Unmarshal(raw, &env)
	if err := validator.Validate(env); err != nil {
		t.Errorf("the audit event breaks its contract: %v", err)
	}

	// Removing the mapping keeps what the manifests declare.
	status, out = h.as(engineer(h.org), "PUT", path, map[string]any{"project_id": h.project, "depends_on": []any{}})
	if status != 200 || len(out["mapped"].([]any)) != 0 {
		t.Fatalf("removal: %d %v", status, out)
	}
	refund = h.detail("TOOL", "refund_payment")
	if findRelation(refund, "PUBLISHES", "out", "QUEUE", "refund-events") != nil {
		t.Error("the mapped queue is still an edge")
	}
	if got := evidenceRefs(relation(t, refund, "CALLS", "out", "SERVICE", "payments-api")); slices.Contains(got, "MANUAL mapping:refund_payment") || len(got) != 2 {
		t.Errorf("after removal = %v", got)
	}

	for name, b := range map[string]any{
		"no depends_on":  map[string]any{"project_id": h.project},
		"unknown kind":   map[string]any{"project_id": h.project, "depends_on": []map[string]any{{"kind": "SCENARIO", "name": "x"}}},
		"unknown field":  map[string]any{"project_id": h.project, "depends_on": []any{}, "extra": 1},
		"empty name":     map[string]any{"project_id": h.project, "depends_on": []map[string]any{{"kind": "SERVICE", "name": ""}}},
		"bad project id": map[string]any{"project_id": "p", "depends_on": []any{}},
	} {
		if status, out := h.as(engineer(h.org), "PUT", path, b); status != 400 {
			t.Errorf("%s: %d %v", name, status, out)
		}
	}
	if status, _ := h.as(engineer(h.org), "PUT", "/api/v1/graph/mappings/Bad%20Tool", body); status != 400 {
		t.Errorf("bad tool name: %d", status)
	}
}

func TestBlastRadiusOfAPromptChange(t *testing.T) {
	h := newHarness(t)
	h.demo()
	status, out := h.blast(viewer(h.org), map[string]any{
		"changes": []map[string]any{{
			"component": map[string]any{"kind": "PROMPT", "key": "sha256:bbbb"}, "change": "modified",
			"summary": "refund instructions changed", "mentions": []string{"refund_payment"},
		}},
	})
	if status != 200 {
		t.Fatalf("%d %v", status, out)
	}
	br := out["blast_radius"].(map[string]any)
	affected := names(br["affected"])
	for _, want := range []string{"sha256:bbbb", "support-refund-agent@1.3.0", "refund_payment", "payments-api", "payments-db"} {
		if !slices.Contains(affected, want) {
			t.Errorf("affected misses %s: %v", want, affected)
		}
	}
	if slices.Contains(affected, "support-refund-agent@1.2.4") {
		t.Error("an older version was entered")
	}
	if got := names(br["irreversible_actions"]); !slices.Equal(got, []string{"refund_payment", "export_customer_data"}) {
		t.Errorf("irreversible actions = %v", got)
	}
	scenarios := names(br["scenarios"])
	if len(scenarios) < 3 || scenarios[0] != "refund-timeout-after-mutation" {
		t.Errorf("scenarios = %v; the one testing the mentioned irreversible tool comes first", scenarios)
	}
	if got := names(br["policies"]); !slices.Equal(got, []string{"refund-limit"}) {
		t.Errorf("policies = %v", got)
	}

	// The same change scoped to the older version enters it instead.
	_, out = h.blast(viewer(h.org), map[string]any{
		"changes": []map[string]any{{"component": map[string]any{"kind": "TOOL", "key": "refund_payment"}, "change": "modified"}},
		"scope":   map[string]any{"agent": "support-refund-agent", "version": "1.2.4"}, "max_depth": 2, "max_nodes": 50,
	})
	affected = names(out["blast_radius"].(map[string]any)["affected"])
	if !slices.Contains(affected, "support-refund-agent@1.2.4") || slices.Contains(affected, "support-refund-agent@1.3.0") {
		t.Errorf("scoped to 1.2.4: %v", affected)
	}

	// Unknown components are reported, never guessed.
	_, out = h.blast(viewer(h.org), map[string]any{
		"changes": []map[string]any{{"component": map[string]any{"kind": "TOOL", "key": "issue_voucher"}, "change": "added"}},
		"scope":   nil,
	})
	br = out["blast_radius"].(map[string]any)
	if len(br["unresolved"].([]any)) != 1 || len(br["affected"].([]any)) != 0 {
		t.Errorf("unknown tool: %v", br)
	}
}

func TestBlastRadiusRejectsWhatItCannotCompute(t *testing.T) {
	h := newHarness(t)
	tool := map[string]any{"kind": "TOOL", "key": "refund_payment"}
	many := make([]map[string]any, 101)
	for i := range many {
		many[i] = map[string]any{"component": tool, "change": "modified"}
	}
	for name, body := range map[string]map[string]any{
		"no changes":     {"changes": []any{}},
		"too many":       {"changes": many},
		"unknown kind":   {"changes": []map[string]any{{"component": map[string]any{"kind": "WIDGET", "key": "x"}, "change": "modified"}}},
		"renamed":        {"changes": []map[string]any{{"component": tool, "change": "renamed"}}},
		"bad mention":    {"changes": []map[string]any{{"component": tool, "change": "modified", "mentions": []string{"Bad Name"}}}},
		"half a scope":   {"changes": []map[string]any{{"component": tool, "change": "modified"}}, "scope": map[string]any{"agent": "a", "version": ""}},
		"depth 0":        {"changes": []map[string]any{{"component": tool, "change": "modified"}}, "max_depth": 0},
		"depth 9":        {"changes": []map[string]any{{"component": tool, "change": "modified"}}, "max_depth": 9},
		"nodes 2001":     {"changes": []map[string]any{{"component": tool, "change": "modified"}}, "max_nodes": 2001},
		"unknown field":  {"changes": []map[string]any{{"component": tool, "change": "modified", "why": "x"}}},
		"control in key": {"changes": []map[string]any{{"component": map[string]any{"kind": "TOOL", "key": "a\nb"}, "change": "modified"}}},
	} {
		if status, out := h.blast(viewer(h.org), body); status != 400 {
			t.Errorf("%s: %d %v", name, status, out)
		}
	}
}

func TestProjectsAndComponentsOfOthersAreInvisible(t *testing.T) {
	h := newHarness(t)
	h.demo()
	id := h.componentID("TOOL", "refund_payment")
	other := ids.New()
	// The edge names a person's own projects in the internal token: another
	// organization's, or another project of the same one.
	for _, p := range []authn.Principal{
		{OrgID: other, Actor: "user:other-org", Role: authn.RoleEngineer, ProjectIDs: []string{ids.New()}},
		{OrgID: h.org, Actor: "user:other-project", Role: authn.RoleEngineer, ProjectIDs: []string{ids.New()}},
	} {
		for _, path := range []string{
			"/api/v1/graph?project_id=" + h.project,
			"/api/v1/graph/components?project_id=" + h.project,
			"/api/v1/graph/components/" + id,
		} {
			if status, _ := h.as(p, "GET", path, nil); status != 404 {
				t.Errorf("%s as %s: %d, want 404", path, p.Actor, status)
			}
		}
		if status, _ := h.blast(p, map[string]any{"changes": []map[string]any{{"component": map[string]any{"kind": "TOOL", "key": "refund_payment"}, "change": "modified"}}}); status != 404 {
			t.Errorf("blast radius as %s: %d", p.Actor, status)
		}
		if status, _ := h.as(p, "PUT", "/api/v1/graph/mappings/refund_payment", map[string]any{"project_id": h.project, "depends_on": []any{}}); status != 404 {
			t.Errorf("mapping as %s: %d", p.Actor, status)
		}
	}

	// A component of this project cannot be pulled into another project's
	// view by its id.
	own := ids.New()
	outsider := authn.Principal{OrgID: other, Actor: "user:outsider", Role: authn.RoleEngineer, ProjectIDs: []string{own}}
	if status, out := h.as(outsider, "GET", "/api/v1/graph?project_id="+own+"&focus="+id, nil); status != 404 {
		t.Errorf("focus on another organization's component: %d %v", status, out)
	}

	// Even a token claiming every project of another organization sees
	// nothing of this one and cannot disturb it: every row is keyed by
	// organization too.
	intruder := engineer(other)
	if _, out := h.as(intruder, "GET", "/api/v1/graph?project_id="+h.project, nil); len(out["nodes"].([]any)) != 0 ||
		out["totals"].(map[string]any)["edges"] != 0.0 {
		t.Errorf("another organization sees %v", out)
	}
	if status, _ := h.as(intruder, "GET", "/api/v1/graph/components/"+id, nil); status != 404 {
		t.Errorf("another organization reads a component: %d", status)
	}
	if status, out := h.as(intruder, "PUT", "/api/v1/graph/mappings/refund_payment", map[string]any{"project_id": h.project,
		"depends_on": []map[string]any{{"kind": "SERVICE", "name": "attacker-api", "relation": "CALLS"}}}); status != 200 {
		t.Fatalf("intruder mapping: %d %v", status, out)
	}
	h.apply(h.event("policy.activated.v1", t0.Add(time.Hour), map[string]any{
		"policy_id": ids.New(), "policy_version_id": ids.New(), "name": "email-limit", "version": 1,
		"target_tool": "refund_payment", "spec_hash": strings.Repeat("e", 64),
	}))
	refund := h.detail("TOOL", "refund_payment")
	if findRelation(refund, "CALLS", "out", "SERVICE", "attacker-api") != nil {
		t.Error("another organization's mapping reached this graph")
	}
	relation(t, refund, "GUARDED_BY", "out", "POLICY", "email-limit")

	// Without the internal token the API does not answer at all.
	res, err := http.Get(h.srv.URL + "/api/v1/graph?project_id=" + h.project)
	if err != nil {
		t.Fatal(err)
	}
	_ = res.Body.Close()
	if res.StatusCode != 401 {
		t.Errorf("no token: %d", res.StatusCode)
	}
	if status, _ := h.as(engineer(h.org), "GET", "/api/v1/graph/components/not-a-uuid", nil); status != 400 {
		t.Errorf("malformed id: %d", status)
	}
	if status, _ := h.as(engineer(h.org), "GET", "/api/v1/graph/components/"+ids.New(), nil); status != 404 {
		t.Errorf("unknown id: %d", status)
	}
}

// A database failure is the service's, answered 5xx and retried, never
// blamed on the caller or parked as a malformed event.
func TestStoreFailuresAreNotClientErrors(t *testing.T) {
	h := newHarness(t)
	h.demo()
	h.pool.Close()
	status, out := h.blast(viewer(h.org), map[string]any{
		"changes": []map[string]any{{"component": map[string]any{"kind": "TOOL", "key": "refund_payment"}, "change": "modified"}},
	})
	if status < 500 {
		t.Errorf("a closed database answered %d %v", status, out)
	}
	if err := h.handle(h.event("trace.ingested.v1", t0, tracePayload("production", 1))); err == nil || events.IsPermanent(err) {
		t.Errorf("a store failure must be retried, got %v", err)
	}
}
