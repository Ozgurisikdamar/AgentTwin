// Command runtime-gateway contains what agents do in production (ADR-0033):
// every tool call an agent routes through it is decided by the project's
// active policies, recorded, and forwarded only when allowed or approved.
package main

import (
	"context"
	"net/http"
	"os"
	"time"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/config"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/netguard"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/service"
	"github.com/Ozgurisikdamar/AgentTwin/services/runtime-gateway/internal/api"
	"github.com/Ozgurisikdamar/AgentTwin/services/runtime-gateway/internal/store"
	"github.com/Ozgurisikdamar/AgentTwin/services/runtime-gateway/migrations"
)

// maxToolTimeout is the longest a tool endpoint may be given (the API caps
// timeout_ms at 60000); the client's own timeout sits just above it so the
// per-call deadline is the one that fires.
const maxToolTimeout = 65 * time.Second

func main() {
	var egress netguard.Policy
	os.Exit(service.Main(service.Spec{
		Name:        "runtime-gateway",
		DefaultPort: 8086,
		Schema:      migrations.Schema,
		Migrations:  migrations.FS,
		Configure: func(l *config.Loader) {
			egress = egressPolicy(l)
		},
		Build: func(ctx context.Context, rt *service.Runtime) (http.Handler, error) {
			st := store.New(rt.Pool)
			srv := &api.Server{Store: st, Tokens: rt.Tokens, Log: rt.Log, Egress: egress, Client: egress.Client(),
				Metrics: api.NewMetrics(rt.Tel.Registry)}
			rt.Relay(ctx, migrations.Schema)
			rt.Go(ctx, "approvals-gauge", func(ctx context.Context) error {
				t := time.NewTicker(30 * time.Second)
				defer t.Stop()
				for {
					if n, err := st.CountPending(ctx, time.Now().UTC()); err == nil {
						srv.Metrics.SetPending(n)
					}
					select {
					case <-ctx.Done():
						return nil
					case <-t.C:
					}
				}
			})
			rt.Go(ctx, "retention", func(ctx context.Context) error {
				t := time.NewTicker(10 * time.Minute)
				defer t.Stop()
				for {
					if n, err := st.PurgeExpired(ctx, time.Now().UTC()); err != nil {
						rt.Log.WarnContext(ctx, "purging expired idempotency records failed", "error", err.Error())
					} else if n > 0 {
						rt.Log.InfoContext(ctx, "purged expired runtime records", "rows", n)
					}
					select {
					case <-ctx.Done():
						return nil
					case <-t.C:
					}
				}
			})
			mux := http.NewServeMux()
			srv.Routes(mux)
			return mux, nil
		},
	}))
}

// egressPolicy is where tools may be: public HTTPS hosts by default,
// narrowed by RUNTIME_EGRESS_ALLOWED_HOSTS; internal hosts (compose service
// names) only when listed in RUNTIME_EGRESS_PRIVATE_HOSTS. Plain HTTP is
// refused in production. Redirects are never followed: a tool that answers
// with one is misconfigured, and following it would leave the checked host.
func egressPolicy(l *config.Loader) netguard.Policy {
	p := netguard.Policy{
		AllowedHosts:        l.List("RUNTIME_EGRESS_ALLOWED_HOSTS", nil),
		AllowedPrivateHosts: l.List("RUNTIME_EGRESS_PRIVATE_HOSTS", nil),
		AllowHTTP:           l.Bool("RUNTIME_EGRESS_ALLOW_HTTP", l.Env() != config.Production),
		Timeout:             maxToolTimeout,
		MaxRedirects:        0,
	}
	l.Check(l.Env() != config.Production || !p.AllowHTTP, "RUNTIME_EGRESS_ALLOW_HTTP",
		"plain HTTP to tools is refused when APP_ENV=production")
	for _, h := range append(append([]string(nil), p.AllowedHosts...), p.AllowedPrivateHosts...) {
		l.Check(h != "*" && h != "*.", "RUNTIME_EGRESS_ALLOWED_HOSTS",
			"a bare wildcard is not a host; leave the list empty to allow any public host")
	}
	return p
}
