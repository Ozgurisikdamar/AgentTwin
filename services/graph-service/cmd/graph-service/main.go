// Command graph-service keeps each project's dependency graph (agents, their
// versions, prompts, models, tools, the systems those tools reach, and the
// scenarios and policies that cover them) from the events that describe it,
// and answers what a change can influence.
package main

import (
	"context"
	"net/http"
	"os"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/config"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/service"
	"github.com/Ozgurisikdamar/AgentTwin/services/graph-service/internal/api"
	"github.com/Ozgurisikdamar/AgentTwin/services/graph-service/internal/store"
	"github.com/Ozgurisikdamar/AgentTwin/services/graph-service/migrations"
)

func main() {
	var concurrency int
	os.Exit(service.Main(service.Spec{
		Name:        "graph-service",
		DefaultPort: 8082,
		Schema:      migrations.Schema,
		Migrations:  migrations.FS,
		Configure: func(l *config.Loader) {
			concurrency = l.Int("GRAPH_CONSUMER_CONCURRENCY", 4, 1, 64)
		},
		Build: func(ctx context.Context, rt *service.Runtime) (http.Handler, error) {
			s := &api.Server{Store: store.New(rt.Pool), Tokens: rt.Tokens, Log: rt.Log}
			rt.Relay(ctx, migrations.Schema)
			rt.Consume(ctx, "graph-service.events", concurrency, s.Handlers())
			mux := http.NewServeMux()
			s.Routes(mux)
			return mux, nil
		},
	}))
}
