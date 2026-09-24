// Package netguard builds outbound HTTP clients that resist SSRF: scheme and
// host allowlists, blocking of loopback/private/link-local/metadata ranges,
// dial-time IP checks (DNS-rebinding defense), redirect re-validation,
// timeouts and response size caps.
package netguard

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/netip"
	"net/url"
	"strings"
	"time"
)

// ErrBlocked is returned when a destination violates the egress policy.
var ErrBlocked = errors.New("egress blocked")

// Policy is an egress policy.
type Policy struct {
	// AllowedHosts restricts destinations. Entries are exact hosts or
	// "*.example.com" suffix patterns. Empty means any public host.
	AllowedHosts []string
	// AllowedPrivateHosts may resolve to private addresses (internal services
	// such as docker-compose hostnames). Loopback and link-local (cloud
	// metadata) stay blocked unless AllowLoopback is set.
	AllowedPrivateHosts []string
	// AllowLoopback permits 127.0.0.0/8 and ::1 (tests only).
	AllowLoopback bool
	AllowHTTP     bool
	Timeout       time.Duration
	MaxRedirects  int
	// Resolver overrides DNS resolution (tests).
	Resolver func(ctx context.Context, host string) ([]netip.Addr, error)
}

var blockedPrefixes = mustPrefixes(
	"0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
	"172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24", "192.168.0.0/16", "198.18.0.0/15",
	"198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4", "255.255.255.255/32",
	"::/128", "::1/128", "fc00::/7", "fe80::/10", "ff00::/8", "64:ff9b::/96", "2001:db8::/32",
)

var alwaysBlocked = mustPrefixes("169.254.0.0/16", "fe80::/10", "0.0.0.0/8", "::/128", "224.0.0.0/4", "ff00::/8", "255.255.255.255/32")
var loopback = mustPrefixes("127.0.0.0/8", "::1/128")

func mustPrefixes(ss ...string) []netip.Prefix {
	out := make([]netip.Prefix, 0, len(ss))
	for _, s := range ss {
		out = append(out, netip.MustParsePrefix(s))
	}
	return out
}

func inAny(a netip.Addr, ps []netip.Prefix) bool {
	a = a.Unmap()
	for _, p := range ps {
		if p.Contains(a) {
			return true
		}
	}
	return false
}

func hostMatches(host string, patterns []string) bool {
	host = strings.ToLower(strings.TrimSuffix(host, "."))
	for _, p := range patterns {
		p = strings.ToLower(p)
		if strings.HasPrefix(p, "*.") {
			if strings.HasSuffix(host, p[1:]) && len(host) > len(p)-1 {
				return true
			}
		} else if host == p {
			return true
		}
	}
	return false
}

// CheckURL validates a destination URL before any network activity.
func (p Policy) CheckURL(u *url.URL) error {
	if u == nil {
		return fmt.Errorf("%w: empty url", ErrBlocked)
	}
	switch u.Scheme {
	case "https":
	case "http":
		if !p.AllowHTTP {
			return fmt.Errorf("%w: plain http is not allowed", ErrBlocked)
		}
	default:
		return fmt.Errorf("%w: scheme %q not allowed", ErrBlocked, u.Scheme)
	}
	if u.User != nil {
		return fmt.Errorf("%w: credentials in url are not allowed", ErrBlocked)
	}
	host := u.Hostname()
	if host == "" {
		return fmt.Errorf("%w: missing host", ErrBlocked)
	}
	if len(p.AllowedHosts) > 0 && !hostMatches(host, p.AllowedHosts) && !hostMatches(host, p.AllowedPrivateHosts) {
		return fmt.Errorf("%w: host %q is not in the allowlist", ErrBlocked, host)
	}
	if addr, err := netip.ParseAddr(host); err == nil {
		if err := p.checkAddr(host, addr); err != nil {
			return err
		}
	}
	return nil
}

func (p Policy) checkAddr(host string, a netip.Addr) error {
	a = a.Unmap()
	if inAny(a, loopback) {
		if p.AllowLoopback {
			return nil
		}
		return fmt.Errorf("%w: loopback address %s", ErrBlocked, a)
	}
	if inAny(a, alwaysBlocked) {
		return fmt.Errorf("%w: address %s is never reachable (link-local/metadata/multicast)", ErrBlocked, a)
	}
	if inAny(a, blockedPrefixes) && !hostMatches(host, p.AllowedPrivateHosts) {
		return fmt.Errorf("%w: private address %s for host %q", ErrBlocked, a, host)
	}
	return nil
}

func (p Policy) resolve(ctx context.Context, host string) ([]netip.Addr, error) {
	if p.Resolver != nil {
		return p.Resolver(ctx, host)
	}
	return net.DefaultResolver.LookupNetIP(ctx, "ip", host)
}

// DialContext resolves the host itself and dials a checked IP, so the address
// that was validated is the address that is connected (no DNS rebinding gap).
func (p Policy) DialContext(ctx context.Context, network, address string) (net.Conn, error) {
	host, port, err := net.SplitHostPort(address)
	if err != nil {
		return nil, err
	}
	var candidates []netip.Addr
	if a, err := netip.ParseAddr(host); err == nil {
		candidates = []netip.Addr{a}
	} else {
		candidates, err = p.resolve(ctx, host)
		if err != nil {
			return nil, fmt.Errorf("resolve %s: %w", host, err)
		}
	}
	lastErr := fmt.Errorf("%w: no addresses for %s", ErrBlocked, host)
	d := net.Dialer{Timeout: 5 * time.Second}
	for _, a := range candidates {
		if err := p.checkAddr(host, a); err != nil {
			lastErr = err
			continue
		}
		conn, err := d.DialContext(ctx, network, net.JoinHostPort(a.Unmap().String(), port))
		if err == nil {
			return conn, nil
		}
		lastErr = err
	}
	return nil, lastErr
}

// Client returns an HTTP client enforcing the policy. It never uses
// environment proxies (a proxy would bypass the dial-time checks).
func (p Policy) Client() *http.Client {
	timeout := p.Timeout
	if timeout <= 0 {
		timeout = 10 * time.Second
	}
	maxRedirects := p.MaxRedirects
	tr := &http.Transport{
		Proxy:                 nil,
		DialContext:           p.DialContext,
		ForceAttemptHTTP2:     true,
		MaxIdleConns:          50,
		IdleConnTimeout:       60 * time.Second,
		TLSHandshakeTimeout:   5 * time.Second,
		ResponseHeaderTimeout: timeout,
	}
	return &http.Client{
		Timeout:   timeout,
		Transport: tr,
		CheckRedirect: func(req *http.Request, via []*http.Request) error {
			if len(via) > maxRedirects {
				return fmt.Errorf("%w: too many redirects", ErrBlocked)
			}
			return p.CheckURL(req.URL)
		},
	}
}

// ReadLimited reads at most max bytes from r; larger bodies are an error, not
// silently truncated data.
func ReadLimited(r io.Reader, max int64) ([]byte, error) {
	b, err := io.ReadAll(io.LimitReader(r, max+1))
	if err != nil {
		return nil, err
	}
	if int64(len(b)) > max {
		return nil, fmt.Errorf("response exceeds %d bytes", max)
	}
	return b, nil
}
