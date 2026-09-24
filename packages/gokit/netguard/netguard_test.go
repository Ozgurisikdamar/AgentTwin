package netguard

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"net/netip"
	"net/url"
	"strings"
	"testing"
)

func mustURL(s string) *url.URL {
	u, err := url.Parse(s)
	if err != nil {
		panic(err)
	}
	return u
}

func TestCheckURL(t *testing.T) {
	p := Policy{AllowHTTP: true}
	blocked := []string{
		"ftp://example.com/x",
		"http://user:pass@example.com/",
		"http://127.0.0.1:8080/",
		"http://[::1]/",
		"http://169.254.169.254/latest/meta-data/",
		"http://10.0.0.5/",
		"http://192.168.1.1/",
		"http://172.20.0.3/",
		"http://[::ffff:127.0.0.1]/",
		"http://0.0.0.0/",
		"http://[fd00::1]/",
		"http:///nohost",
	}
	for _, s := range blocked {
		if err := p.CheckURL(mustURL(s)); !errors.Is(err, ErrBlocked) {
			t.Errorf("%s should be blocked, got %v", s, err)
		}
	}
	if err := p.CheckURL(mustURL("https://api.example.com/v1")); err != nil {
		t.Errorf("public https must pass: %v", err)
	}
	if err := (Policy{}).CheckURL(mustURL("http://api.example.com")); err == nil {
		t.Error("plain http must be refused unless allowed")
	}
}

func TestAllowlist(t *testing.T) {
	p := Policy{AllowedHosts: []string{"api.example.com", "*.payments.internal"}, AllowHTTP: true}
	for _, ok := range []string{"https://api.example.com/x", "http://eu.payments.internal/"} {
		if err := p.CheckURL(mustURL(ok)); err != nil {
			t.Errorf("%s should pass: %v", ok, err)
		}
	}
	for _, bad := range []string{"https://evil.com", "https://payments.internal.evil.com", "https://xapi.example.com"} {
		if err := p.CheckURL(mustURL(bad)); err == nil {
			t.Errorf("%s should be blocked", bad)
		}
	}
}

func TestPrivateHostExceptionDoesNotOpenMetadata(t *testing.T) {
	p := Policy{AllowHTTP: true, AllowedPrivateHosts: []string{"demo-tools"}}
	if err := p.checkAddr("demo-tools", netip.MustParseAddr("172.18.0.7")); err != nil {
		t.Fatalf("explicitly allowed private host must pass: %v", err)
	}
	if err := p.checkAddr("demo-tools", netip.MustParseAddr("169.254.169.254")); err == nil {
		t.Fatal("metadata address must stay blocked even for allowed private hosts")
	}
	if err := p.checkAddr("other", netip.MustParseAddr("172.18.0.7")); err == nil {
		t.Fatal("non-allowlisted host resolving to private address must be blocked")
	}
}

func TestDNSRebindingIsBlockedAtDialTime(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { _, _ = w.Write([]byte("secret")) }))
	defer srv.Close()
	port := srv.URL[strings.LastIndex(srv.URL, ":")+1:]
	// A public-looking name that resolves to loopback (rebinding attack).
	p := Policy{AllowHTTP: true, Resolver: func(context.Context, string) ([]netip.Addr, error) {
		return []netip.Addr{netip.MustParseAddr("127.0.0.1")}, nil
	}}
	_, err := p.Client().Get("http://rebind.attacker.example:" + port + "/")
	if err == nil || !strings.Contains(err.Error(), "egress blocked") {
		t.Fatalf("rebinding to loopback must be blocked, got %v", err)
	}
	// With loopback allowed (tests), the same request succeeds.
	p.AllowLoopback = true
	resp, err := p.Client().Get("http://rebind.attacker.example:" + port + "/")
	if err != nil {
		t.Fatal(err)
	}
	_ = resp.Body.Close()
}

func TestRedirectToPrivateIsBlocked(t *testing.T) {
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { _, _ = w.Write([]byte("internal")) }))
	defer target.Close()
	redirector := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, "http://169.254.169.254/latest/meta-data/", http.StatusFound)
	}))
	defer redirector.Close()
	p := Policy{AllowHTTP: true, AllowLoopback: true, MaxRedirects: 3}
	_, err := p.Client().Get(redirector.URL)
	if err == nil || !strings.Contains(err.Error(), "egress blocked") {
		t.Fatalf("redirect to metadata must be blocked, got %v", err)
	}
}

func TestReadLimited(t *testing.T) {
	if _, err := ReadLimited(strings.NewReader("12345"), 4); err == nil {
		t.Fatal("oversized body must error")
	}
	b, err := ReadLimited(strings.NewReader("1234"), 4)
	if err != nil || string(b) != "1234" {
		t.Fatalf("got %q %v", b, err)
	}
}

func FuzzCheckURLNeverPanics(f *testing.F) {
	f.Add("http://127.0.0.1/")
	f.Add("https://[::ffff:10.0.0.1]:443/x")
	f.Add("http://%zz")
	p := Policy{AllowHTTP: true, AllowedHosts: []string{"*.example.com"}}
	f.Fuzz(func(t *testing.T, s string) {
		u, err := url.Parse(s)
		if err != nil {
			return
		}
		_ = p.CheckURL(u)
	})
}
