// Package store is the control-plane's PostgreSQL persistence layer. All SQL
// of the service lives here; every tenant-owned query is scoped by
// organization_id.
package store

import (
	"context"
	"errors"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/db"
)

// ErrNotFound is returned when a scoped lookup finds nothing.
var ErrNotFound = errors.New("not found")

// ErrConflict is returned on unique-constraint violations.
var ErrConflict = errors.New("conflict")

// Store wraps the connection pool.
type Store struct {
	Pool *pgxpool.Pool
	Now  func() time.Time
}

// New creates a Store.
func New(pool *pgxpool.Pool) *Store {
	return &Store{Pool: pool, Now: func() time.Time { return time.Now().UTC() }}
}

// Tx runs fn in a transaction.
func (s *Store) Tx(ctx context.Context, fn func(tx pgx.Tx) error) error {
	return db.WithTx(ctx, s.Pool, fn)
}

func mapErr(err error) error {
	switch {
	case err == nil:
		return nil
	case db.IsNoRows(err):
		return ErrNotFound
	case db.IsUniqueViolation(err):
		return ErrConflict
	}
	return err
}
