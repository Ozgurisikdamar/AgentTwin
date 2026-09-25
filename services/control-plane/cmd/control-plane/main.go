// Command control-plane is the AgentTwin edge: authentication, organizations,
// projects, agent/tool registries, API keys, audit, release gate, and the
// authenticated reverse proxy to internal services.
package main

import (
	"context"
	"net/http"
	"os"
	"time"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/config"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/events"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/service"
	"github.com/Ozgurisikdamar/AgentTwin/services/control-plane/internal/server"
	"github.com/Ozgurisikdamar/AgentTwin/services/control-plane/migrations"
)

func main() {
	var c server.Config
	os.Exit(service.Main(service.Spec{
		Name:        "control-plane",
		DefaultPort: 8080,
		Schema:      migrations.Schema,
		Migrations:  migrations.FS,
		Configure:   func(l *config.Loader) { c = server.LoadConfig(l) },
		Build: func(ctx context.Context, rt *service.Runtime) (http.Handler, error) {
			s, err := server.New(ctx, rt.Pool, rt.Tokens, rt.Log, c)
			if err != nil {
				return nil, err
			}
			rt.Relay(ctx, migrations.Schema)
			rt.Consume(ctx, "control-plane.events", 4, map[string]events.Handler{
				"audit.recorded.v1":           s.App.HandleAuditEvent,
				"evaluation.run_completed.v1": s.App.HandleEvaluationCompleted,
			})
			rt.Go(ctx, "idempotency-purge", func(ctx context.Context) error {
				t := time.NewTicker(time.Hour)
				defer t.Stop()
				for {
					if n, err := s.Store.PurgeIdempotency(ctx); err == nil && n > 0 {
						rt.Log.Info("purged expired idempotency records", "count", n)
					}
					select {
					case <-ctx.Done():
						return nil
					case <-t.C:
					}
				}
			})
			return s.Handler, nil
		},
	}))
}
