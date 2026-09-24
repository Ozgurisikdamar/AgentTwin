package events

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// OutboxDDL creates the outbox table in a service schema. Services include it
// in their initial migration (the table name is always "<schema>.outbox").
const OutboxDDL = `
CREATE TABLE IF NOT EXISTS outbox (
    id uuid PRIMARY KEY,
    event_type text NOT NULL,
    envelope jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz,
    attempts int NOT NULL DEFAULT 0,
    last_error text
);
CREATE INDEX IF NOT EXISTS outbox_unpublished_idx ON outbox (created_at) WHERE published_at IS NULL;
`

// Publisher is implemented by *Broker (and fakes in tests).
type Publisher interface {
	Publish(ctx context.Context, env Envelope) error
}

// WriteOutbox stores env in the outbox inside the caller's transaction, so the
// event is published if and only if the state change commits.
func WriteOutbox(ctx context.Context, tx pgx.Tx, schema string, env Envelope) error {
	v, err := DefaultValidator()
	if err != nil {
		return err
	}
	if err := v.Validate(env); err != nil {
		return fmt.Errorf("outbox: %w", err)
	}
	raw, err := json.Marshal(env)
	if err != nil {
		return err
	}
	table := pgx.Identifier{schema, "outbox"}.Sanitize()
	_, err = tx.Exec(ctx, "INSERT INTO "+table+" (id, event_type, envelope) VALUES ($1, $2, $3)", env.ID, env.Type, raw)
	if err != nil {
		return fmt.Errorf("outbox insert: %w", err)
	}
	return nil
}

// OutboxRelay publishes committed outbox rows with publisher confirms.
type OutboxRelay struct {
	Pool      *pgxpool.Pool
	Schema    string
	Publisher Publisher
	Log       *slog.Logger
	Interval  time.Duration
	BatchSize int
	// Observe receives the number of published rows per tick (metrics).
	Observe func(published int, failed bool)
}

// Run polls until ctx is cancelled.
func (r *OutboxRelay) Run(ctx context.Context) {
	interval := r.Interval
	if interval <= 0 {
		interval = 300 * time.Millisecond
	}
	t := time.NewTicker(interval)
	defer t.Stop()
	for {
		n, err := r.Tick(ctx)
		if r.Observe != nil {
			r.Observe(n, err != nil)
		}
		if err != nil && ctx.Err() == nil && r.Log != nil {
			r.Log.Warn("outbox relay tick failed", "schema", r.Schema, "error", err.Error())
		}
		if n > 0 && err == nil {
			continue // drain quickly while there is backlog
		}
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		}
	}
}

// Tick publishes one batch. Rows are locked with SKIP LOCKED so several relay
// replicas can run concurrently without double-publishing within a tick; a
// crash after publish but before commit republishes, which idempotent
// consumers tolerate.
func (r *OutboxRelay) Tick(ctx context.Context) (int, error) {
	batch := r.BatchSize
	if batch <= 0 {
		batch = 50
	}
	table := pgx.Identifier{r.Schema, "outbox"}.Sanitize()
	tx, err := r.Pool.Begin(ctx)
	if err != nil {
		return 0, err
	}
	defer func() { _ = tx.Rollback(context.Background()) }()
	rows, err := tx.Query(ctx, "SELECT id, envelope FROM "+table+" WHERE published_at IS NULL ORDER BY created_at LIMIT $1 FOR UPDATE SKIP LOCKED", batch)
	if err != nil {
		return 0, err
	}
	type item struct {
		id  string
		raw []byte
	}
	var items []item
	for rows.Next() {
		var it item
		if err := rows.Scan(&it.id, &it.raw); err != nil {
			rows.Close()
			return 0, err
		}
		items = append(items, it)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return 0, err
	}
	published := 0
	var pubErr error
	for _, it := range items {
		var env Envelope
		if err := json.Unmarshal(it.raw, &env); err != nil {
			pubErr = err
			_, _ = tx.Exec(ctx, "UPDATE "+table+" SET attempts = attempts + 1, last_error = $2 WHERE id = $1", it.id, err.Error())
			break
		}
		if err := r.Publisher.Publish(ctx, env); err != nil {
			pubErr = err
			_, _ = tx.Exec(ctx, "UPDATE "+table+" SET attempts = attempts + 1, last_error = $2 WHERE id = $1", it.id, truncate(err.Error(), 1000))
			break
		}
		if _, err := tx.Exec(ctx, "UPDATE "+table+" SET published_at = now(), attempts = attempts + 1, last_error = NULL WHERE id = $1", it.id); err != nil {
			return published, err
		}
		published++
	}
	if err := tx.Commit(ctx); err != nil {
		return 0, err
	}
	return published, pubErr
}

// Backlog returns the number of unpublished outbox rows (metrics, doctor).
func Backlog(ctx context.Context, pool *pgxpool.Pool, schema string) (int64, error) {
	var n int64
	err := pool.QueryRow(ctx, "SELECT count(*) FROM "+pgx.Identifier{schema, "outbox"}.Sanitize()+" WHERE published_at IS NULL").Scan(&n)
	return n, err
}
