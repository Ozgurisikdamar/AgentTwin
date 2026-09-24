// Package logx configures structured JSON logging with correlation IDs and
// defensive redaction of credential-like attributes.
package logx

import (
	"context"
	"io"
	"log/slog"
	"os"
	"strings"

	"go.opentelemetry.io/otel/trace"
)

type ctxKey struct{}

// sensitiveKeys are attribute keys whose values must never reach a log line.
// Matching is by substring on the lower-cased key so "api_key", "x-api-key" and
// "apiKeySecret" are all caught.
var sensitiveKeys = []string{"password", "secret", "token", "authorization", "api_key", "apikey", "cookie", "pepper", "private_key"}

// IsSensitiveKey reports whether a log attribute key must be redacted.
func IsSensitiveKey(key string) bool {
	k := strings.ToLower(key)
	for _, s := range sensitiveKeys {
		if strings.Contains(k, s) {
			return true
		}
	}
	return false
}

// New returns a JSON logger for the given service writing to w (stdout when nil).
func New(service string, level slog.Level, w io.Writer) *slog.Logger {
	if w == nil {
		w = os.Stdout
	}
	h := slog.NewJSONHandler(w, &slog.HandlerOptions{
		Level: level,
		ReplaceAttr: func(_ []string, a slog.Attr) slog.Attr {
			if IsSensitiveKey(a.Key) {
				return slog.String(a.Key, "[REDACTED]")
			}
			return a
		},
	})
	return slog.New(&correlationHandler{Handler: h}).With("service", service)
}

// ParseLevel maps LOG_LEVEL values to slog levels (default info).
func ParseLevel(s string) slog.Level {
	switch strings.ToLower(s) {
	case "debug":
		return slog.LevelDebug
	case "warn", "warning":
		return slog.LevelWarn
	case "error":
		return slog.LevelError
	}
	return slog.LevelInfo
}

// WithRequestID stores a request id in the context for log correlation.
func WithRequestID(ctx context.Context, id string) context.Context {
	return context.WithValue(ctx, ctxKey{}, id)
}

// RequestID returns the request id stored in ctx, or "".
func RequestID(ctx context.Context) string {
	v, _ := ctx.Value(ctxKey{}).(string)
	return v
}

// correlationHandler adds request_id and trace_id/span_id from the context.
type correlationHandler struct{ slog.Handler }

func (h *correlationHandler) Handle(ctx context.Context, r slog.Record) error {
	if id := RequestID(ctx); id != "" {
		r.AddAttrs(slog.String("request_id", id))
	}
	if sc := trace.SpanContextFromContext(ctx); sc.IsValid() {
		r.AddAttrs(slog.String("trace_id", sc.TraceID().String()), slog.String("span_id", sc.SpanID().String()))
	}
	return h.Handler.Handle(ctx, r)
}

func (h *correlationHandler) WithAttrs(attrs []slog.Attr) slog.Handler {
	return &correlationHandler{Handler: h.Handler.WithAttrs(attrs)}
}

func (h *correlationHandler) WithGroup(name string) slog.Handler {
	return &correlationHandler{Handler: h.Handler.WithGroup(name)}
}
