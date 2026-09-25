// Package server assembles the control-plane HTTP surface (public API behind
// authentication, internal API behind internal JWTs) so that the binary and
// the integration tests run exactly the same stack.
package server

import (
	"context"
	"log/slog"
	"net"
	"net/http"
	"strings"
	"time"

	"github.com/coreos/go-oidc/v3/oidc"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/authn"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/config"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/httpx"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/svcclient"
	"github.com/Ozgurisikdamar/AgentTwin/services/control-plane/internal/api"
	"github.com/Ozgurisikdamar/AgentTwin/services/control-plane/internal/app"
	"github.com/Ozgurisikdamar/AgentTwin/services/control-plane/internal/auth"
	"github.com/Ozgurisikdamar/AgentTwin/services/control-plane/internal/proxy"
	"github.com/Ozgurisikdamar/AgentTwin/services/control-plane/internal/store"
)

// Config is the control-plane specific configuration.
type Config struct {
	AuthMode       auth.Mode
	SessionSecret  string
	Pepper         string
	DemoAPIKey     string
	DemoBootstrap  bool
	OIDCIssuer     string
	OIDCClientID   string
	OIDCAudience   string
	Targets        map[string]string
	RateRPS        float64
	RateBurst      int
	IPRateRPS      float64
	IPRateBurst    int
	TrustedProxies []*net.IPNet
	// OIDCVerifier overrides OIDC discovery (tests).
	OIDCVerifier *oidc.IDTokenVerifier
	Now          func() time.Time
}

// LoadConfig reads control-plane variables; problems accumulate in l.
func LoadConfig(l *config.Loader) Config {
	var c Config
	c.AuthMode = auth.Mode(l.OneOf("AUTH_MODE", "dev", "dev", "oidc"))
	l.Check(l.Env() != config.Production || c.AuthMode != auth.ModeDev, "AUTH_MODE",
		"dev authentication is refused when APP_ENV=production; configure OIDC")
	c.SessionSecret = l.Secret("AGENTTWIN_SESSION_SECRET", 32)
	c.Pepper = l.Secret("AGENTTWIN_API_KEY_PEPPER", 32)
	c.DemoAPIKey = l.String("AGENTTWIN_DEMO_API_KEY", "")
	c.DemoBootstrap = l.Bool("DEMO_BOOTSTRAP", c.AuthMode == auth.ModeDev)
	l.Check(l.Env() != config.Production || !c.DemoBootstrap, "DEMO_BOOTSTRAP",
		"demo users and keys are refused when APP_ENV=production")
	c.OIDCIssuer = l.String("OIDC_ISSUER_URL", "")
	c.OIDCClientID = l.String("OIDC_CLIENT_ID", "")
	c.OIDCAudience = l.String("OIDC_AUDIENCE", "")
	l.Check(c.AuthMode != auth.ModeOIDC || (c.OIDCIssuer != "" && c.OIDCClientID != ""), "OIDC_ISSUER_URL",
		"AUTH_MODE=oidc requires OIDC_ISSUER_URL and OIDC_CLIENT_ID")
	c.Targets = map[string]string{
		"trace-service":      l.String("TRACE_SERVICE_URL", ""),
		"graph-service":      l.String("GRAPH_SERVICE_URL", ""),
		"evaluation-service": l.String("EVALUATION_SERVICE_URL", ""),
		"simulation-service": l.String("SIMULATION_SERVICE_URL", ""),
		"runtime-gateway":    l.String("RUNTIME_GATEWAY_URL", ""),
	}
	c.RateRPS = l.Float("RATE_LIMIT_RPS", 50, 1, 100000)
	c.RateBurst = l.Int("RATE_LIMIT_BURST", 200, 1, 100000)
	c.IPRateRPS = l.Float("IP_RATE_LIMIT_RPS", 100, 1, 100000)
	c.IPRateBurst = l.Int("IP_RATE_LIMIT_BURST", 400, 1, 100000)
	for _, cidr := range l.List("TRUSTED_PROXIES", nil) {
		_, n, err := net.ParseCIDR(strings.TrimSpace(cidr))
		l.Check(err == nil, "TRUSTED_PROXIES", "must be a comma-separated list of CIDRs")
		if err == nil {
			c.TrustedProxies = append(c.TrustedProxies, n)
		}
	}
	return c
}

// impactTimeout bounds each call a change impact makes.
const impactTimeout = 20 * time.Second

// Server is the assembled control plane.
type Server struct {
	App     *app.App
	Store   *store.Store
	Auth    *auth.Authenticator
	Proxy   *proxy.Proxy
	Demo    app.DemoIdentity
	Handler http.Handler
}

// New wires the control plane on top of an open pool.
func New(ctx context.Context, pool *pgxpool.Pool, tokens *authn.TokenService, log *slog.Logger, c Config) (*Server, error) {
	now := c.Now
	if now == nil {
		now = time.Now
	}
	st := store.New(pool)
	authenticator, err := auth.New(ctx, auth.Config{
		Mode: c.AuthMode, SessionSecret: c.SessionSecret, Pepper: c.Pepper,
		OIDCIssuer: c.OIDCIssuer, OIDCClientID: c.OIDCClientID, OIDCVerifier: c.OIDCVerifier,
	}, st)
	if err != nil {
		return nil, err
	}
	a := &app.App{Store: st, Auth: authenticator, Pepper: c.Pepper, Log: log, Now: now}
	// The services a change set's impact asks (spec §22) and the one a
	// release's gate reads its run from (spec §28); a missing URL makes the
	// impact or the gate incomplete, not the control plane unavailable.
	for name, client := range map[string]**svcclient.Client{
		"graph-service": &a.Graph, "simulation-service": &a.Simulation, "evaluation-service": &a.Evaluation,
	} {
		if url := c.Targets[name]; url != "" {
			if *client, err = svcclient.New(name, url, tokens, impactTimeout); err != nil {
				return nil, err
			}
		}
	}
	s := &Server{App: a, Store: st, Auth: authenticator}

	if c.DemoBootstrap {
		s.Demo, err = a.BootstrapDemo(ctx, c.DemoAPIKey)
		if err != nil {
			return nil, err
		}
		log.Info("demo organization ready", "organization_id", s.Demo.OrganizationID, "project_id", s.Demo.ProjectID)
	}

	s.Proxy, err = proxy.New(tokens, c.Targets, st.ProjectIDs, log)
	if err != nil {
		return nil, err
	}
	apiSrv := &api.Server{App: a, Auth: authenticator, Tokens: tokens, Proxy: s.Proxy, Log: log, AuthMode: c.AuthMode,
		OIDC: map[string]string{"issuer": c.OIDCIssuer, "client_id": c.OIDCClientID, "audience": c.OIDCAudience}}

	if c.IPRateRPS == 0 {
		c.IPRateRPS, c.IPRateBurst = 100, 400
	}
	if c.RateRPS == 0 {
		c.RateRPS, c.RateBurst = 50, 200
	}
	ipLimiter := httpx.NewRateLimiter(c.IPRateRPS, c.IPRateBurst)
	principalLimiter := httpx.NewRateLimiter(c.RateRPS, c.RateBurst)
	clientIP := func(r *http.Request) string { return "ip:" + httpx.ClientIP(r, c.TrustedProxies) }

	public := http.NewServeMux()
	apiSrv.PublicRoutes(public)
	publicHandler := httpx.Chain(public,
		// Coarse per-IP limit before authentication bounds credential guessing.
		httpx.RateLimit(ipLimiter, clientIP),
		authenticator.Middleware(api.IsPublic),
		httpx.RateLimit(principalLimiter, func(r *http.Request) string {
			if p, ok := authn.FromContext(r.Context()); ok {
				return p.OrgID + "/" + p.Actor
			}
			return clientIP(r)
		}),
		api.Idempotency(st),
	)

	internal := http.NewServeMux()
	apiSrv.InternalRoutes(internal)
	internalHandler := httpx.Chain(internal, authn.RequireInternal(tokens, "control-plane"))

	// Agents call their tools here (ADR-0033): authenticated and rate limited
	// like the API, without the edge's Idempotency-Key replay (the key is the
	// tool call's, enforced by the gateway).
	gatewayHandler := httpx.Chain(s.Proxy,
		httpx.RateLimit(ipLimiter, clientIP),
		authenticator.Middleware(nil),
		httpx.RateLimit(principalLimiter, func(r *http.Request) string {
			p, _ := authn.FromContext(r.Context())
			return p.OrgID + "/" + p.Actor
		}),
	)

	root := http.NewServeMux()
	root.Handle("/api/", publicHandler)
	root.Handle(proxy.GatewayPrefix+"/", gatewayHandler)
	root.Handle("/internal/", internalHandler)
	s.Handler = root
	return s, nil
}
