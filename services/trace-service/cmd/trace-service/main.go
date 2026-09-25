// Command trace-service ingests OTLP traces, normalizes them through versioned
// semantic-convention adapters, enforces the project's content policy, and
// serves the trace explorer.
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"time"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/config"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/service"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/svcclient"
	"github.com/Ozgurisikdamar/AgentTwin/services/trace-service/internal/api"
	"github.com/Ozgurisikdamar/AgentTwin/services/trace-service/internal/finalizer"
	"github.com/Ozgurisikdamar/AgentTwin/services/trace-service/internal/keys"
	"github.com/Ozgurisikdamar/AgentTwin/services/trace-service/internal/otlp"
	"github.com/Ozgurisikdamar/AgentTwin/services/trace-service/internal/store"
	"github.com/Ozgurisikdamar/AgentTwin/services/trace-service/internal/summary"
	"github.com/Ozgurisikdamar/AgentTwin/services/trace-service/migrations"
)

type traceConfig struct {
	controlPlaneURL string
	settle          time.Duration
	incompleteAfter time.Duration
	keyCacheTTL     time.Duration
	maxConcurrent   int
	maxBodyBytes    int
	pricing         summary.Pricing
}

func main() {
	var c traceConfig
	os.Exit(service.Main(service.Spec{
		Name:        "trace-service",
		DefaultPort: 8081,
		Schema:      migrations.Schema,
		Migrations:  migrations.FS,
		Configure: func(l *config.Loader) {
			c.controlPlaneURL = l.Required("CONTROL_PLANE_URL")
			c.settle = l.Duration("TRACE_SETTLE_DURATION", 2*time.Second)
			c.incompleteAfter = l.Duration("TRACE_INCOMPLETE_AFTER", 2*time.Minute)
			c.keyCacheTTL = l.Duration("API_KEY_CACHE_TTL", 30*time.Second)
			c.maxConcurrent = l.Int("INGEST_MAX_CONCURRENT", 16, 1, 1024)
			c.maxBodyBytes = l.Int("INGEST_MAX_BODY_BYTES", 16<<20, 1<<10, 256<<20)
			if raw := l.String("MODEL_PRICING_JSON", ""); raw != "" {
				err := json.Unmarshal([]byte(raw), &c.pricing)
				l.Check(err == nil, "MODEL_PRICING_JSON", fmt.Sprintf("must be a JSON object of {model: {input_per_mtok, output_per_mtok}}: %v", err))
			}
		},
		Build: func(ctx context.Context, rt *service.Runtime) (http.Handler, error) {
			cp, err := svcclient.New("control-plane", c.controlPlaneURL, rt.Tokens, 5*time.Second)
			if err != nil {
				return nil, err
			}
			st := store.New(rt.Pool)
			metrics := api.NewMetrics(rt.Tel.Registry)
			srv := &api.Server{
				Store: st, Keys: keys.NewVerifier(keys.ControlPlaneLookup(cp, "trace-service"), c.keyCacheTTL, 10*time.Second),
				Log: rt.Log, Metrics: metrics, Limits: otlp.DefaultLimits, MaxBody: int64(c.maxBodyBytes), Tokens: rt.Tokens,
			}
			srv.Init(c.maxConcurrent)
			fin := &finalizer.Finalizer{Pool: rt.Pool, Store: st, Log: rt.Log, Settle: c.settle, IncompleteAfter: c.incompleteAfter,
				Pricing: c.pricing, Observe: func(n int) { metrics.Finalized.Add(float64(n)) },
				OnError: metrics.FinalizeErrors.Inc}
			rt.Go(ctx, "finalizer", func(ctx context.Context) error { fin.Run(ctx); return nil })
			rt.Relay(ctx, migrations.Schema)
			rt.Go(ctx, "retention", func(ctx context.Context) error {
				t := time.NewTicker(10 * time.Minute)
				defer t.Stop()
				for {
					for {
						n, err := st.PurgeExpired(ctx, 1000)
						if err != nil || n < 1000 {
							break
						}
					}
					if n, err := st.PurgeContent(ctx, 500); err == nil && n > 0 {
						rt.Log.Info("purged expired trace content", "traces", n)
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
