package httpx

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"sync"
	"syscall"
	"time"
)

// Checker reports readiness of a dependency.
type Checker struct {
	Name  string
	Check func(ctx context.Context) error
}

// Health serves /health/live and /health/ready.
type Health struct {
	mu       sync.RWMutex
	checkers []Checker
	draining bool
	timeout  time.Duration
}

// NewHealth creates a health handler with the given readiness checkers.
func NewHealth(checkers ...Checker) *Health {
	return &Health{checkers: checkers, timeout: 2 * time.Second}
}

// Add registers an extra readiness checker.
func (h *Health) Add(c Checker) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.checkers = append(h.checkers, c)
}

// SetDraining makes readiness fail so load balancers stop routing during shutdown.
func (h *Health) SetDraining() {
	h.mu.Lock()
	h.draining = true
	h.mu.Unlock()
}

// Register mounts the health endpoints on mux.
func (h *Health) Register(mux *http.ServeMux) {
	mux.HandleFunc("GET /health/live", func(w http.ResponseWriter, _ *http.Request) {
		WriteJSON(w, http.StatusOK, map[string]string{"status": "live"})
	})
	mux.HandleFunc("GET /health/ready", func(w http.ResponseWriter, r *http.Request) {
		h.mu.RLock()
		checkers := append([]Checker(nil), h.checkers...)
		draining := h.draining
		h.mu.RUnlock()
		result := map[string]string{}
		ok := !draining
		if draining {
			result["shutdown"] = "draining"
		}
		ctx, cancel := context.WithTimeout(r.Context(), h.timeout)
		defer cancel()
		for _, c := range checkers {
			if err := c.Check(ctx); err != nil {
				ok = false
				result[c.Name] = "unavailable"
			} else {
				result[c.Name] = "ok"
			}
		}
		status := http.StatusOK
		state := "ready"
		if !ok {
			status = http.StatusServiceUnavailable
			state = "not_ready"
		}
		WriteJSON(w, status, map[string]any{"status": state, "checks": result})
	})
}

// ServerConfig holds HTTP server settings.
type ServerConfig struct {
	Addr            string
	ReadTimeout     time.Duration
	WriteTimeout    time.Duration
	IdleTimeout     time.Duration
	ShutdownTimeout time.Duration
}

// Run serves handler until SIGINT/SIGTERM or ctx cancellation, then drains
// in-flight requests (graceful shutdown). onShutdown runs after the listener
// stops accepting connections (close consumers, flush telemetry).
func Run(ctx context.Context, log *slog.Logger, cfg ServerConfig, handler http.Handler, health *Health, onShutdown ...func(context.Context)) error {
	if cfg.ReadTimeout == 0 {
		cfg.ReadTimeout = 15 * time.Second
	}
	if cfg.WriteTimeout == 0 {
		cfg.WriteTimeout = 60 * time.Second
	}
	if cfg.IdleTimeout == 0 {
		cfg.IdleTimeout = 120 * time.Second
	}
	if cfg.ShutdownTimeout == 0 {
		cfg.ShutdownTimeout = 20 * time.Second
	}
	srv := &http.Server{
		Addr:              cfg.Addr,
		Handler:           handler,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       cfg.ReadTimeout,
		WriteTimeout:      cfg.WriteTimeout,
		IdleTimeout:       cfg.IdleTimeout,
		MaxHeaderBytes:    64 << 10,
		BaseContext:       func(net.Listener) context.Context { return ctx },
	}
	ctx, stop := signal.NotifyContext(ctx, os.Interrupt, syscall.SIGTERM)
	defer stop()

	errCh := make(chan error, 1)
	go func() {
		log.Info("http server listening", "addr", cfg.Addr)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			errCh <- err
		}
		close(errCh)
	}()

	select {
	case err := <-errCh:
		return err
	case <-ctx.Done():
	}
	log.Info("shutdown requested; draining")
	if health != nil {
		health.SetDraining()
	}
	shutdownCtx, cancel := context.WithTimeout(context.Background(), cfg.ShutdownTimeout)
	defer cancel()
	err := srv.Shutdown(shutdownCtx)
	for _, fn := range onShutdown {
		fn(shutdownCtx)
	}
	log.Info("shutdown complete")
	return err
}

// Cursor is an opaque keyset pagination position (sort timestamp + tiebreak id).
type Cursor struct {
	TS time.Time `json:"t"`
	ID string    `json:"i"`
}

// EncodeCursor returns an opaque cursor token.
func EncodeCursor(c Cursor) string {
	b, _ := json.Marshal(c)
	return base64.RawURLEncoding.EncodeToString(b)
}

// DecodeCursor parses a cursor token; empty input returns (nil, nil).
func DecodeCursor(s string) (*Cursor, error) {
	if s == "" {
		return nil, nil
	}
	if len(s) > 512 {
		return nil, Invalid("INVALID_CURSOR", "Cursor is malformed.", nil)
	}
	b, err := base64.RawURLEncoding.DecodeString(s)
	if err != nil {
		return nil, Invalid("INVALID_CURSOR", "Cursor is malformed.", nil)
	}
	var c Cursor
	if err := json.Unmarshal(b, &c); err != nil || c.ID == "" {
		return nil, Invalid("INVALID_CURSOR", "Cursor is malformed.", nil)
	}
	return &c, nil
}

// QueryInt parses an integer query parameter with bounds and a default.
func QueryInt(r *http.Request, key string, def, min, max int) (int, error) {
	v := r.URL.Query().Get(key)
	if v == "" {
		return def, nil
	}
	n, err := strconv.Atoi(v)
	if err != nil || n < min || n > max {
		return 0, Invalid("INVALID_PARAMETER", "Query parameter "+key+" must be an integer between "+strconv.Itoa(min)+" and "+strconv.Itoa(max)+".", map[string]any{"parameter": key})
	}
	return n, nil
}

// Page is the standard list envelope.
type Page[T any] struct {
	Items      []T    `json:"items"`
	NextCursor string `json:"next_cursor,omitempty"`
}
