package proxy

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"slices"
	"strings"
	"testing"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/authn"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/httpx"
)

// An upstream service answers with the same conventions as the edge (request
// id, security headers). Through the proxy each header must reach the client
// once, with the edge's value; the upstream's other headers pass through.
func TestProxiedResponsesCarryTheEdgeHeadersOnce(t *testing.T) {
	var sawAuth, sawRequestID string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		sawAuth = r.Header.Get("Authorization")
		sawRequestID = r.Header.Get(httpx.RequestIDHeader)
		h := w.Header()
		h.Set(httpx.RequestIDHeader, "upstream-generated-id")
		h.Set("X-Content-Type-Options", "nosniff")
		h.Set("X-Frame-Options", "DENY")
		h.Set("Referrer-Policy", "no-referrer")
		h.Set("Cache-Control", "no-store")
		h.Set("Server", "uvicorn")
		h.Set("Content-Type", "application/json")
		h.Set("Retry-After", "2")
		_, _ = io.WriteString(w, `{"items":[]}`)
	}))
	defer upstream.Close()

	tokens, err := authn.NewTokenService("test-secret-0123456789abcdef0123456789")
	if err != nil {
		t.Fatal(err)
	}
	p, err := New(tokens, map[string]string{"simulation-service": upstream.URL}, noProjects, slog.New(slog.DiscardHandler))
	if err != nil {
		t.Fatal(err)
	}
	principal := authn.ServicePrincipal("tester", "0190f3b4-0000-7000-8000-00000000000a")
	withPrincipal := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		p.ServeHTTP(w, r.WithContext(authn.WithPrincipal(r.Context(), principal)))
	})
	edge := httpx.Chain(withPrincipal, httpx.RequestID(), httpx.SecurityHeaders())

	req := httptest.NewRequest(http.MethodGet, "/api/v1/simulations", nil)
	req.Header.Set(httpx.RequestIDHeader, "client-request-0001")
	req.Header.Set("Authorization", "Bearer client-credential")
	rec := httptest.NewRecorder()
	edge.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK || rec.Body.String() != `{"items":[]}` {
		t.Fatalf("got %d %q", rec.Code, rec.Body.String())
	}
	for _, name := range httpx.OwnedResponseHeaders() {
		if got := rec.Header().Values(name); len(got) != 1 {
			t.Errorf("%s = %q, want exactly one value", name, got)
		}
	}
	if got := rec.Header().Get(httpx.RequestIDHeader); got != "client-request-0001" {
		t.Errorf("request id = %q, want the edge's", got)
	}
	if got := rec.Header().Get("Retry-After"); got != "2" {
		t.Errorf("upstream header lost: Retry-After = %q", got)
	}
	if got := rec.Header().Get("Server"); got != "" {
		t.Errorf("upstream Server header leaked: %q", got)
	}
	// The client's credential never reaches the service; an internal token does,
	// with the request id for correlation.
	if sawAuth == "Bearer client-credential" || len(sawAuth) < len("Bearer x") {
		t.Errorf("upstream Authorization = %q, want an internal token", sawAuth)
	}
	if sawRequestID != "client-request-0001" {
		t.Errorf("upstream request id = %q", sawRequestID)
	}
}

func noProjects(context.Context, string) ([]string, error) { return nil, nil }

const org = "0190f3b4-0000-7000-8000-00000000000a"

// forwarded proxies one request as principal and returns what the upstream
// service was told about the caller.
func forwarded(t *testing.T, principal authn.Principal, projects OrgProjects) (int, authn.Principal, bool) {
	t.Helper()
	tokens, err := authn.NewTokenService("test-secret-0123456789abcdef0123456789")
	if err != nil {
		t.Fatal(err)
	}
	var seen authn.Principal
	called := false
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		called = true
		seen, _, err = tokens.Verify(strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer "), "graph-service")
		if err != nil {
			t.Errorf("upstream token: %v", err)
		}
		_, _ = io.WriteString(w, `{}`)
	}))
	defer upstream.Close()
	p, err := New(tokens, map[string]string{"graph-service": upstream.URL}, projects, slog.New(slog.DiscardHandler))
	if err != nil {
		t.Fatal(err)
	}
	req := httptest.NewRequest(http.MethodGet, "/api/v1/graph?project_id=x", nil)
	req = req.WithContext(authn.WithPrincipal(req.Context(), principal))
	rec := httptest.NewRecorder()
	p.ServeHTTP(rec, req)
	return rec.Code, seen, called
}

// A person may act in every project of their organization. A service cannot
// tell which organization a project id belongs to, so the token names them:
// another organization's project is not among them.
func TestAPersonsTokenNamesTheirOrganizationsProjects(t *testing.T) {
	person := authn.Principal{OrgID: org, Actor: "user:u", Role: authn.RoleEngineer, AllProjects: true}
	asked := ""
	code, seen, _ := forwarded(t, person, func(_ context.Context, orgID string) ([]string, error) {
		asked = orgID
		return []string{"p-1", "p-2"}, nil
	})
	if code != 200 || asked != org {
		t.Fatalf("%d, projects of %q", code, asked)
	}
	if seen.AllProjects || !slices.Equal(seen.ProjectIDs, []string{"p-1", "p-2"}) {
		t.Errorf("the service was told %+v", seen)
	}
	if !seen.CanAccessProject("p-2") || seen.CanAccessProject("p-of-another-organization") {
		t.Error("project access does not follow the organization's projects")
	}
	// An organization without projects: access to none, not to all.
	_, seen, _ = forwarded(t, person, noProjects)
	if seen.AllProjects || seen.CanAccessProject("p-1") {
		t.Errorf("no projects: %+v", seen)
	}
}

func TestAKeysProjectsAreNotLookedUp(t *testing.T) {
	key := authn.Principal{OrgID: org, Actor: "apikey:k", Role: authn.RoleAPIKey, ProjectIDs: []string{"p-9"}, Scopes: []authn.Scope{authn.ScopeRead}}
	code, seen, _ := forwarded(t, key, func(context.Context, string) ([]string, error) {
		t.Error("a key's projects were looked up")
		return nil, nil
	})
	if code != 200 || !slices.Equal(seen.ProjectIDs, []string{"p-9"}) {
		t.Errorf("%d %+v", code, seen)
	}
}

func TestNoProjectListNoForwarding(t *testing.T) {
	person := authn.Principal{OrgID: org, Actor: "user:u", Role: authn.RoleEngineer, AllProjects: true}
	code, _, called := forwarded(t, person, func(context.Context, string) ([]string, error) {
		return nil, errors.New("database down")
	})
	if code != http.StatusServiceUnavailable || called {
		t.Errorf("lookup failure: %d, forwarded %v", code, called)
	}
	// A token that cannot be minted is the edge's failure, not a 401 from
	// the service.
	many := make([]string, authn.MaxTokenProjects+1)
	for i := range many {
		many[i] = "p"
	}
	code, _, called = forwarded(t, person, func(context.Context, string) ([]string, error) { return many, nil })
	if code != http.StatusInternalServerError || called {
		t.Errorf("unmintable token: %d, forwarded %v", code, called)
	}
	if _, err := New(nil, nil, nil, slog.New(slog.DiscardHandler)); err == nil {
		t.Error("a proxy without the organization's projects was built")
	}
}
