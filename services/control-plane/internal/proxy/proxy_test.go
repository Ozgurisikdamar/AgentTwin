package proxy

import (
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
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
	p, err := New(tokens, map[string]string{"simulation-service": upstream.URL}, slog.New(slog.DiscardHandler))
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
