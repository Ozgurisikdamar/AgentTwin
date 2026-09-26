package service

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	promtest "github.com/prometheus/client_golang/prometheus/testutil"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/db"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/httpx"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/testutil"
)

// A chaos test (spec §64): PostgreSQL goes away under a running service.
// Every Go service uses this runtime, its pool and its error rendering.
func TestADatabaseOutageAnswers503AndRecovers(t *testing.T) {
	raw := testutil.NewDatabase(t)
	u, err := url.Parse(raw)
	if err != nil {
		t.Fatal(err)
	}
	proxy := testutil.NewCutProxy(t, u.Host)
	ctx := context.Background()
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	rt, err := Start(ctx, Spec{Name: "chaos", Schema: "chaos"}, Config{
		Name: "chaos", DatabaseURL: proxy.URL(t, raw), InternalSecret: strings.Repeat("s", 32), DBMaxConns: 4,
	}, log)
	if err != nil {
		t.Fatal(err)
	}
	defer rt.Close(ctx)
	if _, err := rt.Pool.Exec(ctx, "CREATE TABLE public.note (id serial PRIMARY KEY, body text NOT NULL)"); err != nil {
		t.Fatal(err)
	}

	app := http.NewServeMux()
	app.Handle("GET /notes", httpx.Handle(func(w http.ResponseWriter, r *http.Request) error {
		var n int
		if err := rt.Pool.QueryRow(r.Context(), "SELECT count(*) FROM public.note").Scan(&n); err != nil {
			return err
		}
		httpx.WriteJSON(w, http.StatusOK, map[string]int{"notes": n})
		return nil
	}))
	app.Handle("POST /notes", httpx.Handle(func(w http.ResponseWriter, r *http.Request) error {
		if err := db.WithTx(r.Context(), rt.Pool, func(tx pgx.Tx) error {
			_, err := tx.Exec(r.Context(), "INSERT INTO public.note (body) VALUES ('n')")
			return err
		}); err != nil {
			return err
		}
		w.WriteHeader(http.StatusCreated)
		return nil
	}))
	srv := httptest.NewServer(rt.Handler(app))
	defer srv.Close()

	type answer struct {
		status     int
		code       string
		retryAfter string
		body       string
	}
	call := func(method, path string) answer {
		req, _ := http.NewRequestWithContext(ctx, method, srv.URL+path, nil)
		res, err := srv.Client().Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer func() { _ = res.Body.Close() }()
		b, _ := io.ReadAll(res.Body)
		var e struct {
			Error struct {
				Code string `json:"code"`
			} `json:"error"`
		}
		_ = json.Unmarshal(b, &e)
		return answer{res.StatusCode, e.Error.Code, res.Header.Get("Retry-After"), string(b)}
	}
	for _, m := range []string{"POST", "GET"} {
		if a := call(m, "/notes"); a.status >= 300 {
			t.Fatalf("%s /notes before the outage: %+v", m, a)
		}
	}

	// PostgreSQL goes away: requests that need it answer 503 with a retry
	// hint (not 500, not a hang), and readiness reports which dependency.
	proxy.Cut()
	for i, m := range []string{"GET", "POST", "GET", "POST"} {
		start := time.Now()
		a := call(m, "/notes")
		if a.status != http.StatusServiceUnavailable || a.code != "UNAVAILABLE" || a.retryAfter == "" {
			t.Fatalf("request %d (%s) during the outage: %+v", i, m, a)
		}
		if strings.Contains(a.body, "EOF") || strings.Contains(a.body, "connect") {
			t.Fatalf("the cause leaked: %s", a.body)
		}
		if took := time.Since(start); took > 6*time.Second {
			t.Fatalf("request %d took %v during the outage", i, took)
		}
	}
	if a := call("GET", "/health/ready"); a.status != http.StatusServiceUnavailable || !strings.Contains(a.body, `"postgres":"unavailable"`) {
		t.Fatalf("readiness during the outage: %+v", a)
	}
	if a := call("GET", "/health/live"); a.status != http.StatusOK {
		t.Fatalf("liveness must not depend on the database: %+v", a)
	}
	if n := promtest.ToFloat64(rt.Tel.HTTPRequests.WithLabelValues("GET", "GET /notes", "503")); n != 2 {
		t.Fatalf("503s counted for GET /notes = %v, want 2", n)
	}

	// It comes back: the pool reconnects on its own and requests succeed again.
	proxy.Restore()
	deadline := time.Now().Add(15 * time.Second)
	for call("GET", "/notes").status != http.StatusOK {
		if time.Now().After(deadline) {
			t.Fatal("the service did not recover after the database came back")
		}
		time.Sleep(100 * time.Millisecond)
	}
	for i := 0; i < 20; i++ {
		m := []string{"GET", "POST"}[i%2]
		if a := call(m, "/notes"); a.status >= 300 {
			t.Fatalf("request %d (%s) after recovery: %+v", i, m, a)
		}
	}
	if a := call("GET", "/health/ready"); a.status != http.StatusOK {
		t.Fatalf("readiness after recovery: %+v", a)
	}
	// Nothing written during the outage was half-applied: only the writes
	// that were answered 201 exist.
	var notes int
	if err := rt.Pool.QueryRow(ctx, "SELECT count(*) FROM public.note").Scan(&notes); err != nil {
		t.Fatal(err)
	}
	if notes != 1+10 {
		t.Fatalf("notes = %d, want 11 (one before, ten after, none during)", notes)
	}
}
