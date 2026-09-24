package httpx

import (
	"context"
	"encoding/json"
	"errors"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestWriteErrorEnvelopeHidesInternals(t *testing.T) {
	h := Chain(Handle(func(w http.ResponseWriter, r *http.Request) error {
		return errors.New("pq: password authentication failed for user secret-user")
	}), RequestID())
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/x", nil))
	if rec.Code != 500 {
		t.Fatalf("status %d", rec.Code)
	}
	if strings.Contains(rec.Body.String(), "secret-user") {
		t.Fatal("internal error details leaked to client")
	}
	var body errorBody
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	if body.Error.Code != "INTERNAL" || body.Error.RequestID == "" || body.Error.RequestID != rec.Header().Get(RequestIDHeader) {
		t.Fatalf("bad envelope: %+v", body)
	}
}

func TestAPIErrorsAreRenderedWithDetails(t *testing.T) {
	h := Handle(func(w http.ResponseWriter, r *http.Request) error {
		return Invalid("SCENARIO_INVALID", "Scenario contains an unknown tool.", map[string]any{"tool": "wire_money"})
	})
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/x", nil))
	if rec.Code != 400 || !strings.Contains(rec.Body.String(), `"SCENARIO_INVALID"`) || !strings.Contains(rec.Body.String(), "wire_money") {
		t.Fatalf("got %d %s", rec.Code, rec.Body.String())
	}
}

func TestDecodeJSON(t *testing.T) {
	type payload struct {
		Name string `json:"name"`
	}
	cases := []struct {
		name, body, ct string
		max            int64
		wantCode       string
	}{
		{"ok", `{"name":"a"}`, "application/json", 0, ""},
		{"unknown field", `{"name":"a","extra":1}`, "application/json", 0, "INVALID_JSON"},
		{"too large", `{"name":"` + strings.Repeat("a", 100) + `"}`, "application/json", 20, "PAYLOAD_TOO_LARGE"},
		{"empty", ``, "application/json", 0, "EMPTY_BODY"},
		{"trailing", `{"name":"a"}{"name":"b"}`, "application/json", 0, "INVALID_JSON"},
		{"wrong content type", `{"name":"a"}`, "text/plain", 0, "UNSUPPORTED_MEDIA_TYPE"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			r := httptest.NewRequest(http.MethodPost, "/", strings.NewReader(c.body))
			r.Header.Set("Content-Type", c.ct)
			var p payload
			err := DecodeJSON(httptest.NewRecorder(), r, &p, c.max)
			if c.wantCode == "" {
				if err != nil || p.Name != "a" {
					t.Fatalf("err=%v p=%+v", err, p)
				}
				return
			}
			var apiErr *Error
			if !errors.As(err, &apiErr) || apiErr.Code != c.wantCode {
				t.Fatalf("want %s got %v", c.wantCode, err)
			}
		})
	}
}

func TestRequestIDPropagationAndSanitization(t *testing.T) {
	var seen string
	h := RequestID()(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen = r.Header.Get(RequestIDHeader)
	}))
	r := httptest.NewRequest(http.MethodGet, "/", nil)
	r.Header.Set(RequestIDHeader, "abc-12345678")
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, r)
	if rec.Header().Get(RequestIDHeader) != "abc-12345678" || seen != "abc-12345678" {
		t.Fatal("valid incoming request id must be kept")
	}
	r2 := httptest.NewRequest(http.MethodGet, "/", nil)
	r2.Header.Set(RequestIDHeader, "<script>alert(1)</script>")
	rec2 := httptest.NewRecorder()
	h.ServeHTTP(rec2, r2)
	if strings.Contains(rec2.Header().Get(RequestIDHeader), "<") {
		t.Fatal("malicious request id must be replaced")
	}
}

func TestRecoverTurnsPanicInto500(t *testing.T) {
	h := Chain(http.HandlerFunc(func(http.ResponseWriter, *http.Request) { panic("boom") }), RequestID(), Recover())
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/", nil))
	if rec.Code != 500 || strings.Contains(rec.Body.String(), "boom") {
		t.Fatalf("got %d %s", rec.Code, rec.Body.String())
	}
}

func TestRateLimiter(t *testing.T) {
	now := time.Unix(1000, 0)
	rl := NewRateLimiter(1, 2)
	rl.now = func() time.Time { return now }
	first, second := rl.Allow("k"), rl.Allow("k")
	if !first || !second {
		t.Fatal("burst of 2 must pass")
	}
	if rl.Allow("k") {
		t.Fatal("third request must be limited")
	}
	if !rl.Allow("other") {
		t.Fatal("keys are independent")
	}
	now = now.Add(1100 * time.Millisecond)
	if !rl.Allow("k") {
		t.Fatal("token must refill")
	}
}

func TestCursorRoundTripAndRejectsGarbage(t *testing.T) {
	c := Cursor{TS: time.Date(2026, 9, 24, 10, 0, 0, 0, time.UTC), ID: "abc"}
	got, err := DecodeCursor(EncodeCursor(c))
	if err != nil || got.ID != "abc" || !got.TS.Equal(c.TS) {
		t.Fatalf("round trip: %+v %v", got, err)
	}
	for _, bad := range []string{"%%%", "e30", strings.Repeat("a", 600)} {
		if _, err := DecodeCursor(bad); err == nil {
			t.Errorf("cursor %q must be rejected", bad)
		}
	}
	if c, err := DecodeCursor(""); c != nil || err != nil {
		t.Fatal("empty cursor means first page")
	}
}

func TestHealthReadiness(t *testing.T) {
	fail := false
	h := NewHealth(Checker{Name: "db", Check: func(context.Context) error {
		if fail {
			return errors.New("down")
		}
		return nil
	}})
	mux := http.NewServeMux()
	h.Register(mux)
	get := func(path string) int {
		rec := httptest.NewRecorder()
		mux.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, path, nil))
		return rec.Code
	}
	if get("/health/live") != 200 || get("/health/ready") != 200 {
		t.Fatal("healthy service must be live and ready")
	}
	fail = true
	if get("/health/ready") != 503 || get("/health/live") != 200 {
		t.Fatal("dependency failure must only affect readiness")
	}
	fail = false
	h.SetDraining()
	if get("/health/ready") != 503 {
		t.Fatal("draining service must not be ready")
	}
}

func TestClientIPTrustsOnlyConfiguredProxies(t *testing.T) {
	_, proxyNet, _ := net.ParseCIDR("10.0.0.0/8")
	r := httptest.NewRequest(http.MethodGet, "/", nil)
	r.RemoteAddr = "203.0.113.9:1234"
	r.Header.Set("X-Forwarded-For", "1.2.3.4")
	if got := ClientIP(r, []*net.IPNet{proxyNet}); got != "203.0.113.9" {
		t.Fatalf("untrusted peer must not be able to spoof XFF, got %s", got)
	}
	r.RemoteAddr = "10.1.2.3:1234"
	if got := ClientIP(r, []*net.IPNet{proxyNet}); got != "1.2.3.4" {
		t.Fatalf("trusted proxy XFF must be honored, got %s", got)
	}
}
