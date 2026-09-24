// Package db provides PostgreSQL connectivity, schema migrations and
// transaction helpers for the Go services.
package db

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

// PoolConfig controls connection pooling.
type PoolConfig struct {
	URL             string
	MaxConns        int32
	MinConns        int32
	ConnectTimeout  time.Duration
	StartupAttempts int
	// Schema, when set, becomes the connection search_path (own schema first).
	Schema string
	// Tracer optionally observes every query (latency metrics).
	Tracer pgx.QueryTracer
}

// Connect opens a pool, retrying with exponential backoff while the database
// starts (docker compose ordering is not a readiness guarantee).
func Connect(ctx context.Context, log *slog.Logger, cfg PoolConfig) (*pgxpool.Pool, error) {
	pc, err := pgxpool.ParseConfig(cfg.URL)
	if err != nil {
		return nil, fmt.Errorf("parse database url: %w", err)
	}
	if cfg.MaxConns > 0 {
		pc.MaxConns = cfg.MaxConns
	}
	if cfg.MinConns > 0 {
		pc.MinConns = cfg.MinConns
	}
	pc.MaxConnIdleTime = 5 * time.Minute
	pc.HealthCheckPeriod = 30 * time.Second
	if cfg.ConnectTimeout == 0 {
		cfg.ConnectTimeout = 5 * time.Second
	}
	pc.ConnConfig.ConnectTimeout = cfg.ConnectTimeout
	if cfg.Schema != "" {
		pc.ConnConfig.RuntimeParams["search_path"] = cfg.Schema + ",public"
	}
	pc.ConnConfig.RuntimeParams["application_name"] = "agenttwin"
	if cfg.Tracer != nil {
		pc.ConnConfig.Tracer = cfg.Tracer
	}
	attempts := cfg.StartupAttempts
	if attempts <= 0 {
		attempts = 10
	}
	backoff := 250 * time.Millisecond
	var lastErr error
	for i := 1; i <= attempts; i++ {
		pool, err := pgxpool.NewWithConfig(ctx, pc)
		if err == nil {
			pingCtx, cancel := context.WithTimeout(ctx, cfg.ConnectTimeout)
			err = pool.Ping(pingCtx)
			cancel()
			if err == nil {
				return pool, nil
			}
			pool.Close()
		}
		lastErr = err
		if log != nil {
			log.Warn("database not ready", "attempt", i, "error", err.Error())
		}
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(backoff):
		}
		if backoff < 5*time.Second {
			backoff *= 2
		}
	}
	return nil, fmt.Errorf("connect database after %d attempts: %w", attempts, lastErr)
}

// Querier is implemented by *pgxpool.Pool, *pgxpool.Conn and pgx.Tx.
type Querier interface {
	Exec(ctx context.Context, sql string, args ...any) (pgconn.CommandTag, error)
	Query(ctx context.Context, sql string, args ...any) (pgx.Rows, error)
	QueryRow(ctx context.Context, sql string, args ...any) pgx.Row
}

// WithTx runs fn in a transaction, committing on success and rolling back on
// error or panic.
func WithTx(ctx context.Context, pool *pgxpool.Pool, fn func(tx pgx.Tx) error) (err error) {
	tx, err := pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return fmt.Errorf("begin tx: %w", err)
	}
	defer func() {
		if p := recover(); p != nil {
			_ = tx.Rollback(context.Background())
			panic(p)
		}
		if err != nil {
			_ = tx.Rollback(context.Background())
		}
	}()
	if err = fn(tx); err != nil {
		return err
	}
	if err = tx.Commit(ctx); err != nil {
		return fmt.Errorf("commit: %w", err)
	}
	return nil
}

// IsUniqueViolation reports a unique-constraint violation (23505).
func IsUniqueViolation(err error) bool {
	var pgErr *pgconn.PgError
	return errors.As(err, &pgErr) && pgErr.Code == "23505"
}

// IsForeignKeyViolation reports a foreign-key violation (23503).
func IsForeignKeyViolation(err error) bool {
	var pgErr *pgconn.PgError
	return errors.As(err, &pgErr) && pgErr.Code == "23503"
}

// IsNoRows reports pgx.ErrNoRows.
func IsNoRows(err error) bool { return errors.Is(err, pgx.ErrNoRows) }

// Checker returns a readiness check pinging the pool.
func Checker(pool *pgxpool.Pool) func(ctx context.Context) error {
	return func(ctx context.Context) error { return pool.Ping(ctx) }
}

// MarkProcessed records that consumer handled eventID inside tx. It returns
// false when the event was already processed — the caller must then skip its
// side effects. This is what makes at-least-once delivery safe.
func MarkProcessed(ctx context.Context, tx pgx.Tx, table, consumer, eventID string) (bool, error) {
	tag, err := tx.Exec(ctx, "INSERT INTO "+pgx.Identifier{table}.Sanitize()+" (consumer, event_id) VALUES ($1, $2) ON CONFLICT DO NOTHING", consumer, eventID)
	if err != nil {
		return false, fmt.Errorf("mark processed: %w", err)
	}
	return tag.RowsAffected() == 1, nil
}
