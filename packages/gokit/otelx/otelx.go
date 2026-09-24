// Package otelx wires AgentTwin's own observability: Prometheus metrics and
// OpenTelemetry tracing for the Go services.
package otelx

import (
	"context"
	"net/http"
	"strconv"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/collectors"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	semconv "go.opentelemetry.io/otel/semconv/v1.26.0"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/httpx"
)

// Telemetry bundles the metric registry and tracer shutdown.
type Telemetry struct {
	Service  string
	Registry *prometheus.Registry

	HTTPRequests  *prometheus.CounterVec
	HTTPDuration  *prometheus.HistogramVec
	EventsHandled *prometheus.CounterVec
	EventDuration *prometheus.HistogramVec
	OutboxBacklog prometheus.Gauge
	OutboxFailed  prometheus.Counter
	DBDuration    *prometheus.HistogramVec

	shutdown func(context.Context) error
}

// Config controls telemetry setup.
type Config struct {
	Service      string
	OTLPEndpoint string // e.g. http://otel-collector:4318 ; empty disables tracing export
	SampleRatio  float64
}

// Setup creates metrics and (optionally) an OTLP trace exporter.
func Setup(ctx context.Context, cfg Config) (*Telemetry, error) {
	reg := prometheus.NewRegistry()
	reg.MustRegister(collectors.NewGoCollector(), collectors.NewProcessCollector(collectors.ProcessCollectorOpts{}))
	constLabels := prometheus.Labels{"service": cfg.Service}
	t := &Telemetry{
		Service:  cfg.Service,
		Registry: reg,
		HTTPRequests: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "agenttwin_http_requests_total", Help: "HTTP requests by route and status.", ConstLabels: constLabels,
		}, []string{"method", "route", "status"}),
		HTTPDuration: prometheus.NewHistogramVec(prometheus.HistogramOpts{
			Name: "agenttwin_http_request_duration_seconds", Help: "HTTP request latency.", ConstLabels: constLabels,
			Buckets: []float64{.005, .01, .025, .05, .1, .25, .5, 1, 2.5, 5, 10},
		}, []string{"method", "route"}),
		EventsHandled: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "agenttwin_events_consumed_total", Help: "Consumed events by type and outcome.", ConstLabels: constLabels,
		}, []string{"type", "outcome"}),
		EventDuration: prometheus.NewHistogramVec(prometheus.HistogramOpts{
			Name: "agenttwin_event_handling_seconds", Help: "Event handler duration.", ConstLabels: constLabels,
		}, []string{"type"}),
		OutboxBacklog: prometheus.NewGauge(prometheus.GaugeOpts{
			Name: "agenttwin_outbox_backlog", Help: "Unpublished outbox rows.", ConstLabels: constLabels,
		}),
		OutboxFailed: prometheus.NewCounter(prometheus.CounterOpts{
			Name: "agenttwin_outbox_publish_failures_total", Help: "Outbox publish failures.", ConstLabels: constLabels,
		}),
		DBDuration: prometheus.NewHistogramVec(prometheus.HistogramOpts{
			Name: "agenttwin_db_query_duration_seconds", Help: "Database query latency.", ConstLabels: constLabels,
			Buckets: []float64{.001, .0025, .005, .01, .025, .05, .1, .25, .5, 1, 2.5},
		}, []string{"outcome"}),
	}
	reg.MustRegister(t.HTTPRequests, t.HTTPDuration, t.EventsHandled, t.EventDuration, t.OutboxBacklog, t.OutboxFailed, t.DBDuration)

	otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(propagation.TraceContext{}, propagation.Baggage{}))
	t.shutdown = func(context.Context) error { return nil }
	if cfg.OTLPEndpoint != "" {
		exp, err := otlptracehttp.New(ctx, otlptracehttp.WithEndpointURL(cfg.OTLPEndpoint+"/v1/traces"), otlptracehttp.WithTimeout(5*time.Second))
		if err != nil {
			return nil, err
		}
		res, _ := resource.Merge(resource.Default(), resource.NewWithAttributes(semconv.SchemaURL,
			semconv.ServiceName(cfg.Service),
			semconv.ServiceNamespace("agenttwin"),
			attribute.String("agenttwin.internal", "true"),
		))
		ratio := cfg.SampleRatio
		if ratio <= 0 {
			ratio = 1
		}
		tp := sdktrace.NewTracerProvider(
			sdktrace.WithBatcher(exp, sdktrace.WithMaxQueueSize(2048)),
			sdktrace.WithResource(res),
			sdktrace.WithSampler(sdktrace.ParentBased(sdktrace.TraceIDRatioBased(ratio))),
		)
		otel.SetTracerProvider(tp)
		t.shutdown = tp.Shutdown
	}
	return t, nil
}

// HTTPObserver records request metrics (use with httpx.AccessLog).
func (t *Telemetry) HTTPObserver() httpx.Observer {
	return func(r *http.Request, route string, status int, dur time.Duration) {
		t.HTTPRequests.WithLabelValues(r.Method, route, strconv.Itoa(status)).Inc()
		t.HTTPDuration.WithLabelValues(r.Method, route).Observe(dur.Seconds())
	}
}

// EventObserver records consumer metrics.
func (t *Telemetry) EventObserver() func(eventType, outcome string, dur time.Duration) {
	return func(eventType, outcome string, dur time.Duration) {
		if eventType == "" {
			eventType = "unknown"
		}
		t.EventsHandled.WithLabelValues(eventType, outcome).Inc()
		t.EventDuration.WithLabelValues(eventType).Observe(dur.Seconds())
	}
}

// MetricsHandler serves /metrics.
func (t *Telemetry) MetricsHandler() http.Handler {
	return promhttp.HandlerFor(t.Registry, promhttp.HandlerOpts{})
}

// WrapHandler adds OTel server spans around h.
func WrapHandler(h http.Handler, service string) http.Handler {
	return otelhttp.NewHandler(h, service, otelhttp.WithFilter(func(r *http.Request) bool {
		return r.URL.Path != "/metrics" && r.URL.Path != "/health/live" && r.URL.Path != "/health/ready"
	}))
}

// HTTPClient returns a client that propagates trace context.
func HTTPClient(timeout time.Duration) *http.Client {
	return &http.Client{Timeout: timeout, Transport: otelhttp.NewTransport(http.DefaultTransport)}
}

// Shutdown flushes traces.
func (t *Telemetry) Shutdown(ctx context.Context) {
	_ = t.shutdown(ctx)
}

// QueryTracer measures pgx query latency into DBDuration.
type QueryTracer struct{ Hist *prometheus.HistogramVec }

type qtKey struct{}

// TraceQueryStart implements pgx.QueryTracer.
func (q QueryTracer) TraceQueryStart(ctx context.Context, _ *pgx.Conn, _ pgx.TraceQueryStartData) context.Context {
	return context.WithValue(ctx, qtKey{}, time.Now())
}

// TraceQueryEnd implements pgx.QueryTracer.
func (q QueryTracer) TraceQueryEnd(ctx context.Context, _ *pgx.Conn, data pgx.TraceQueryEndData) {
	start, ok := ctx.Value(qtKey{}).(time.Time)
	if !ok {
		return
	}
	outcome := "ok"
	if data.Err != nil {
		outcome = "error"
	}
	q.Hist.WithLabelValues(outcome).Observe(time.Since(start).Seconds())
}
