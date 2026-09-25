// Package proxy forwards authenticated public API requests to the owning
// internal service with a short-lived internal JWT (ADR-0009).
package proxy

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"net/http/httputil"
	"net/url"
	"sort"
	"strings"
	"time"

	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/authn"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/httpx"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/logx"
)

// Route maps a public path prefix to an internal service.
type Route struct {
	Prefix  string
	Service string
}

// Routes is the ownership table of the public API.
var Routes = []Route{
	{"/api/v1/traces", "trace-service"},
	{"/api/v1/trace-stats", "trace-service"},
	{"/api/v1/graph", "graph-service"},
	{"/api/v1/blast-radius", "graph-service"},
	{"/api/v1/datasets", "evaluation-service"},
	{"/api/v1/cases", "evaluation-service"},
	{"/api/v1/eval-runs", "evaluation-service"},
	{"/api/v1/regressions", "evaluation-service"},
	{"/api/v1/reviews", "evaluation-service"},
	{"/api/v1/evaluators", "evaluation-service"},
	{"/api/v1/judges", "evaluation-service"},
	{"/api/v1/scenarios", "simulation-service"},
	{"/api/v1/simulations", "simulation-service"},
	{"/api/v1/twins", "simulation-service"},
	{"/api/v1/policies", "runtime-gateway"},
	{"/api/v1/approvals", "runtime-gateway"},
	{"/api/v1/policy-decisions", "runtime-gateway"},
	{"/api/v1/tool-endpoints", "runtime-gateway"},
	{GatewayPrefix, "runtime-gateway"},
}

// GatewayPrefix is where agents call their tools through the runtime gateway
// (ADR-0033). It is served beside /api, not under it: the edge's
// Idempotency-Key replay does not apply there, because the key belongs to the
// tool call — the gateway enforces it and forwards it to the tool.
const GatewayPrefix = "/gateway/v1"

// OrgProjects returns the ids of an organization's projects.
type OrgProjects func(ctx context.Context, orgID string) ([]string, error)

// Proxy forwards requests to services.
type Proxy struct {
	tokens   *authn.TokenService
	projects OrgProjects
	targets  map[string]*url.URL
	proxies  map[string]*httputil.ReverseProxy
	log      *slog.Logger
	maxBody  int64
	routes   []Route
}

type tokenKey struct{}

// New builds a proxy. targets maps service name to base URL; services without a
// URL answer 503 with an actionable message. projects lists an organization's
// projects: a person may act in every project of their organization, and the
// internal token names them (a service cannot tell which organization a
// project id belongs to).
func New(tokens *authn.TokenService, targets map[string]string, projects OrgProjects, log *slog.Logger) (*Proxy, error) {
	if projects == nil {
		return nil, errors.New("proxy: the organization's projects are required")
	}
	p := &Proxy{tokens: tokens, projects: projects, targets: map[string]*url.URL{}, proxies: map[string]*httputil.ReverseProxy{}, log: log, maxBody: 16 << 20}
	transport := otelhttp.NewTransport(&http.Transport{
		Proxy:                 nil,
		MaxIdleConnsPerHost:   32,
		IdleConnTimeout:       90 * time.Second,
		ResponseHeaderTimeout: 60 * time.Second,
	})
	for name, raw := range targets {
		if raw == "" {
			continue
		}
		u, err := url.Parse(raw)
		if err != nil || u.Scheme == "" || u.Host == "" {
			return nil, fmt.Errorf("invalid URL for %s: %q", name, raw)
		}
		p.targets[name] = u
		service := name
		target := u
		p.proxies[name] = &httputil.ReverseProxy{
			Transport:     transport,
			FlushInterval: -1, // stream SSE progress immediately
			Rewrite: func(pr *httputil.ProxyRequest) {
				pr.SetURL(target)
				pr.Out.Host = target.Host
				// Client credentials never travel to internal services.
				for _, h := range []string{"Authorization", "Cookie", "X-Agenttwin-Api-Key", "X-Agenttwin-Org"} {
					pr.Out.Header.Del(h)
				}
				// The edge answers an API request's Idempotency-Key itself; a
				// tool call's key is the gateway's to enforce.
				if !isGatewayPath(pr.In.URL.Path) {
					pr.Out.Header.Del("Idempotency-Key")
				}
				pr.SetXForwarded()
				if tok, ok := pr.In.Context().Value(tokenKey{}).(string); ok {
					pr.Out.Header.Set("Authorization", "Bearer "+tok)
				}
				if rid := logx.RequestID(pr.In.Context()); rid != "" {
					pr.Out.Header.Set(httpx.RequestIDHeader, rid)
				}
			},
			ModifyResponse: func(resp *http.Response) error {
				resp.Header.Del("Server")
				// The edge sets these itself (request id, security headers);
				// copied from the upstream too, every one would appear twice.
				for _, h := range httpx.OwnedResponseHeaders() {
					resp.Header.Del(h)
				}
				return nil
			},
			ErrorHandler: func(w http.ResponseWriter, r *http.Request, err error) {
				log.WarnContext(r.Context(), "upstream error", "service", service, "error", err.Error())
				httpx.WriteError(w, r, httpx.NewError(http.StatusBadGateway, "UPSTREAM_UNAVAILABLE",
					fmt.Sprintf("The %s is not reachable right now. Check `make doctor` or the service logs, then retry.", service)).
					WithDetails(map[string]any{"service": service}))
			},
		}
	}
	p.routes = append([]Route(nil), Routes...)
	sort.Slice(p.routes, func(i, j int) bool { return len(p.routes[i].Prefix) > len(p.routes[j].Prefix) })
	return p, nil
}

func isGatewayPath(path string) bool { return strings.HasPrefix(path, GatewayPrefix+"/") }

// Match returns the owning service for a path.
func (p *Proxy) Match(path string) (string, bool) {
	for _, r := range p.routes {
		if path == r.Prefix || strings.HasPrefix(path, r.Prefix+"/") {
			return r.Service, true
		}
	}
	return "", false
}

// ServeHTTP forwards the request. The principal must already be in context.
func (p *Proxy) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	service, ok := p.Match(r.URL.Path)
	if !ok {
		httpx.WriteError(w, r, httpx.ErrNotFound)
		return
	}
	principal, ok := authn.FromContext(r.Context())
	if !ok {
		httpx.WriteError(w, r, httpx.ErrUnauthorized)
		return
	}
	rp, ok := p.proxies[service]
	if !ok {
		httpx.WriteError(w, r, httpx.NewError(http.StatusServiceUnavailable, "SERVICE_NOT_CONFIGURED",
			fmt.Sprintf("The %s is not configured on this control plane (set %s_URL).", service, strings.ToUpper(strings.ReplaceAll(service, "-", "_")))))
		return
	}
	if principal.AllProjects {
		// "Every project" means every project of this organization: named,
		// so that a project id of another organization is not one of them.
		ids, err := p.projects(r.Context(), principal.OrgID)
		if err != nil {
			p.log.ErrorContext(r.Context(), "listing the organization's projects failed", "error", err.Error())
			httpx.WriteError(w, r, httpx.NewError(http.StatusServiceUnavailable, "UNAVAILABLE",
				"The control plane cannot read its projects right now. Retry shortly."))
			return
		}
		principal.ProjectIDs, principal.AllProjects = ids, false
	}
	tok, err := p.tokens.Mint(principal, service, logx.RequestID(r.Context()))
	if err != nil {
		httpx.WriteError(w, r, err)
		return
	}
	r = r.WithContext(context.WithValue(authn.WithPrincipal(r.Context(), principal), tokenKey{}, tok))
	httpx.SetRouteName(r, "proxy "+service)
	r.Body = http.MaxBytesReader(w, r.Body, p.maxBody)
	rp.ServeHTTP(w, r)
}

// Configured reports the services with a target URL (doctor/readiness).
func (p *Proxy) Configured() map[string]string {
	out := map[string]string{}
	for k, v := range p.targets {
		out[k] = v.String()
	}
	return out
}
