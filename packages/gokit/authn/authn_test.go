package authn

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/golang-jwt/jwt/v5"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/ids"
)

const secret = "0123456789abcdef0123456789abcdef-test"

func TestRBACMatrix(t *testing.T) {
	cases := []struct {
		role Role
		perm Permission
		want bool
	}{
		{RoleViewer, PermRead, true},
		{RoleViewer, PermSettingsRead, false},
		{RoleViewer, PermSimulationRun, false},
		{RoleViewer, PermApprovalDecide, false},
		{RoleEngineer, PermSimulationRun, true},
		{RoleEngineer, PermReleaseWrite, true},
		{RoleEngineer, PermRegressionPromote, false},
		{RoleEngineer, PermPolicyActivate, false},
		{RoleEngineer, PermApprovalDecide, false},
		{RoleReviewer, PermRegressionPromote, true},
		{RoleReviewer, PermApprovalDecide, true},
		{RoleReviewer, PermSimulationRun, false},
		{RoleReviewer, PermPolicyWrite, false},
		{RoleReviewer, PermSettingsRead, false},
		{RoleAdmin, PermPolicyActivate, true},
		{RoleAdmin, PermAPIKeyManage, true},
		{RoleOwner, PermReleaseOverride, true},
		{Role("MALLORY"), PermRead, false},
	}
	for _, c := range cases {
		p := Principal{OrgID: "o", Actor: "user:1", Role: c.role}
		if got := p.Can(c.perm); got != c.want {
			t.Errorf("%s.Can(%s)=%v want %v", c.role, c.perm, got, c.want)
		}
	}
}

func TestAPIKeyScopes(t *testing.T) {
	ingest := Principal{OrgID: "o", Actor: "apikey:1", Role: RoleAPIKey, Scopes: []Scope{ScopeTracesWrite}}
	if !ingest.Can(PermTraceWrite) || ingest.Can(PermRead) || ingest.Can(PermReleaseWrite) {
		t.Fatal("traces:write key must only write traces")
	}
	ci := Principal{OrgID: "o", Actor: "apikey:2", Role: RoleAPIKey, Scopes: []Scope{ScopeCI}}
	if !ci.Can(PermReleaseWrite) || ci.Can(PermPolicyActivate) || ci.Can(PermApprovalDecide) {
		t.Fatal("ci key scope wrong")
	}
	if ValidScope("admin:*") {
		t.Fatal("unknown scope must be invalid")
	}
}

func TestProjectAccess(t *testing.T) {
	p := Principal{ProjectIDs: []string{"p1"}}
	if !p.CanAccessProject("p1") || p.CanAccessProject("p2") || p.CanAccessProject("") {
		t.Fatal("project scoping broken")
	}
	all := Principal{AllProjects: true}
	if !all.CanAccessProject("anything") || all.CanAccessProject("") {
		t.Fatal("all-projects scoping broken")
	}
	// Project ids are UUIDs: case-insensitive, and nothing more lenient.
	id := "0190f3b4-0000-7000-8000-00000000abcd"
	scoped := Principal{ProjectIDs: []string{id}}
	if !scoped.CanAccessProject(strings.ToUpper(id)) || scoped.CanAccessProject(id[:35]) ||
		scoped.CanAccessProject(" "+id) {
		t.Fatal("project ids must match case-insensitively and exactly otherwise")
	}
}

func TestMintVerifyRoundTrip(t *testing.T) {
	ts, err := NewTokenService(secret)
	if err != nil {
		t.Fatal(err)
	}
	p := Principal{OrgID: "org-1", Actor: "user:u1", Role: RoleEngineer, AllProjects: true, Email: "a@example.com"}
	tok, err := ts.Mint(p, "trace-service", "req-1")
	if err != nil {
		t.Fatal(err)
	}
	got, rid, err := ts.Verify(tok, "trace-service")
	if err != nil {
		t.Fatal(err)
	}
	if got.OrgID != "org-1" || got.Actor != "user:u1" || got.Role != RoleEngineer || !got.AllProjects || rid != "req-1" {
		t.Fatalf("round trip mismatch: %+v rid=%s", got, rid)
	}
}

// A token names at most MaxTokenProjects projects, and a token naming that
// many stays small enough for every service's header limits (the Python
// services' HTTP parser refuses headers past 16 KiB; 8 KiB is a common
// proxy default).
func TestTokensNameABoundedNumberOfProjects(t *testing.T) {
	ts, _ := NewTokenService(secret)
	many := make([]string, MaxTokenProjects)
	for i := range many {
		many[i] = ids.New()
	}
	p := Principal{OrgID: ids.New(), Actor: "user:u1", Role: RoleEngineer, ProjectIDs: many, Email: "someone-with-a-long-address@example.com"}
	tok, err := ts.Mint(p, "evaluation-service", ids.New())
	if err != nil {
		t.Fatal(err)
	}
	if len("Authorization: Bearer "+tok) > 8<<10 {
		t.Errorf("a token naming %d projects is %d bytes", MaxTokenProjects, len(tok))
	}
	got, _, err := ts.Verify(tok, "evaluation-service")
	if err != nil || len(got.ProjectIDs) != MaxTokenProjects || !got.CanAccessProject(many[MaxTokenProjects-1]) {
		t.Fatalf("round trip: %v %d", err, len(got.ProjectIDs))
	}
	p.ProjectIDs = append(p.ProjectIDs, ids.New())
	if _, err := ts.Mint(p, "evaluation-service", ""); err == nil {
		t.Error("a token naming too many projects was minted")
	}
}

func TestVerifyRejections(t *testing.T) {
	ts, _ := NewTokenService(secret)
	p := Principal{OrgID: "org-1", Actor: "user:u1", Role: RoleViewer}
	tok, _ := ts.Mint(p, "graph-service", "")

	if _, _, err := ts.Verify(tok, "trace-service"); err == nil {
		t.Error("audience mismatch must be rejected")
	}
	other, _ := NewTokenService(strings.Repeat("x", 40))
	if _, _, err := other.Verify(tok, "graph-service"); err == nil {
		t.Error("wrong key must be rejected")
	}
	// Tamper with payload.
	parts := strings.Split(tok, ".")
	tampered := parts[0] + "." + parts[1] + "x." + parts[2]
	if _, _, err := ts.Verify(tampered, "graph-service"); err == nil {
		t.Error("tampered token must be rejected")
	}
	// Expired.
	past := time.Now().Add(-10 * time.Minute)
	old, _ := NewTokenService(secret)
	old.SetClock(func() time.Time { return past })
	expired, _ := old.Mint(p, "graph-service", "")
	if _, _, err := ts.Verify(expired, "graph-service"); err == nil {
		t.Error("expired token must be rejected")
	}
	// alg=none.
	none := jwt.NewWithClaims(jwt.SigningMethodNone, jwt.MapClaims{"iss": InternalIssuer, "aud": "graph-service", "sub": "user:x", "org": "o", "role": "OWNER", "exp": time.Now().Add(time.Minute).Unix()})
	noneTok, _ := none.SignedString(jwt.UnsafeAllowNoneSignatureType)
	if _, _, err := ts.Verify(noneTok, "graph-service"); err == nil {
		t.Error("alg none must be rejected")
	}
	if _, err := ts.Mint(Principal{Actor: "x", Role: RoleViewer}, "a", ""); err == nil {
		t.Error("mint without org must fail")
	}
	if _, err := NewTokenService("short"); err == nil {
		t.Error("short secret must be rejected")
	}
}

func TestRequireInternalMiddleware(t *testing.T) {
	ts, _ := NewTokenService(secret)
	h := RequireInternal(ts, "svc")(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		p, ok := FromContext(r.Context())
		if !ok || p.OrgID != "org-9" {
			t.Errorf("principal missing: %+v", p)
		}
		w.WriteHeader(http.StatusNoContent)
	}))
	// Missing token.
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/api/v1/x", nil))
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("missing token: got %d", rec.Code)
	}
	// Health exempt.
	rec = httptest.NewRecorder()
	h2 := RequireInternal(ts, "svc")(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(200) }))
	h2.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/health/live", nil))
	if rec.Code != 200 {
		t.Fatalf("health must be exempt: %d", rec.Code)
	}
	// Valid token.
	tok, _ := ts.Mint(Principal{OrgID: "org-9", Actor: "user:1", Role: RoleViewer}, "svc", "r1")
	req := httptest.NewRequest(http.MethodGet, "/api/v1/x", nil)
	req.Header.Set("Authorization", "Bearer "+tok)
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusNoContent {
		t.Fatalf("valid token: got %d %s", rec.Code, rec.Body.String())
	}
}

func TestRequireProjectHidesOtherProjects(t *testing.T) {
	req := httptest.NewRequest(http.MethodGet, "/", nil)
	req = req.WithContext(WithPrincipal(req.Context(), Principal{OrgID: "o", Actor: "user:1", Role: RoleViewer, ProjectIDs: []string{"p1"}}))
	if _, err := RequireProject(req, PermRead, "p1"); err != nil {
		t.Fatalf("own project: %v", err)
	}
	_, err := RequireProject(req, PermRead, "p2")
	if err == nil || !strings.Contains(err.Error(), "NOT_FOUND") {
		t.Fatalf("foreign project must look like NOT_FOUND, got %v", err)
	}
	if _, err := RequireProject(req, PermSimulationRun, "p1"); err == nil || !strings.Contains(err.Error(), "FORBIDDEN") {
		t.Fatalf("missing permission must be FORBIDDEN, got %v", err)
	}
}
