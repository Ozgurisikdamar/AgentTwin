package service

import (
	"bytes"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strconv"
	"testing"
)

func TestHealthcheckProbesTheLocalReadinessEndpoint(t *testing.T) {
	ready := true
	var paths []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		paths = append(paths, r.URL.Path)
		if r.URL.Path == "/health/ready" && !ready {
			w.WriteHeader(http.StatusServiceUnavailable)
			return
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()
	u, _ := url.Parse(srv.URL)
	port := u.Port()
	var stderr bytes.Buffer

	if code := Healthcheck(port, 1, nil, &stderr); code != 0 {
		t.Fatalf("ready service: exit %d (%s)", code, stderr.String())
	}
	ready = false
	if code := Healthcheck(port, 1, nil, &stderr); code != 1 {
		t.Fatalf("unready service: exit %d, want 1", code)
	}
	// Liveness does not depend on dependencies.
	if code := Healthcheck(port, 1, []string{"live"}, &stderr); code != 0 {
		t.Fatalf("live probe: exit %d", code)
	}
	if got := paths; len(got) != 3 || got[0] != "/health/ready" || got[2] != "/health/live" {
		t.Fatalf("probed paths %v", got)
	}
	// The default port applies when PORT is unset.
	p, _ := strconv.Atoi(port)
	ready = true
	if code := Healthcheck("", p, nil, &stderr); code != 0 {
		t.Fatalf("default port: exit %d", code)
	}
}

func TestHealthcheckRejectsBadInputAndDeadProcess(t *testing.T) {
	var stderr bytes.Buffer
	for _, port := range []string{"abc", "0", "70000"} {
		if code := Healthcheck(port, 8080, nil, &stderr); code != 2 {
			t.Errorf("PORT=%q: exit %d, want 2", port, code)
		}
	}
	if code := Healthcheck("8080", 8080, []string{"bogus"}, &stderr); code != 2 {
		t.Errorf("unknown probe: exit %d, want 2", code)
	}
	// Reserve a port, close it, and probe: nothing listens there.
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	port := strconv.Itoa(l.Addr().(*net.TCPAddr).Port)
	_ = l.Close()
	if code := Healthcheck(port, 1, nil, &stderr); code != 1 {
		t.Errorf("dead process: exit %d, want 1", code)
	}
}
