package server_test

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

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/Ozgurisikdamar/AgentTwin/packages/contracts"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/authn"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/config"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/db"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/events"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/httpx"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/ids"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/openapicheck"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/testutil"
	"github.com/Ozgurisikdamar/AgentTwin/services/control-plane/internal/auth"
	"github.com/Ozgurisikdamar/AgentTwin/services/control-plane/internal/server"
	"github.com/Ozgurisikdamar/AgentTwin/services/control-plane/migrations"
)

const (
	internalSecret = "test-internal-secret-0123456789abcdef-xyz"
	sessionSecret  = "test-session-secret-0123456789abcdef-xyz"
	pepper         = "test-pepper-0123456789abcdef-0123456789"
	demoKey        = "atk_demo0000_test-demo-secret-0123456789abcdef"
)

// contract is the control plane's API contract (ADR-0021). Every exchange of
// a documented operation in these tests is held to it, and the suite fails
// when a documented operation had no checked successful exchange (TestMain).
var contract = func() *openapicheck.Contract {
	c, err := openapicheck.Load(contracts.OpenAPI, "openapi/control-plane.openapi.yaml")
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
			fmt.Fprintf(os.Stderr, "control-plane contract: no checked successful exchange for %s\n", strings.Join(missing, ", "))
			code = 1
		}
	}
	os.Exit(code)
}

type harness struct {
	t      *testing.T
	srv    *httptest.Server
	s      *server.Server
	pool   *pgxpool.Pool
	tokens *authn.TokenService
	mu     sync.Mutex
	logins map[string]string
}

func newHarness(t *testing.T, targets map[string]string) *harness {
	t.Helper()
	dbURL := testutil.NewDatabase(t)
	ctx := context.Background()
	pool, err := db.Connect(ctx, nil, db.PoolConfig{URL: dbURL, Schema: migrations.Schema, MaxConns: 10})
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	t.Cleanup(pool.Close)
	migs, err := db.LoadMigrations(migrations.FS, ".")
	if err != nil {
		t.Fatalf("load migrations: %v", err)
	}
	if _, err := (&db.Migrator{Pool: pool, Schema: migrations.Schema, Migrations: migs}).Up(ctx); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	tokens, err := authn.NewTokenService(internalSecret)
	if err != nil {
		t.Fatal(err)
	}
	log := slog.New(slog.NewJSONHandler(io.Discard, nil))
	s, err := server.New(ctx, pool, tokens, log, server.Config{
		AuthMode: auth.ModeDev, SessionSecret: sessionSecret, Pepper: pepper,
		DemoAPIKey: demoKey, DemoBootstrap: true, Targets: targets,
		RateRPS: 10000, RateBurst: 10000, IPRateRPS: 10000, IPRateBurst: 10000,
	})
	if err != nil {
		t.Fatalf("server: %v", err)
	}
	srv := httptest.NewServer(httpx.Chain(s.Handler, httpx.RequestID()))
	t.Cleanup(srv.Close)
	return &harness{t: t, srv: srv, s: s, pool: pool, tokens: tokens, logins: map[string]string{}}
}

type resp struct {
	Status int
	Header http.Header
	Body   map[string]any
	Raw    []byte
}

func (h *harness) request(method, path string, body any, headers map[string]string) resp {
	h.t.Helper()
	var payload []byte
	switch b := body.(type) {
	case nil:
	case []byte:
		payload = b
	case string:
		payload = []byte(b)
	default:
		payload, _ = json.Marshal(b)
	}
	var rdr io.Reader
	if body != nil {
		rdr = bytes.NewReader(payload)
	}
	req, err := http.NewRequest(method, h.srv.URL+path, rdr)
	if err != nil {
		h.t.Fatal(err)
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	res, err := h.srv.Client().Do(req)
	if err != nil {
		h.t.Fatalf("%s %s: %v", method, path, err)
	}
	defer func() { _ = res.Body.Close() }()
	raw, _ := io.ReadAll(res.Body)
	if _, _, documented := contract.Find(method, req.URL.Path); documented {
		if err := contract.CheckExchange(req, payload, res.StatusCode, res.Header, raw); err != nil {
			h.t.Errorf("%s %s: %v", method, path, err)
		}
	}
	out := resp{Status: res.StatusCode, Header: res.Header, Raw: raw}
	_ = json.Unmarshal(raw, &out.Body)
	return out
}

func bearer(tok string) map[string]string { return map[string]string{"Authorization": "Bearer " + tok} }

func (h *harness) login(email string) string {
	h.t.Helper()
	h.mu.Lock()
	defer h.mu.Unlock()
	if tok, ok := h.logins[email]; ok {
		return tok
	}
	r := h.request("POST", "/api/v1/auth/dev/login", map[string]string{"email": email}, nil)
	if r.Status != 200 {
		h.t.Fatalf("login %s: %d %s", email, r.Status, r.Raw)
	}
	tok := r.Body["token"].(string)
	h.logins[email] = tok
	return tok
}

func errCode(r resp) string {
	if e, ok := r.Body["error"].(map[string]any); ok {
		s, _ := e["code"].(string)
		return s
	}
	return ""
}

func readManifest(t *testing.T, version string) []byte {
	t.Helper()
	b, err := os.ReadFile("../../../../demo/support-refund-agent/manifests/" + version + ".yaml")
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func (h *harness) registerManifest(tok, projectID string, manifest []byte) resp {
	h.t.Helper()
	return h.request("POST", "/api/v1/projects/"+projectID+"/agent-manifests?commit_sha=0a1b2c3d", manifest,
		map[string]string{"Authorization": "Bearer " + tok, "Content-Type": "application/yaml"})
}

func (h *harness) count(sql string, args ...any) int {
	h.t.Helper()
	var n int
	if err := h.pool.QueryRow(context.Background(), sql, args...).Scan(&n); err != nil {
		h.t.Fatalf("count %q: %v", sql, err)
	}
	return n
}

func TestManifestRegistrationIsImmutableAndIdempotent(t *testing.T) {
	h := newHarness(t, nil)
	eng := h.login("engineer@demo.agenttwin.dev")
	pid := h.s.Demo.ProjectID

	first := h.registerManifest(eng, pid, readManifest(t, "1.2.4"))
	if first.Status != http.StatusCreated || first.Body["created"] != true {
		t.Fatalf("first registration: %d %s", first.Status, first.Raw)
	}
	version := first.Body["version"].(map[string]any)
	if version["commit_sha"] != "0a1b2c3d" || version["model_name"] != "scripted-planner-v1" {
		t.Fatalf("unexpected version metadata: %v", version)
	}

	again := h.registerManifest(eng, pid, readManifest(t, "1.2.4"))
	if again.Status != http.StatusOK || again.Body["created"] != false {
		t.Fatalf("idempotent re-registration: %d %s", again.Status, again.Raw)
	}
	if again.Body["version"].(map[string]any)["id"] != version["id"] {
		t.Fatal("re-registration returned a different version id")
	}

	changed := bytes.Replace(readManifest(t, "1.2.4"), []byte("maxSteps: 30"), []byte("maxSteps: 31"), 1)
	conflict := h.registerManifest(eng, pid, changed)
	if conflict.Status != http.StatusConflict || errCode(conflict) != "VERSION_EXISTS" {
		t.Fatalf("different content under same version: %d %s", conflict.Status, conflict.Raw)
	}

	bad := h.registerManifest(eng, pid, []byte("apiVersion: agenttwin.dev/v1\nkind: Agent\nmetadata: {name: Bad_Name, version: one}\n"))
	if bad.Status != http.StatusBadRequest || errCode(bad) != "INVALID_MANIFEST" {
		t.Fatalf("invalid manifest: %d %s", bad.Status, bad.Raw)
	}
	if problems, _ := bad.Body["error"].(map[string]any)["details"].(map[string]any)["problems"].([]any); len(problems) == 0 {
		t.Fatalf("invalid manifest must list problems: %s", bad.Raw)
	}

	// Exactly one version event reached the outbox (the idempotent replay and
	// the conflict must not emit), and exactly one audit entry was written.
	if n := h.count(`SELECT count(*) FROM control.outbox WHERE event_type = 'agent.version_registered.v1'`); n != 1 {
		t.Fatalf("outbox events = %d, want 1", n)
	}
	if n := h.count(`SELECT count(*) FROM control.audit_event WHERE action = 'agent_version.created'`); n != 1 {
		t.Fatalf("audit entries = %d, want 1", n)
	}
	// All seven tools are registered with their declared risk.
	tools := h.request("GET", "/api/v1/projects/"+pid+"/tools", nil, bearer(eng))
	items := tools.Body["items"].([]any)
	if len(items) != 7 {
		t.Fatalf("tools = %d, want 7: %s", len(items), tools.Raw)
	}
	risks := map[string]string{}
	for _, it := range items {
		m := it.(map[string]any)
		risks[m["name"].(string)] = m["risk"].(string)
	}
	if risks["refund_payment"] != "WRITE_IRREVERSIBLE" || risks["lookup_order"] != "READ" ||
		risks["export_customer_data"] != "ADMIN" {
		t.Fatalf("unexpected tool risks: %v", risks)
	}
}

func TestConcurrentRegistrationOfSameVersion(t *testing.T) {
	h := newHarness(t, nil)
	eng := h.login("engineer@demo.agenttwin.dev")
	pid := h.s.Demo.ProjectID
	manifest := readManifest(t, "1.3.0")
	var wg sync.WaitGroup
	statuses := make([]int, 8)
	for i := range statuses {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			statuses[i] = h.registerManifest(eng, pid, manifest).Status
		}(i)
	}
	wg.Wait()
	created := 0
	for _, s := range statuses {
		switch s {
		case http.StatusCreated:
			created++
		case http.StatusOK:
		default:
			t.Fatalf("unexpected status in concurrent registration: %v", statuses)
		}
	}
	if created != 1 {
		t.Fatalf("exactly one request must create the version, got %d (%v)", created, statuses)
	}
	if n := h.count(`SELECT count(*) FROM control.agent_version WHERE version = '1.3.0'`); n != 1 {
		t.Fatalf("versions stored = %d", n)
	}
}

func TestRBACAndTenantIsolation(t *testing.T) {
	h := newHarness(t, nil)
	pid := h.s.Demo.ProjectID
	viewer := h.login("viewer@demo.agenttwin.dev")
	eng := h.login("engineer@demo.agenttwin.dev")
	reviewer := h.login("reviewer@demo.agenttwin.dev")
	other := h.login("owner@other.agenttwin.dev")

	cases := []struct {
		name, method, path, token string
		body                      any
		want                      int
	}{
		{"viewer reads project", "GET", "/api/v1/projects/" + pid, viewer, nil, 200},
		{"viewer cannot create api key", "POST", "/api/v1/projects/" + pid + "/api-keys", viewer, map[string]any{"name": "x", "scopes": []string{"read"}}, 403},
		{"engineer cannot create api key", "POST", "/api/v1/projects/" + pid + "/api-keys", eng, map[string]any{"name": "x", "scopes": []string{"read"}}, 403},
		{"engineer cannot change settings", "PATCH", "/api/v1/projects/" + pid, eng, map[string]any{"content_mode": "full"}, 403},
		{"reviewer cannot register agents", "POST", "/api/v1/projects/" + pid + "/agents", reviewer, map[string]any{"name": "x-agent"}, 403},
		{"viewer cannot read audit", "GET", "/api/v1/audit", viewer, nil, 403},
		{"other org cannot see project", "GET", "/api/v1/projects/" + pid, other, nil, 404},
		{"other org cannot list its tools", "GET", "/api/v1/projects/" + pid + "/tools", other, nil, 404},
		{"other org cannot register into it", "POST", "/api/v1/projects/" + pid + "/agents", other, map[string]any{"name": "evil-agent"}, 404},
		{"unauthenticated", "GET", "/api/v1/projects", "", nil, 401},
		{"garbage token", "GET", "/api/v1/projects", "not-a-token", nil, 401},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			hdr := map[string]string{}
			if c.token != "" {
				hdr["Authorization"] = "Bearer " + c.token
			}
			r := h.request(c.method, c.path, c.body, hdr)
			if r.Status != c.want {
				t.Fatalf("%s %s = %d, want %d: %s", c.method, c.path, r.Status, c.want, r.Raw)
			}
		})
	}

	// Listing is filtered, not failing: the other org sees only its own projects.
	r := h.request("GET", "/api/v1/projects", nil, bearer(other))
	if items := r.Body["items"].([]any); len(items) != 0 {
		t.Fatalf("other org sees projects of demo org: %s", r.Raw)
	}
	// An org header naming an organization the user is not a member of is refused.
	r = h.request("GET", "/api/v1/projects", nil, map[string]string{"Authorization": "Bearer " + other, "X-AgentTwin-Org": h.s.Demo.OrganizationID})
	if r.Status != http.StatusForbidden {
		t.Fatalf("foreign org header = %d", r.Status)
	}
}

func TestAPIKeyLifecycleAndScopes(t *testing.T) {
	h := newHarness(t, nil)
	pid := h.s.Demo.ProjectID
	admin := h.login("admin@demo.agenttwin.dev")

	created := h.request("POST", "/api/v1/projects/"+pid+"/api-keys", map[string]any{"name": "ingest", "scopes": []string{"traces:write"}}, bearer(admin))
	if created.Status != http.StatusCreated {
		t.Fatalf("create key: %d %s", created.Status, created.Raw)
	}
	key := created.Body["key"].(string)
	keyID := created.Body["id"].(string)
	if !strings.HasPrefix(key, "atk_") || created.Body["secret_hash"] != nil {
		t.Fatalf("plaintext must be returned once and the hash never: %s", created.Raw)
	}
	listed := h.request("GET", "/api/v1/projects/"+pid+"/api-keys", nil, bearer(admin))
	if bytes.Contains(listed.Raw, []byte(key[13:])) {
		t.Fatal("list response leaks the key secret")
	}

	// A traces:write key cannot read the registry.
	if r := h.request("GET", "/api/v1/projects", nil, map[string]string{"X-AgentTwin-Api-Key": key}); r.Status != http.StatusForbidden {
		t.Fatalf("ingest key reading projects = %d, want 403", r.Status)
	}
	// Tampered secret is rejected.
	if r := h.request("GET", "/api/v1/me", nil, map[string]string{"X-AgentTwin-Api-Key": key[:len(key)-1] + "x"}); r.Status != http.StatusUnauthorized {
		t.Fatalf("tampered key = %d, want 401", r.Status)
	}

	rotated := h.request("POST", "/api/v1/api-keys/"+keyID+"/rotate", nil, bearer(admin))
	if rotated.Status != http.StatusCreated {
		t.Fatalf("rotate: %d %s", rotated.Status, rotated.Raw)
	}
	newKey := rotated.Body["key"].(string)
	if r := h.request("GET", "/api/v1/me", nil, map[string]string{"X-AgentTwin-Api-Key": key}); r.Status != http.StatusUnauthorized {
		t.Fatalf("old key after rotation = %d, want 401", r.Status)
	}
	if r := h.request("GET", "/api/v1/me", nil, map[string]string{"X-AgentTwin-Api-Key": newKey}); r.Status != http.StatusOK {
		t.Fatalf("new key after rotation = %d, want 200: %s", r.Status, r.Raw)
	}
	newID := rotated.Body["id"].(string)
	if r := h.request("POST", "/api/v1/api-keys/"+newID+"/revoke", map[string]string{"reason": "test"}, bearer(admin)); r.Status != http.StatusOK {
		t.Fatalf("revoke: %d %s", r.Status, r.Raw)
	}
	if r := h.request("GET", "/api/v1/me", nil, map[string]string{"X-AgentTwin-Api-Key": newKey}); r.Status != http.StatusUnauthorized {
		t.Fatalf("revoked key = %d, want 401", r.Status)
	}

	// The audit log records every step but never the secret or its hash.
	var auditJSON []byte
	if err := h.pool.QueryRow(context.Background(), `SELECT coalesce(json_agg(a)::text, '')::bytea FROM control.audit_event a`).Scan(&auditJSON); err != nil {
		t.Fatal(err)
	}
	for _, secret := range []string{key[13:], newKey[13:]} {
		if bytes.Contains(auditJSON, []byte(secret)) {
			t.Fatal("audit log contains an API key secret")
		}
	}
	var hashes []string
	rows, _ := h.pool.Query(context.Background(), `SELECT secret_hash FROM control.api_key`)
	for rows.Next() {
		var s string
		_ = rows.Scan(&s)
		hashes = append(hashes, s)
	}
	rows.Close()
	for _, hs := range hashes {
		if bytes.Contains(auditJSON, []byte(hs)) {
			t.Fatal("audit log contains an API key hash")
		}
	}
	for _, action := range []string{"api_key.created", "api_key.rotated", "api_key.revoked"} {
		if n := h.count(`SELECT count(*) FROM control.audit_event WHERE action = $1`, action); n == 0 {
			t.Fatalf("missing audit action %s", action)
		}
	}
}

func TestAuditChainDetectsTampering(t *testing.T) {
	h := newHarness(t, nil)
	pid := h.s.Demo.ProjectID
	admin := h.login("admin@demo.agenttwin.dev")
	for i := 0; i < 3; i++ {
		r := h.request("POST", "/api/v1/projects/"+pid+"/api-keys", map[string]any{"name": "k", "scopes": []string{"read"}}, bearer(admin))
		if r.Status != http.StatusCreated {
			t.Fatalf("create key: %d", r.Status)
		}
	}
	v := h.request("GET", "/api/v1/audit/verify", nil, bearer(admin))
	if v.Body["valid"] != true || v.Body["entries"].(float64) != 3 {
		t.Fatalf("fresh chain must verify: %s", v.Raw)
	}
	ctx := context.Background()
	// The table is append-only: UPDATE and DELETE are rejected by a trigger.
	if _, err := h.pool.Exec(ctx, `UPDATE control.audit_event SET actor = 'user:mallory' WHERE seq = (SELECT min(seq) FROM control.audit_event)`); err == nil {
		t.Fatal("UPDATE on audit_event must be rejected")
	}
	if _, err := h.pool.Exec(ctx, `DELETE FROM control.audit_event`); err == nil {
		t.Fatal("DELETE on audit_event must be rejected")
	}
	// A privileged attacker who bypasses the trigger is still detected.
	if _, err := h.pool.Exec(ctx, `ALTER TABLE control.audit_event DISABLE TRIGGER USER`); err != nil {
		t.Fatal(err)
	}
	if _, err := h.pool.Exec(ctx, `UPDATE control.audit_event SET actor = 'user:mallory' WHERE seq = (SELECT min(seq) + 1 FROM control.audit_event)`); err != nil {
		t.Fatal(err)
	}
	v = h.request("GET", "/api/v1/audit/verify", nil, bearer(admin))
	if v.Body["valid"] != false || v.Body["broken_at_seq"] == nil {
		t.Fatalf("tampered chain must fail verification: %s", v.Raw)
	}
}

// Audit events from other services arrive at least once and in any order
// (spec §64): each is recorded once, keeps the time it happened, and the chain
// stays whole, also when deliveries of one event race.
func TestAuditEventsFromOtherServicesAreRecordedOnceWhateverTheDelivery(t *testing.T) {
	h := newHarness(t, nil)
	org, pid := h.s.Demo.OrganizationID, h.s.Demo.ProjectID
	admin := h.login("admin@demo.agenttwin.dev")
	at := time.Date(2026, 9, 26, 10, 0, 0, 0, time.UTC)
	event := func(action string, when time.Time) events.Envelope {
		env, err := events.New("audit.recorded.v1", "runtime-gateway", org, pid, "", "", map[string]any{
			"actor": "user:approver", "action": action, "resource_type": "approval", "resource_id": ids.New(),
			"timestamp": when.Format(time.RFC3339Nano), "metadata": map[string]any{"decision": "approved"},
		})
		if err != nil {
			t.Fatal(err)
		}
		return env
	}
	handle := func(env events.Envelope) {
		t.Helper()
		if err := h.s.App.HandleAuditEvent(context.Background(), env); err != nil {
			t.Fatalf("handle %s: %v", env.ID, err)
		}
	}
	later, earlier := event("approval.approved", at.Add(time.Minute)), event("approval.requested", at)
	handle(later)
	handle(later)   // redelivered
	handle(earlier) // delayed: it happened first but arrives last
	handle(later)
	// Racing deliveries of one event (several consumers, a retry overtaking
	// the original), next to other events handled at the same moment.
	racing := event("approval.denied", at.Add(2*time.Minute))
	concurrent := make([]events.Envelope, 8)
	for i := range concurrent {
		concurrent[i] = event("policy.tested", at.Add(3*time.Minute))
	}
	var wg sync.WaitGroup
	errs := make(chan error, 16)
	for i := range 8 {
		wg.Add(2)
		go func() {
			defer wg.Done()
			errs <- h.s.App.HandleAuditEvent(context.Background(), racing)
		}()
		go func() {
			defer wg.Done()
			errs <- h.s.App.HandleAuditEvent(context.Background(), concurrent[i])
		}()
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		if err != nil {
			t.Errorf("a racing delivery failed: %v", err)
		}
	}

	rows, err := h.pool.Query(context.Background(), `SELECT action, occurred_at FROM control.audit_event
		WHERE source_service = 'runtime-gateway' AND action <> 'policy.tested' ORDER BY seq`)
	if err != nil {
		t.Fatal(err)
	}
	type row struct {
		action string
		at     time.Time
	}
	got, err := pgx.CollectRows(rows, func(r pgx.CollectableRow) (row, error) {
		var x row
		return x, r.Scan(&x.action, &x.at)
	})
	if err != nil {
		t.Fatal(err)
	}
	want := []row{{"approval.approved", at.Add(time.Minute)}, {"approval.requested", at}, {"approval.denied", at.Add(2 * time.Minute)}}
	if len(got) != len(want) {
		t.Fatalf("recorded %v, want each event once: %v", got, want)
	}
	for i := range want {
		if got[i].action != want[i].action || !got[i].at.Equal(want[i].at) {
			t.Errorf("entry %d = %v, want %v (arrival order, with the time each happened)", i, got[i], want[i])
		}
	}
	var tested int
	if err := h.pool.QueryRow(context.Background(), `SELECT count(*) FROM control.audit_event WHERE action = 'policy.tested'`).Scan(&tested); err != nil || tested != 8 {
		t.Errorf("%d concurrent events recorded (%v), want 8", tested, err)
	}
	// One chain, no fork: every entry links to the one before it.
	v := h.request("GET", "/api/v1/audit/verify", nil, bearer(admin))
	if v.Body["valid"] != true {
		t.Fatalf("the chain must verify after duplicate and racing deliveries: %s", v.Raw)
	}
}

func TestIdempotencyKeyReplaysAndRejectsReuse(t *testing.T) {
	h := newHarness(t, nil)
	owner := h.login("owner@demo.agenttwin.dev")
	hdr := map[string]string{"Authorization": "Bearer " + owner, "Idempotency-Key": "create-project-0001"}
	body := map[string]any{"slug": "payments", "name": "Payments"}
	first := h.request("POST", "/api/v1/projects", body, hdr)
	if first.Status != http.StatusCreated {
		t.Fatalf("create: %d %s", first.Status, first.Raw)
	}
	second := h.request("POST", "/api/v1/projects", body, hdr)
	if second.Status != http.StatusCreated || second.Header.Get("Idempotent-Replayed") != "true" {
		t.Fatalf("replay: %d replayed=%q %s", second.Status, second.Header.Get("Idempotent-Replayed"), second.Raw)
	}
	if first.Body["id"] != second.Body["id"] {
		t.Fatal("replay returned a different resource")
	}
	if n := h.count(`SELECT count(*) FROM control.project WHERE slug = 'payments'`); n != 1 {
		t.Fatalf("projects created = %d", n)
	}
	reuse := h.request("POST", "/api/v1/projects", map[string]any{"slug": "payments-2", "name": "Other"}, hdr)
	if reuse.Status != http.StatusUnprocessableEntity || errCode(reuse) != "IDEMPOTENCY_KEY_REUSED" {
		t.Fatalf("reuse: %d %s", reuse.Status, reuse.Raw)
	}
	// Without a key, the duplicate is a plain conflict.
	dup := h.request("POST", "/api/v1/projects", body, bearer(owner))
	if dup.Status != http.StatusConflict {
		t.Fatalf("duplicate slug without key: %d", dup.Status)
	}
}

func TestInternalAPIRequiresServiceToken(t *testing.T) {
	h := newHarness(t, nil)
	mint := func(p authn.Principal, aud string) string {
		tok, err := h.tokens.Mint(p, aud, "rid-12345678")
		if err != nil {
			t.Fatal(err)
		}
		return tok
	}
	svc := mint(authn.SystemPrincipal("trace-service"), "control-plane")
	r := h.request("POST", "/internal/v1/api-keys/verify", map[string]string{"key": demoKey}, bearer(svc))
	if r.Status != 200 || r.Body["valid"] != true || r.Body["project_id"] != h.s.Demo.ProjectID || r.Body["content_mode"] != "redacted" {
		t.Fatalf("verify demo key: %d %s", r.Status, r.Raw)
	}
	r = h.request("POST", "/internal/v1/api-keys/verify", map[string]string{"key": "atk_demo0000_wrong"}, bearer(svc))
	if r.Status != 200 || r.Body["valid"] != false {
		t.Fatalf("verify wrong key: %d %s", r.Status, r.Raw)
	}
	// Wrong audience, user principals and session tokens are refused.
	wrongAud := mint(authn.SystemPrincipal("trace-service"), "graph-service")
	if r := h.request("POST", "/internal/v1/api-keys/verify", map[string]string{"key": demoKey}, bearer(wrongAud)); r.Status != 401 {
		t.Fatalf("wrong audience = %d", r.Status)
	}
	user := mint(authn.Principal{OrgID: h.s.Demo.OrganizationID, Actor: "user:x", Role: authn.RoleViewer, AllProjects: true}, "control-plane")
	if r := h.request("POST", "/internal/v1/api-keys/verify", map[string]string{"key": demoKey}, bearer(user)); r.Status != 403 {
		t.Fatalf("user principal = %d", r.Status)
	}
	session := h.login("owner@demo.agenttwin.dev")
	if r := h.request("GET", "/internal/v1/projects/"+h.s.Demo.ProjectID, nil, bearer(session)); r.Status != 401 {
		t.Fatalf("session token on internal API = %d", r.Status)
	}
	// System principals may resolve a project before knowing its organization.
	if r := h.request("GET", "/internal/v1/projects/"+h.s.Demo.ProjectID, nil, bearer(svc)); r.Status != 200 {
		t.Fatalf("system project lookup = %d %s", r.Status, r.Raw)
	}
}

func TestInternalAgentVersionLookupByName(t *testing.T) {
	h := newHarness(t, nil)
	owner := h.login("owner@demo.agenttwin.dev")
	if r := h.registerManifest(owner, h.s.Demo.ProjectID, readManifest(t, "1.2.4")); r.Status != 201 && r.Status != 200 {
		t.Fatalf("register: %d %s", r.Status, r.Raw)
	}
	mint := func(p authn.Principal) map[string]string {
		tok, err := h.tokens.Mint(p, "control-plane", "rid-12345678")
		if err != nil {
			t.Fatal(err)
		}
		return bearer(tok)
	}
	svc := mint(authn.Principal{OrgID: h.s.Demo.OrganizationID, Actor: "service:simulation-service", Role: authn.RoleService,
		ProjectIDs: []string{h.s.Demo.ProjectID}})
	path := "/internal/v1/agent-versions?project_id=" + h.s.Demo.ProjectID + "&agent=support-refund-agent&version="
	r := h.request("GET", path+"1.2.4", nil, svc)
	if r.Status != 200 || r.Body["version"] != "1.2.4" || r.Body["agent_name"] != "support-refund-agent" {
		t.Fatalf("lookup: %d %s", r.Status, r.Raw)
	}
	tools, _ := r.Body["tools"].([]any)
	if len(tools) != 7 {
		t.Fatalf("tools = %d, want 7: %s", len(tools), r.Raw)
	}
	if r := h.request("GET", path+"9.9.9", nil, svc); r.Status != 404 {
		t.Fatalf("unknown version = %d", r.Status)
	}
	// A token for another project cannot resolve this project's versions.
	other := mint(authn.Principal{OrgID: h.s.Demo.OrganizationID, Actor: "service:simulation-service", Role: authn.RoleService,
		ProjectIDs: []string{"0190f3b4-0000-7000-8000-00000000abcd"}})
	if r := h.request("GET", path+"1.2.4", nil, other); r.Status != 404 {
		t.Fatalf("other project = %d", r.Status)
	}
	if r := h.request("GET", "/internal/v1/agent-versions?agent=x&version=1", nil, svc); r.Status != 400 {
		t.Fatalf("missing project_id = %d", r.Status)
	}
}

func TestProxyForwardsWithInternalTokenAndStripsClientCredentials(t *testing.T) {
	var got struct {
		sync.Mutex
		auth, apiKey, cookie, rid, path string
		principal                       authn.Principal
	}
	tokens, _ := authn.NewTokenService(internalSecret)
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got.Lock()
		defer got.Unlock()
		got.auth = r.Header.Get("Authorization")
		got.apiKey = r.Header.Get("X-AgentTwin-Api-Key")
		got.cookie = r.Header.Get("Cookie")
		got.rid = r.Header.Get("X-Request-Id")
		got.path = r.URL.RequestURI()
		p, _, err := tokens.Verify(authn.BearerToken(r), "trace-service")
		if err != nil {
			http.Error(w, err.Error(), http.StatusUnauthorized)
			return
		}
		got.principal = p
		httpx.WriteJSON(w, 200, map[string]any{"items": []any{}})
	}))
	defer upstream.Close()

	h := newHarness(t, map[string]string{"trace-service": upstream.URL})
	eng := h.login("engineer@demo.agenttwin.dev")
	r := h.request("GET", "/api/v1/traces?project_id="+h.s.Demo.ProjectID+"&limit=5", nil,
		map[string]string{"Authorization": "Bearer " + eng, "Cookie": "session=abc", "X-Request-Id": "req-proxy-000001"})
	if r.Status != 200 {
		t.Fatalf("proxied request: %d %s", r.Status, r.Raw)
	}
	got.Lock()
	defer got.Unlock()
	if got.cookie != "" || got.apiKey != "" || strings.Contains(got.auth, eng) {
		t.Fatalf("client credentials leaked upstream: auth=%q cookie=%q", got.auth, got.cookie)
	}
	if got.principal.Role != authn.RoleEngineer || got.principal.OrgID != h.s.Demo.OrganizationID {
		t.Fatalf("upstream principal = %+v", got.principal)
	}
	if got.rid != "req-proxy-000001" || got.path != "/api/v1/traces?project_id="+h.s.Demo.ProjectID+"&limit=5" {
		t.Fatalf("request id %q / path %q not propagated", got.rid, got.path)
	}
	// Unconfigured services answer an actionable 503; unknown paths 404.
	if r := h.request("GET", "/api/v1/scenarios", nil, bearer(eng)); r.Status != 503 || errCode(r) != "SERVICE_NOT_CONFIGURED" {
		t.Fatalf("unconfigured service: %d %s", r.Status, r.Raw)
	}
	if r := h.request("GET", "/api/v1/nope", nil, bearer(eng)); r.Status != 404 {
		t.Fatalf("unknown path: %d", r.Status)
	}
	// Unauthenticated requests never reach the upstream.
	if r := h.request("GET", "/api/v1/traces", nil, nil); r.Status != 401 {
		t.Fatalf("unauthenticated proxy request: %d", r.Status)
	}
}

// Agents call their tools through the edge at /gateway/v1 (ADR-0033). The
// key authenticates the call and never travels further; the Idempotency-Key
// and the approval token do, because they belong to the tool call: the edge
// must not answer a retry itself, the gateway decides what a retry means.
func TestAgentsReachTheGatewayThroughTheEdge(t *testing.T) {
	type seen struct{ path, idem, approval, apiKey, auth string }
	var mu sync.Mutex
	var calls []seen
	tokens, _ := authn.NewTokenService(internalSecret)
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if _, _, err := tokens.Verify(authn.BearerToken(r), "runtime-gateway"); err != nil {
			http.Error(w, err.Error(), http.StatusUnauthorized)
			return
		}
		mu.Lock()
		calls = append(calls, seen{r.URL.Path, r.Header.Get("Idempotency-Key"), r.Header.Get("X-AgentTwin-Approval-Token"),
			r.Header.Get("X-AgentTwin-Api-Key"), r.Header.Get("Authorization")})
		n := len(calls)
		mu.Unlock()
		httpx.WriteJSON(w, 201, map[string]any{"call": n})
	}))
	defer upstream.Close()
	h := newHarness(t, map[string]string{"runtime-gateway": upstream.URL})
	agent := map[string]string{"X-AgentTwin-Api-Key": demoKey, "Idempotency-Key": "refund-ORD-1003",
		"X-AgentTwin-Approval-Token": "apt_token-for-the-exact-action"}

	for i := 1; i <= 2; i++ {
		r := h.request("POST", "/gateway/v1/tools/refund_payment", map[string]any{"amount": 250}, agent)
		if r.Status != 201 || r.Body["call"] != float64(i) || r.Header.Get("Idempotent-Replayed") != "" {
			t.Fatalf("call %d: %d replayed=%q %s", i, r.Status, r.Header.Get("Idempotent-Replayed"), r.Raw)
		}
	}
	mu.Lock()
	if len(calls) != 2 {
		t.Fatalf("the gateway saw %d calls, want both", len(calls))
	}
	for _, c := range calls {
		if c.path != "/gateway/v1/tools/refund_payment" || c.idem != "refund-ORD-1003" ||
			c.approval != "apt_token-for-the-exact-action" || c.apiKey != "" || strings.Contains(c.auth, demoKey) {
			t.Fatalf("forwarded %+v", c)
		}
	}
	calls = nil
	mu.Unlock()

	// The management API under /api keeps the edge's replay: the service
	// never sees the key and a retry is answered by the edge.
	for i := 0; i < 2; i++ {
		r := h.request("POST", "/api/v1/policies", map[string]any{"name": "refund-limit"},
			map[string]string{"X-AgentTwin-Api-Key": demoKey, "Idempotency-Key": "create-policy-0001"})
		if r.Status != 201 {
			t.Fatalf("policy create %d: %d %s", i, r.Status, r.Raw)
		}
	}
	mu.Lock()
	if len(calls) != 1 || calls[0].idem != "" {
		t.Fatalf("management calls forwarded: %+v", calls)
	}
	calls = nil
	mu.Unlock()

	// Unauthenticated calls stop at the edge; only /gateway/v1 is served.
	if r := h.request("POST", "/gateway/v1/tools/refund_payment", map[string]any{}, nil); r.Status != 401 {
		t.Fatalf("unauthenticated tool call: %d", r.Status)
	}
	if r := h.request("POST", "/gateway/v2/tools/refund_payment", map[string]any{}, agent); r.Status != 404 {
		t.Fatalf("unknown gateway version: %d", r.Status)
	}
	mu.Lock()
	defer mu.Unlock()
	if len(calls) != 0 {
		t.Fatalf("refused calls reached the gateway: %+v", calls)
	}
}

func TestProductionRefusesDevelopmentAuth(t *testing.T) {
	env := map[string]string{
		"APP_ENV": "production", "AUTH_MODE": "dev",
		"AGENTTWIN_SESSION_SECRET": "change-me-dev-session-secret-0123456789abcdef",
		"AGENTTWIN_API_KEY_PEPPER": "prod-pepper-0123456789abcdef-0123456789",
	}
	l := config.NewWithLookup(func(k string) (string, bool) { v, ok := env[k]; return v, ok })
	server.LoadConfig(l)
	err := l.Err()
	if err == nil {
		t.Fatal("production with AUTH_MODE=dev must be refused")
	}
	for _, want := range []string{"AUTH_MODE", "AGENTTWIN_SESSION_SECRET", "DEMO_BOOTSTRAP"} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("config error does not mention %s: %v", want, err)
		}
	}
	env["AUTH_MODE"] = "oidc"
	env["AGENTTWIN_SESSION_SECRET"] = "prod-session-secret-0123456789abcdef-zz"
	env["OIDC_ISSUER_URL"] = "https://id.example.com/realms/agenttwin"
	env["OIDC_CLIENT_ID"] = "agenttwin"
	l = config.NewWithLookup(func(k string) (string, bool) { v, ok := env[k]; return v, ok })
	c := server.LoadConfig(l)
	if err := l.Err(); err != nil || c.DemoBootstrap {
		t.Fatalf("valid production config rejected: %v (demo=%v)", err, c.DemoBootstrap)
	}
}

func TestSessionExpiry(t *testing.T) {
	h := newHarness(t, nil)
	tok := h.login("viewer@demo.agenttwin.dev")
	h.s.Auth.SetClock(func() time.Time { return time.Now().Add(auth.SessionTTL + time.Minute) })
	if r := h.request("GET", "/api/v1/me", nil, bearer(tok)); r.Status != http.StatusUnauthorized {
		t.Fatalf("expired session = %d, want 401", r.Status)
	}
}

// items returns the "items" list of a list response.
func items(t *testing.T, r resp) []map[string]any {
	t.Helper()
	list, ok := r.Body["items"].([]any)
	if !ok {
		t.Fatalf("no items: %d %s", r.Status, r.Raw)
	}
	out := make([]map[string]any, 0, len(list))
	for _, it := range list {
		out = append(out, it.(map[string]any))
	}
	return out
}

// TestAPIContractWalk calls the operations of the API contract the other
// tests leave out (TestMain requires every one to be exercised), and holds
// the promises of the contract: malformed ids answer 400, settings are
// validated, a gate policy is visible with settings.read only, lists page
// to a null cursor, an unchanged tool definition answers 200, and rotation
// does not extend a key's lifetime.
func TestAPIContractWalk(t *testing.T) {
	h := newHarness(t, nil)
	pid := h.s.Demo.ProjectID
	owner := h.login("owner@demo.agenttwin.dev")
	viewer := h.login("viewer@demo.agenttwin.dev")

	if r := h.request("GET", "/api/v1/auth/config", nil, nil); r.Status != 200 || r.Body["mode"] != "dev" {
		t.Fatalf("auth config: %d %s", r.Status, r.Raw)
	}
	if r := h.request("GET", "/api/v1/members", nil, bearer(owner)); r.Status != 200 || len(items(t, r)) < 5 {
		t.Fatalf("members: %d %s", r.Status, r.Raw)
	}
	if r := h.request("GET", "/api/v1/members", nil, bearer(viewer)); r.Status != 403 {
		t.Fatalf("members as viewer: %d", r.Status)
	}

	// Settings: a change, a gate policy, and an invalid name (a 400, not a
	// database error).
	r := h.request("PATCH", "/api/v1/projects/"+pid, map[string]any{
		"description": "Support agents", "trace_retention_days": 14,
		"gate_policy": map[string]any{"maxDepth": 3, "alwaysRunTags": []string{"smoke"}},
	}, bearer(owner))
	if r.Status != 200 || r.Body["trace_retention_days"].(float64) != 14 {
		t.Fatalf("update settings: %d %s", r.Status, r.Raw)
	}
	for _, bad := range []map[string]any{{"name": ""}, {"name": strings.Repeat("n", 201)}} {
		if r := h.request("PATCH", "/api/v1/projects/"+pid, bad, bearer(owner)); r.Status != 400 || errCode(r) != "INVALID_SETTINGS" {
			t.Fatalf("invalid name: %d %s", r.Status, r.Raw)
		}
	}
	if r := h.request("PATCH", "/api/v1/projects/"+pid, map[string]any{"gate_policy": map[string]any{"maxDepth": 99}}, bearer(owner)); r.Status != 400 || errCode(r) != "INVALID_GATE_POLICY" {
		t.Fatalf("invalid gate policy: %d %s", r.Status, r.Raw)
	}
	// The gate policy is visible with settings.read only, in a list as in a get.
	gate := func(tok string) (listed, got map[string]any) {
		for _, p := range items(t, h.request("GET", "/api/v1/projects", nil, bearer(tok))) {
			if p["id"] == pid {
				listed = p["gate_policy"].(map[string]any)
			}
		}
		return listed, h.request("GET", "/api/v1/projects/"+pid, nil, bearer(tok)).Body["gate_policy"].(map[string]any)
	}
	if listed, got := gate(owner); listed["maxDepth"] != 3.0 || got["maxDepth"] != 3.0 {
		t.Fatalf("owner gate policy: %v %v", listed, got)
	}
	if listed, got := gate(viewer); len(listed) != 0 || len(got) != 0 {
		t.Fatalf("a viewer sees the gate policy: %v %v", listed, got)
	}
	if r := h.request("GET", "/api/v1/projects/"+pid+"/environments", nil, bearer(viewer)); r.Status != 200 || len(items(t, r)) != 3 {
		t.Fatalf("environments: %d %s", r.Status, r.Raw)
	}

	// Malformed ids answer 400 INVALID_PARAMETER instead of reaching the database.
	for _, c := range []struct{ method, path, field string }{
		{"GET", "/api/v1/projects/nope", "project_id"},
		{"GET", "/api/v1/projects/nope/tools", "project_id"},
		{"GET", "/api/v1/agents/nope", "agent_id"},
		{"GET", "/api/v1/agents/nope/versions/1.0.0", "agent_id"},
		{"POST", "/api/v1/api-keys/nope/rotate", "key_id"},
	} {
		r := h.request(c.method, c.path, nil, bearer(owner))
		details, _ := r.Body["error"].(map[string]any)["details"].(map[string]any)
		if r.Status != 400 || errCode(r) != "INVALID_PARAMETER" || details["field"] != c.field {
			t.Fatalf("%s %s: %d %s", c.method, c.path, r.Status, r.Raw)
		}
	}

	// Agents: one created directly, one from its manifest with a second
	// version registered through the agent.
	r = h.request("POST", "/api/v1/projects/"+pid+"/agents", map[string]any{"name": "triage-agent", "description": "Routes tickets."}, bearer(owner))
	if r.Status != 201 || r.Body["version_count"].(float64) != 0 || r.Body["latest_version"] != nil {
		t.Fatalf("create agent: %d %s", r.Status, r.Raw)
	}
	triage := r.Body["id"].(string)
	if r := h.request("GET", "/api/v1/agents/"+triage, nil, bearer(viewer)); r.Status != 200 || r.Body["name"] != "triage-agent" {
		t.Fatalf("get agent: %d %s", r.Status, r.Raw)
	}
	r = h.registerManifest(owner, pid, readManifest(t, "1.2.4"))
	if r.Status != 201 {
		t.Fatalf("register: %d %s", r.Status, r.Raw)
	}
	agent := r.Body["agent"].(map[string]any)["id"].(string)
	r = h.request("POST", "/api/v1/agents/"+agent+"/versions", readManifest(t, "1.3.0"),
		map[string]string{"Authorization": "Bearer " + owner, "Content-Type": "application/yaml"})
	if r.Status != 201 || r.Body["version"].(map[string]any)["version"] != "1.3.0" {
		t.Fatalf("version through the agent: %d %s", r.Status, r.Raw)
	}
	if r := h.request("POST", "/api/v1/agents/"+triage+"/versions", readManifest(t, "1.3.1"),
		map[string]string{"Authorization": "Bearer " + owner, "Content-Type": "application/yaml"}); r.Status != 400 || errCode(r) != "MANIFEST_AGENT_MISMATCH" {
		t.Fatalf("manifest of another agent: %d %s", r.Status, r.Raw)
	}
	if list := items(t, h.request("GET", "/api/v1/projects/"+pid+"/agents", nil, bearer(viewer))); len(list) != 2 {
		t.Fatalf("agents: %v", list)
	}
	versions := items(t, h.request("GET", "/api/v1/agents/"+agent+"/versions", nil, bearer(viewer)))
	if len(versions) != 2 || versions[0]["version"] != "1.3.0" {
		t.Fatalf("versions, newest first: %v", versions)
	}
	r = h.request("GET", "/api/v1/agents/"+agent+"/versions/1.2.4", nil, bearer(viewer))
	if r.Status != 200 || len(r.Body["tools"].([]any)) != 7 {
		t.Fatalf("get version: %d %s", r.Status, r.Raw)
	}
	versionID := r.Body["id"].(string)
	if r := h.request("GET", "/api/v1/agents/"+agent+"/versions/9.9.9", nil, bearer(viewer)); r.Status != 404 {
		t.Fatalf("unknown version: %d", r.Status)
	}

	// Validation takes the JSON envelope too; the envelope is strict.
	manifest := string(readManifest(t, "1.3.1"))
	r = h.request("POST", "/api/v1/manifests/validate", map[string]any{"manifest": manifest}, bearer(viewer))
	if r.Status != 200 || r.Body["valid"] != true || r.Body["normalized"].(map[string]any)["version"] != "1.3.1" {
		t.Fatalf("validate: %d %s", r.Status, r.Raw)
	}
	if r := h.request("POST", "/api/v1/manifests/validate", map[string]any{"manifest": manifest, "commit": "abc1234"}, bearer(viewer)); r.Status != 400 || errCode(r) != "INVALID_JSON" {
		t.Fatalf("unknown envelope field: %d %s", r.Status, r.Raw)
	}

	// Tools: a new definition is stored (201); the same again stores nothing (200).
	tool := map[string]any{"name": "escalate_ticket", "description": "Hands a ticket to a person.", "risk": "WRITE_REVERSIBLE",
		"input_schema": map[string]any{"type": "object", "properties": map[string]any{"ticket_id": map[string]any{"type": "string"}}}}
	if r := h.request("POST", "/api/v1/projects/"+pid+"/tools", tool, bearer(owner)); r.Status != 201 || r.Body["created"] != true {
		t.Fatalf("create tool: %d %s", r.Status, r.Raw)
	}
	if r := h.request("POST", "/api/v1/projects/"+pid+"/tools", tool, bearer(owner)); r.Status != 200 || r.Body["created"] != false {
		t.Fatalf("unchanged tool: %d %s", r.Status, r.Raw)
	}

	// Rotation keeps the expiry; an expired key cannot be rotated.
	r = h.request("POST", "/api/v1/projects/"+pid+"/api-keys", map[string]any{"name": "ci", "scopes": []string{"ci"}, "expires_in_days": 30}, bearer(owner))
	if r.Status != 201 {
		t.Fatalf("create key: %d %s", r.Status, r.Raw)
	}
	keyID, expires := r.Body["id"].(string), r.Body["expires_at"]
	if r := h.request("POST", "/api/v1/api-keys/"+keyID+"/rotate", nil, bearer(owner)); r.Status != 201 || r.Body["expires_at"] != expires {
		t.Fatalf("rotation extended the key's lifetime: %v -> %d %s", expires, r.Status, r.Raw)
	}
	r = h.request("POST", "/api/v1/projects/"+pid+"/api-keys", map[string]any{"name": "old", "scopes": []string{"read"}, "expires_in_days": 1}, bearer(owner))
	expired := r.Body["id"].(string)
	if _, err := h.pool.Exec(context.Background(), `UPDATE control.api_key SET expires_at = now() - interval '1 hour' WHERE id = $1`, expired); err != nil {
		t.Fatal(err)
	}
	if r := h.request("POST", "/api/v1/api-keys/"+expired+"/rotate", nil, bearer(owner)); r.Status != 409 || errCode(r) != "API_KEY_EXPIRED" {
		t.Fatalf("rotate an expired key: %d %s", r.Status, r.Raw)
	}
	// A revocation without a body.
	if r := h.request("POST", "/api/v1/api-keys/"+expired+"/revoke", nil, bearer(owner)); r.Status != 200 || r.Body["revoked_at"] == nil {
		t.Fatalf("revoke without a body: %d %s", r.Status, r.Raw)
	}

	// The audit log pages newest first to a null cursor.
	seen, cursor := 0, ""
	for page := 0; ; page++ {
		path := "/api/v1/audit?limit=4"
		if cursor != "" {
			path += "&cursor=" + cursor
		}
		r := h.request("GET", path, nil, bearer(owner))
		if r.Status != 200 {
			t.Fatalf("audit page: %d %s", r.Status, r.Raw)
		}
		seen += len(items(t, r))
		next, isString := r.Body["next_cursor"].(string)
		if !isString {
			if r.Body["next_cursor"] != nil {
				t.Fatalf("next_cursor: %s", r.Raw)
			}
			break
		}
		cursor = next
		if page > 50 {
			t.Fatal("the audit log does not end")
		}
	}
	if want := h.count(`SELECT count(*) FROM control.audit_event`); seen != want || seen < 8 {
		t.Fatalf("audit entries paged = %d, stored = %d", seen, want)
	}

	// The internal lookup of a version by id, for a service acting for the organization.
	tok, err := h.tokens.Mint(authn.ServicePrincipal("simulation-service", h.s.Demo.OrganizationID), "control-plane", "rid-12345678")
	if err != nil {
		t.Fatal(err)
	}
	if r := h.request("GET", "/internal/v1/agent-versions/"+versionID, nil, bearer(tok)); r.Status != 200 || r.Body["version"] != "1.2.4" {
		t.Fatalf("internal version lookup: %d %s", r.Status, r.Raw)
	}
	if r := h.request("GET", "/internal/v1/agent-versions/nope", nil, bearer(tok)); r.Status != 400 || errCode(r) != "INVALID_PARAMETER" {
		t.Fatalf("internal lookup of a malformed id: %d %s", r.Status, r.Raw)
	}
}

// A person may act in every project of their organization; a service cannot
// tell which organization a project id belongs to. Through the edge, a
// person of another organization therefore cannot name this one's project:
// the internal token lists their own projects, read fresh on every request.
func TestTheEdgeNamesOnlyTheCallersProjects(t *testing.T) {
	tokens, _ := authn.NewTokenService(internalSecret)
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		p, _, err := tokens.Verify(strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer "), "graph-service")
		switch {
		case err != nil:
			w.WriteHeader(http.StatusUnauthorized)
		case p.AllProjects || !p.CanAccessProject(r.URL.Query().Get("project_id")):
			// What every service does: another organization's project
			// looks like a missing one.
			w.WriteHeader(http.StatusNotFound)
		default:
			w.Header().Set("Content-Type", "application/json")
			_, _ = io.WriteString(w, `{}`)
		}
	}))
	defer upstream.Close()
	h := newHarness(t, map[string]string{"graph-service": upstream.URL})
	pid := h.s.Demo.ProjectID
	eng := h.login("engineer@demo.agenttwin.dev")
	other := h.login("owner@other.agenttwin.dev")
	if r := h.request("GET", "/api/v1/graph?project_id="+pid, nil, bearer(eng)); r.Status != 200 {
		t.Fatalf("own project: %d %s", r.Status, r.Raw)
	}
	if r := h.request("GET", "/api/v1/graph?project_id="+pid, nil, bearer(other)); r.Status != 404 {
		t.Errorf("another organization's project: %d %s", r.Status, r.Raw)
	}
	created := h.request("POST", "/api/v1/projects", map[string]any{"slug": "other-graph", "name": "Other graph"}, bearer(other))
	if created.Status != 201 {
		t.Fatalf("create: %d %s", created.Status, created.Raw)
	}
	fresh := created.Body["id"].(string)
	if r := h.request("GET", "/api/v1/graph?project_id="+fresh, nil, bearer(other)); r.Status != 200 {
		t.Errorf("a project created a moment ago: %d %s", r.Status, r.Raw)
	}
	if r := h.request("GET", "/api/v1/graph?project_id="+fresh, nil, bearer(eng)); r.Status != 404 {
		t.Errorf("the other organization's new project, from the demo organization: %d", r.Status)
	}
}

// An organization's projects are bounded (they are named in every internal
// token), also when created concurrently.
func TestAnOrganizationHasBoundedProjects(t *testing.T) {
	h := newHarness(t, nil)
	owner := h.login("owner@other.agenttwin.dev")
	create := func(i int) resp {
		return h.request("POST", "/api/v1/projects", map[string]any{"slug": fmt.Sprintf("p-%03d", i), "name": "P"}, bearer(owner))
	}
	have := h.count(`SELECT count(*) FROM control.project p JOIN control.organization o ON o.id = p.organization_id
		JOIN control.membership m ON m.organization_id = o.id JOIN control.app_user u ON u.id = m.user_id
		WHERE u.email = 'owner@other.agenttwin.dev'`)
	for i := have; i < authn.MaxTokenProjects-5; i++ {
		if r := create(i); r.Status != 201 {
			t.Fatalf("project %d: %d %s", i, r.Status, r.Raw)
		}
	}
	var wg sync.WaitGroup
	var mu sync.Mutex
	statuses := map[int]int{}
	for i := range 10 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			r := create(1000 + i)
			mu.Lock()
			statuses[r.Status]++
			if r.Status == 409 && errCode(r) != "PROJECT_LIMIT_REACHED" {
				t.Errorf("limit code = %s", errCode(r))
			}
			mu.Unlock()
		}()
	}
	wg.Wait()
	if statuses[201] != 5 || statuses[409] != 5 {
		t.Errorf("concurrent creations at %d of %d: %v", authn.MaxTokenProjects-5, authn.MaxTokenProjects, statuses)
	}
}

// A key past its expiry is refused at the edge and by the internal
// verification the trace service and runtime gateway rely on (spec §63).
func TestAnExpiredKeyIsRefusedEverywhere(t *testing.T) {
	h := newHarness(t, nil)
	pid := h.s.Demo.ProjectID
	admin := h.login("admin@demo.agenttwin.dev")
	created := h.request("POST", "/api/v1/projects/"+pid+"/api-keys",
		map[string]any{"name": "short-lived", "scopes": []string{"read", "traces:write"}}, bearer(admin))
	if created.Status != http.StatusCreated {
		t.Fatalf("create key: %d %s", created.Status, created.Raw)
	}
	key, keyID := created.Body["key"].(string), created.Body["id"].(string)
	svc, err := h.tokens.Mint(authn.SystemPrincipal("trace-service"), "control-plane", "rid-12345678")
	if err != nil {
		t.Fatal(err)
	}
	valid := func() any {
		r := h.request("POST", "/internal/v1/api-keys/verify", map[string]string{"key": key}, bearer(svc))
		if r.Status != http.StatusOK {
			t.Fatalf("verify: %d %s", r.Status, r.Raw)
		}
		return r.Body["valid"]
	}
	if r := h.request("GET", "/api/v1/me", nil, map[string]string{"X-AgentTwin-Api-Key": key}); r.Status != http.StatusOK || valid() != true {
		t.Fatalf("a fresh key works: %d", r.Status)
	}

	if _, err := h.pool.Exec(context.Background(),
		`UPDATE control.api_key SET expires_at = now() - interval '1 second' WHERE id = $1`, keyID); err != nil {
		t.Fatal(err)
	}
	r := h.request("GET", "/api/v1/me", nil, map[string]string{"X-AgentTwin-Api-Key": key})
	if r.Status != http.StatusUnauthorized || bytes.Contains(r.Raw, []byte(key)) {
		t.Fatalf("expired key at the edge = %d %s, want 401 without the key", r.Status, r.Raw)
	}
	if valid() != false {
		t.Fatal("the internal verification accepts an expired key")
	}
	// Nor can it be brought back by rotation.
	if r := h.request("POST", "/api/v1/api-keys/"+keyID+"/rotate", nil, bearer(admin)); r.Status != http.StatusConflict {
		t.Fatalf("rotating an expired key = %d, want 409", r.Status)
	}
}
