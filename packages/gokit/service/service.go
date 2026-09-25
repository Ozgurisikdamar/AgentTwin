// Package service is the common process skeleton of every Go service:
// configuration, logging, telemetry, database pool, migrations, broker,
// internal token service, the standard HTTP middleware stack and graceful
// shutdown. A service main only declares what is specific to it (Spec).
package service

import (
	"context"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"log/slog"
	"net/http"
	"os"
	"strconv"
	"sync"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/authn"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/buildinfo"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/config"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/db"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/events"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/httpx"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/logx"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/otelx"
)

// Config is the configuration shared by every Go service.
type Config struct {
	Name           string
	Env            config.Environment
	Port           int
	LogLevel       string
	DatabaseURL    string
	AMQPURL        string
	InternalSecret string
	OTLPEndpoint   string
	SampleRatio    float64
	MigrateOnStart bool
	DBMaxConns     int
	CORSOrigins    []string
}

// LoadConfig reads the shared variables. Problems accumulate in l.
func LoadConfig(l *config.Loader, name string, defaultPort int) Config {
	return Config{
		Name:           name,
		Env:            l.Env(),
		Port:           l.Int("PORT", defaultPort, 1, 65535),
		LogLevel:       l.OneOf("LOG_LEVEL", "info", "debug", "info", "warn", "error"),
		DatabaseURL:    l.Required("DATABASE_URL"),
		AMQPURL:        l.Required("AMQP_URL"),
		InternalSecret: l.Secret("AGENTTWIN_INTERNAL_TOKEN_SECRET", 32),
		OTLPEndpoint:   l.String("OTEL_EXPORTER_OTLP_ENDPOINT", ""),
		SampleRatio:    l.Float("OTEL_TRACES_SAMPLER_ARG", 1.0, 0, 1),
		MigrateOnStart: l.Bool("MIGRATE_ON_START", l.Env() != config.Production),
		DBMaxConns:     l.Int("DB_MAX_CONNS", 10, 1, 200),
		CORSOrigins:    l.List("CORS_ALLOWED_ORIGINS", nil),
	}
}

// Spec declares what is specific to a service.
type Spec struct {
	Name        string
	DefaultPort int
	// Schema is the PostgreSQL schema owned by the service.
	Schema string
	// Migrations holds NNNN_name.(up|down).sql files at its root.
	Migrations fs.FS
	// Configure reads service-specific variables; errors accumulate in l.
	Configure func(l *config.Loader)
	// Build wires the service and returns the application handler. Health
	// and metrics endpoints are added by the runtime.
	Build func(ctx context.Context, rt *Runtime) (http.Handler, error)
}

// Runtime holds initialized infrastructure for a running service.
type Runtime struct {
	Config Config
	Log    *slog.Logger
	Tel    *otelx.Telemetry
	Pool   *pgxpool.Pool
	Broker *events.Broker
	Tokens *authn.TokenService
	Health *httpx.Health

	mu      sync.Mutex
	workers sync.WaitGroup
	cancel  context.CancelFunc
	closers []func(context.Context)
}

// Main is the process entry point: `<svc> [serve]`, `<svc> migrate [up|down N|status]`,
// `<svc> healthcheck [live|ready]`.
func Main(spec Spec) int {
	args := os.Args[1:]
	cmd := "serve"
	if len(args) > 0 {
		cmd, args = args[0], args[1:]
	}
	switch cmd {
	case "healthcheck":
		// Runs before configuration loading: the probe needs only the port
		// and must not fail because an unrelated variable is invalid.
		return Healthcheck(os.Getenv("PORT"), spec.DefaultPort, args, os.Stderr)
	case "serve", "migrate":
	default:
		fmt.Fprintf(os.Stderr, "usage: %s [serve | migrate [up | down N | status] | healthcheck [live | ready]]\n", spec.Name)
		return 2
	}
	l := config.New()
	cfg := LoadConfig(l, spec.Name, spec.DefaultPort)
	if spec.Configure != nil {
		spec.Configure(l)
	}
	log := logx.New(spec.Name, logx.ParseLevel(cfg.LogLevel), os.Stdout)
	slog.SetDefault(log) // package-level slog calls (httpx.WriteError, panics) stay structured
	if err := l.Err(); err != nil {
		log.Error("invalid configuration", "error", err.Error())
		return 2
	}
	ctx := context.Background()
	switch cmd {
	case "serve":
		if err := serve(ctx, spec, cfg, log); err != nil {
			log.Error("service failed", "error", err.Error())
			return 1
		}
		return 0
	case "migrate":
		if err := migrate(ctx, spec, cfg, log, args); err != nil {
			log.Error("migration failed", "error", err.Error())
			return 1
		}
		return 0
	}
	return 2 // unreachable: commands are validated above
}

// Healthcheck probes the local process's health endpoint and returns the
// process exit code (0 healthy). It is the container HEALTHCHECK of the
// distroless images, which have no shell or curl. The default probe is
// readiness (database and broker reachable); "live" probes liveness only.
func Healthcheck(portEnv string, defaultPort int, args []string, stderr io.Writer) int {
	port := defaultPort
	if portEnv != "" {
		p, err := strconv.Atoi(portEnv)
		if err != nil || p < 1 || p > 65535 {
			_, _ = fmt.Fprintf(stderr, "healthcheck: invalid PORT %q\n", portEnv)
			return 2
		}
		port = p
	}
	path := "/health/ready"
	if len(args) > 0 {
		switch args[0] {
		case "live":
			path = "/health/live"
		case "ready":
		default:
			_, _ = fmt.Fprintf(stderr, "usage: healthcheck [live|ready]\n")
			return 2
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, "http://127.0.0.1:"+strconv.Itoa(port)+path, nil)
	if err != nil {
		_, _ = fmt.Fprintf(stderr, "healthcheck: %v\n", err)
		return 2
	}
	// No proxy: the probe always targets the loopback interface.
	client := &http.Client{Transport: &http.Transport{Proxy: nil}}
	resp, err := client.Do(req)
	if err != nil {
		_, _ = fmt.Fprintf(stderr, "healthcheck: %v\n", err)
		return 1
	}
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 64<<10))
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		_, _ = fmt.Fprintf(stderr, "healthcheck: %s returned %d\n", path, resp.StatusCode)
		return 1
	}
	return 0
}

func loadMigrations(spec Spec) ([]db.Migration, error) {
	if spec.Migrations == nil {
		return nil, nil
	}
	return db.LoadMigrations(spec.Migrations, ".")
}

func migrate(ctx context.Context, spec Spec, cfg Config, log *slog.Logger, args []string) error {
	migs, err := loadMigrations(spec)
	if err != nil {
		return err
	}
	pool, err := db.Connect(ctx, log, db.PoolConfig{URL: cfg.DatabaseURL, MaxConns: 2, Schema: spec.Schema})
	if err != nil {
		return err
	}
	defer pool.Close()
	m := &db.Migrator{Pool: pool, Schema: spec.Schema, Migrations: migs, Log: log}
	action := "up"
	if len(args) > 0 {
		action = args[0]
	}
	switch action {
	case "up":
		n, err := m.Up(ctx)
		if err != nil {
			return err
		}
		log.Info("migrations applied", "schema", spec.Schema, "count", n)
	case "down":
		steps := 1
		if len(args) > 1 {
			steps, err = strconv.Atoi(args[1])
			if err != nil || steps < 1 {
				return fmt.Errorf("down requires a positive step count, got %q", args[1])
			}
		}
		n, err := m.Down(ctx, steps)
		if err != nil {
			return err
		}
		log.Info("migrations reverted", "schema", spec.Schema, "count", n)
	case "status":
		pending, err := m.Pending(ctx)
		if err != nil {
			return err
		}
		if pending == nil {
			pending = []string{}
		}
		log.Info("migration status", "schema", spec.Schema, "pending", len(pending),
			"pending_versions", pending, "known", len(migs))
	default:
		return fmt.Errorf("unknown migrate action %q (up | down N | status)", action)
	}
	return nil
}

// Start initializes infrastructure. Exported for integration tests that wire
// a service in-process.
func Start(ctx context.Context, spec Spec, cfg Config, log *slog.Logger) (*Runtime, error) {
	rt := &Runtime{Config: cfg, Log: log, Health: httpx.NewHealth()}
	tel, err := otelx.Setup(ctx, otelx.Config{Service: cfg.Name, OTLPEndpoint: cfg.OTLPEndpoint, SampleRatio: cfg.SampleRatio})
	if err != nil {
		return nil, fmt.Errorf("telemetry: %w", err)
	}
	rt.Tel = tel
	rt.onClose(tel.Shutdown)

	tokens, err := authn.NewTokenService(cfg.InternalSecret)
	if err != nil {
		return nil, err
	}
	rt.Tokens = tokens

	if cfg.DatabaseURL != "" && spec.Schema != "" {
		pool, err := db.Connect(ctx, log, db.PoolConfig{
			URL: cfg.DatabaseURL, MaxConns: int32(min(max(cfg.DBMaxConns, 1), 200)), //nolint:gosec // bounded to [1, 200]
			Schema: spec.Schema, StartupAttempts: 20,
			Tracer: otelx.QueryTracer{Hist: tel.DBDuration},
		})
		if err != nil {
			return nil, err
		}
		rt.Pool = pool
		rt.onClose(func(context.Context) { pool.Close() })
		rt.Health.Add(httpx.Checker{Name: "postgres", Check: db.Checker(pool)})
		if cfg.MigrateOnStart {
			migs, err := loadMigrations(spec)
			if err != nil {
				return nil, err
			}
			m := &db.Migrator{Pool: pool, Schema: spec.Schema, Migrations: migs, Log: log}
			n, err := m.Up(ctx)
			if err != nil {
				return nil, fmt.Errorf("migrate %s: %w", spec.Schema, err)
			}
			log.Info("migrations up to date", "schema", spec.Schema, "applied", n)
		}
	}

	if cfg.AMQPURL != "" {
		b, err := events.Dial(ctx, cfg.AMQPURL, log)
		if err != nil {
			return nil, err
		}
		rt.Broker = b
		rt.onClose(func(context.Context) { _ = b.Close() })
		rt.Health.Add(httpx.Checker{Name: "rabbitmq", Check: b.Ping})
	}
	return rt, nil
}

func (rt *Runtime) onClose(fn func(context.Context)) {
	rt.mu.Lock()
	defer rt.mu.Unlock()
	rt.closers = append(rt.closers, fn)
}

// Go runs a background worker tied to the runtime lifecycle. The worker
// context is cancelled on shutdown and awaited before resources close.
func (rt *Runtime) Go(ctx context.Context, name string, fn func(ctx context.Context) error) {
	rt.workers.Add(1)
	go func() {
		defer rt.workers.Done()
		defer func() {
			if rec := recover(); rec != nil {
				rt.Log.Error("background worker panicked", "worker", name, "panic", fmt.Sprint(rec))
			}
		}()
		if err := fn(ctx); err != nil && !errors.Is(err, context.Canceled) {
			rt.Log.Error("background worker stopped", "worker", name, "error", err.Error())
		}
	}()
}

// Relay starts the transactional outbox relay for the service schema and
// exposes its backlog as a readiness-independent metric.
func (rt *Runtime) Relay(ctx context.Context, schema string) {
	relay := &events.OutboxRelay{Pool: rt.Pool, Schema: schema, Publisher: rt.Broker, Log: rt.Log,
		Observe: func(_ int, failed bool) {
			if failed {
				rt.Tel.OutboxFailed.Inc()
			}
		}}
	rt.Go(ctx, "outbox-relay", func(ctx context.Context) error { relay.Run(ctx); return nil })
	rt.Go(ctx, "outbox-backlog", func(ctx context.Context) error {
		t := time.NewTicker(10 * time.Second)
		defer t.Stop()
		for {
			if n, err := events.Backlog(ctx, rt.Pool, schema); err == nil {
				rt.Tel.OutboxBacklog.Set(float64(n))
			}
			select {
			case <-ctx.Done():
				return nil
			case <-t.C:
			}
		}
	})
}

// Consume starts a consumer for queue with the given per-type handlers.
// Unknown types are acknowledged (logged) so that adding a routing key to a
// queue before its handler ships never poisons the queue.
func (rt *Runtime) Consume(ctx context.Context, queue string, concurrency int, handlers map[string]events.Handler) {
	rt.Go(ctx, "consumer:"+queue, func(ctx context.Context) error {
		return rt.Broker.Consume(ctx, events.ConsumerConfig{Queue: queue, Concurrency: concurrency, Observe: rt.Tel.EventObserver()},
			func(ctx context.Context, env events.Envelope) error {
				h, ok := handlers[env.Type]
				if !ok {
					rt.Log.WarnContext(ctx, "no handler for event type; acknowledged", "queue", queue, "type", env.Type, "event_id", env.ID)
					return nil
				}
				return h(ctx, env)
			})
	})
}

// Handler wraps the application handler with the standard middleware stack
// and adds /health/* and /metrics.
func (rt *Runtime) Handler(app http.Handler) http.Handler {
	root := http.NewServeMux()
	rt.Health.Register(root)
	root.Handle("GET /metrics", rt.Tel.MetricsHandler())
	root.Handle("/", app)
	h := httpx.Chain(root,
		httpx.RequestID(),
		httpx.Recover(),
		httpx.SecurityHeaders(),
		httpx.CORS(rt.Config.CORSOrigins),
		httpx.AccessLog(rt.Log, rt.Tel.HTTPObserver()),
		httpx.ValidText(),
	)
	return otelx.WrapHandler(h, rt.Config.Name)
}

// Close cancels workers, waits for them, then releases resources.
func (rt *Runtime) Close(ctx context.Context) {
	if rt.cancel != nil {
		rt.cancel()
	}
	done := make(chan struct{})
	go func() { rt.workers.Wait(); close(done) }()
	select {
	case <-done:
	case <-ctx.Done():
		rt.Log.Warn("background workers did not stop before shutdown deadline")
	}
	rt.mu.Lock()
	closers := rt.closers
	rt.mu.Unlock()
	for i := len(closers) - 1; i >= 0; i-- {
		closers[i](ctx)
	}
}

func serve(ctx context.Context, spec Spec, cfg Config, log *slog.Logger) error {
	log.Info("starting", "version", buildinfo.Version, "revision", buildinfo.Revision(), "env", string(cfg.Env))
	workerCtx, cancel := context.WithCancel(ctx)
	rt, err := Start(workerCtx, spec, cfg, log)
	if err != nil {
		cancel()
		return err
	}
	rt.cancel = cancel
	app, err := spec.Build(workerCtx, rt)
	if err != nil {
		rt.Close(context.Background())
		return err
	}
	return httpx.Run(ctx, log, httpx.ServerConfig{Addr: ":" + strconv.Itoa(cfg.Port)}, rt.Handler(app), rt.Health, rt.Close)
}
