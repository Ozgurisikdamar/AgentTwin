package server_test

import (
	"bytes"
	"context"
	"encoding/json"
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

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/authn"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/config"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/db"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/httpx"
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
