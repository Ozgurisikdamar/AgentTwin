package httpx

import (
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"regexp"
	"runtime/debug"
	"strings"
	"sync"
	"time"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/logx"
)

// Middleware wraps an http.Handler.
type Middleware func(http.Handler) http.Handler

// Chain applies middlewares so that the first one is the outermost.
func Chain(h http.Handler, mws ...Middleware) http.Handler {
	for i := len(mws) - 1; i >= 0; i-- {
		h = mws[i](h)
	}
	return h
}

// RequestIDHeader is propagated between services.
const RequestIDHeader = "X-Request-Id"

var validRequestID = regexp.MustCompile(`^[A-Za-z0-9._:-]{8,128}$`)

// NewRequestID returns a random 16-byte hex id.
func NewRequestID() string {
	var b [16]byte
	_, _ = rand.Read(b[:])
	return hex.EncodeToString(b[:])
}

// RequestID accepts a well-formed incoming X-Request-Id (so a request can be
// correlated across services) or generates one, and echoes it in the response.
func RequestID() Middleware {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			id := r.Header.Get(RequestIDHeader)
			if !validRequestID.MatchString(id) {
				id = NewRequestID()
			}
			w.Header().Set(RequestIDHeader, id)
			next.ServeHTTP(w, r.WithContext(logx.WithRequestID(r.Context(), id)))
		})
	}
}

// Recover turns panics into 500 responses and logs the stack.
func Recover() Middleware {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			defer func() {
				if rec := recover(); rec != nil {
					if err, ok := rec.(error); ok && errors.Is(err, http.ErrAbortHandler) {
						panic(rec)
					}
					slog.ErrorContext(r.Context(), "panic", "panic", fmt.Sprint(rec), "stack", string(debug.Stack()))
					WriteError(w, r, ErrInternal)
				}
			}()
			next.ServeHTTP(w, r)
		})
	}
}

// SecurityHeaders sets conservative headers for API responses.
func SecurityHeaders() Middleware {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			h := w.Header()
			h.Set("X-Content-Type-Options", "nosniff")
			h.Set("X-Frame-Options", "DENY")
			h.Set("Referrer-Policy", "no-referrer")
			h.Set("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
			h.Set("Cache-Control", "no-store")
			next.ServeHTTP(w, r)
		})
	}
}

// CORS allows the configured origins only (no wildcard with credentials).
func CORS(allowed []string) Middleware {
	set := map[string]bool{}
	for _, o := range allowed {
		set[strings.TrimRight(o, "/")] = true
	}
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			origin := r.Header.Get("Origin")
			if origin != "" && set[origin] {
				w.Header().Set("Access-Control-Allow-Origin", origin)
				w.Header().Set("Access-Control-Allow-Credentials", "true")
				w.Header().Set("Vary", "Origin")
				if r.Method == http.MethodOptions {
					w.Header().Set("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
					w.Header().Set("Access-Control-Allow-Headers", "Authorization, Content-Type, Idempotency-Key, X-Request-Id")
					w.Header().Set("Access-Control-Max-Age", "600")
					w.WriteHeader(http.StatusNoContent)
					return
				}
			}
			next.ServeHTTP(w, r)
		})
	}
}

type statusRecorder struct {
	http.ResponseWriter
	status int
	bytes  int
}

func (s *statusRecorder) WriteHeader(code int) {
	s.status = code
	s.ResponseWriter.WriteHeader(code)
}

func (s *statusRecorder) Write(b []byte) (int, error) {
	if s.status == 0 {
		s.status = http.StatusOK
	}
	n, err := s.ResponseWriter.Write(b)
	s.bytes += n
	return n, err
}

func (s *statusRecorder) Flush() {
	if f, ok := s.ResponseWriter.(http.Flusher); ok {
		f.Flush()
	}
}

// Observer receives per-request measurements (metrics).
type Observer func(r *http.Request, route string, status int, dur time.Duration)

// AccessLog logs one line per request (path only, never query strings, which
// may carry tokens) and notifies observers.
func AccessLog(log *slog.Logger, observers ...Observer) Middleware {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			start := time.Now()
			rec := &statusRecorder{ResponseWriter: w}
			next.ServeHTTP(rec, r)
			if rec.status == 0 {
				rec.status = http.StatusOK
			}
			dur := time.Since(start)
			route := r.Pattern
			if route == "" {
				route = "unmatched"
			}
			for _, o := range observers {
				o(r, route, rec.status, dur)
			}
			if strings.HasPrefix(r.URL.Path, "/health/") || r.URL.Path == "/metrics" {
				return
			}
			level := slog.LevelInfo
			if rec.status >= 500 {
				level = slog.LevelError
			}
			log.LogAttrs(r.Context(), level, "http request",
				slog.String("method", r.Method), slog.String("path", r.URL.Path), slog.String("route", route),
				slog.Int("status", rec.status), slog.Int64("duration_ms", dur.Milliseconds()), slog.Int("bytes", rec.bytes))
		})
	}
}

// ClientIP returns the client address. Forwarded headers are honored only
// when the direct peer is a trusted proxy (reverse proxy awareness without
// letting clients spoof their address).
func ClientIP(r *http.Request, trustedProxies []*net.IPNet) string {
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		host = r.RemoteAddr
	}
	peer := net.ParseIP(host)
	trusted := false
	for _, n := range trustedProxies {
		if peer != nil && n.Contains(peer) {
			trusted = true
			break
		}
	}
	if trusted {
		if xff := r.Header.Get("X-Forwarded-For"); xff != "" {
			parts := strings.Split(xff, ",")
			return strings.TrimSpace(parts[0])
		}
	}
	return host
}

// RateLimiter is a per-key token bucket (in-process; see ADR-0013).
type RateLimiter struct {
	mu       sync.Mutex
	rate     float64 // tokens per second
	burst    float64
	buckets  map[string]*bucket
	now      func() time.Time
	maxKeys  int
	lastSwep time.Time
}

type bucket struct {
	tokens float64
	last   time.Time
}

// NewRateLimiter creates a limiter allowing ratePerSec sustained with burst.
func NewRateLimiter(ratePerSec float64, burst int) *RateLimiter {
	return &RateLimiter{rate: ratePerSec, burst: float64(burst), buckets: map[string]*bucket{}, now: time.Now, maxKeys: 100000}
}

// Allow reports whether a request for key may proceed.
func (rl *RateLimiter) Allow(key string) bool {
	rl.mu.Lock()
	defer rl.mu.Unlock()
	now := rl.now()
	if now.Sub(rl.lastSwep) > time.Minute || len(rl.buckets) > rl.maxKeys {
		for k, b := range rl.buckets {
			if now.Sub(b.last) > 10*time.Minute {
				delete(rl.buckets, k)
			}
		}
		rl.lastSwep = now
	}
	b, ok := rl.buckets[key]
	if !ok {
		b = &bucket{tokens: rl.burst, last: now}
		rl.buckets[key] = b
	}
	b.tokens += now.Sub(b.last).Seconds() * rl.rate
	if b.tokens > rl.burst {
		b.tokens = rl.burst
	}
	b.last = now
	if b.tokens < 1 {
		return false
	}
	b.tokens--
	return true
}

// RateLimit rejects requests exceeding the limiter for the key chosen by keyFn.
func RateLimit(rl *RateLimiter, keyFn func(*http.Request) string) Middleware {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if !rl.Allow(keyFn(r)) {
				w.Header().Set("Retry-After", "1")
				WriteError(w, r, ErrRateLimited)
				return
			}
			next.ServeHTTP(w, r)
		})
	}
}
