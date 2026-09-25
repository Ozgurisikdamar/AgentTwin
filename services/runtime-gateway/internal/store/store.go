// Package store keeps the runtime schema: tool endpoints, policies and their
// versions, decisions, approval requests and their attempts, idempotency
// records and the calls forwarded per trace (ADR-0033). Every read and write
// is scoped to one organization and project.
package store

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

// Store is the runtime schema.
type Store struct{ Pool *pgxpool.Pool }

// New returns a store on pool.
func New(pool *pgxpool.Pool) *Store { return &Store{Pool: pool} }

// Scope is the organization and project every row belongs to.
type Scope struct {
	OrgID     string
	ProjectID string
}

// Querier is a pool or a transaction.
type Querier interface {
	Exec(ctx context.Context, sql string, args ...any) (pgconn.CommandTag, error)
	Query(ctx context.Context, sql string, args ...any) (pgx.Rows, error)
	QueryRow(ctx context.Context, sql string, args ...any) pgx.Row
}

// ErrNotFound is returned for a row that does not exist in the scope.
var ErrNotFound = errors.New("not found")

// ErrConflict is returned when a unique key is already taken.
var ErrConflict = errors.New("conflict")

func isUnique(err error) bool {
	var pg *pgconn.PgError
	return errors.As(err, &pg) && pg.Code == "23505"
}

func nullable(s string) any {
	if s == "" {
		return nil
	}
	return s
}

func mustJSON(v any) []byte {
	b, err := json.Marshal(v)
	if err != nil {
		panic(fmt.Sprintf("store: marshal: %v", err))
	}
	return b
}

// ---------------------------------------------------------------- tool endpoints

// Endpoint is a tool the gateway may forward to.
type Endpoint struct {
	Tool           string    `json:"tool"`
	Kind           string    `json:"kind"`
	URL            string    `json:"url"`
	Risk           string    `json:"risk"`
	TimeoutMs      int       `json:"timeout_ms"`
	Idempotency    string    `json:"idempotency"`
	ForwardHeaders []string  `json:"forward_headers"`
	UpdatedBy      string    `json:"updated_by"`
	CreatedAt      time.Time `json:"created_at"`
	UpdatedAt      time.Time `json:"updated_at"`
}

const endpointCols = `tool, kind, url, risk, timeout_ms, idempotency, forward_headers, updated_by, created_at, updated_at`

func scanEndpoint(row pgx.Row) (Endpoint, error) {
	var e Endpoint
	err := row.Scan(&e.Tool, &e.Kind, &e.URL, &e.Risk, &e.TimeoutMs, &e.Idempotency, &e.ForwardHeaders,
		&e.UpdatedBy, &e.CreatedAt, &e.UpdatedAt)
	if e.ForwardHeaders == nil {
		e.ForwardHeaders = []string{}
	}
	return e, err
}

// Endpoint returns the tool's endpoint, or ErrNotFound.
func (s *Store) Endpoint(ctx context.Context, q Querier, sc Scope, tool string) (Endpoint, error) {
	e, err := scanEndpoint(q.QueryRow(ctx, `SELECT `+endpointCols+` FROM tool_endpoint
		WHERE organization_id = $1 AND project_id = $2 AND tool = $3`, sc.OrgID, sc.ProjectID, tool))
	if errors.Is(err, pgx.ErrNoRows) {
		return Endpoint{}, ErrNotFound
	}
	return e, err
}

// Endpoints lists the project's tool endpoints by tool.
func (s *Store) Endpoints(ctx context.Context, sc Scope) ([]Endpoint, error) {
	rows, err := s.Pool.Query(ctx, `SELECT `+endpointCols+` FROM tool_endpoint
		WHERE organization_id = $1 AND project_id = $2 ORDER BY tool`, sc.OrgID, sc.ProjectID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []Endpoint{}
	for rows.Next() {
		e, err := scanEndpoint(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, e)
	}
	return out, rows.Err()
}

// PutEndpoint creates or replaces the tool's endpoint and reports whether it
// was created.
func (s *Store) PutEndpoint(ctx context.Context, tx pgx.Tx, sc Scope, e Endpoint, now time.Time) (Endpoint, bool, error) {
	var created bool
	if e.ForwardHeaders == nil {
		e.ForwardHeaders = []string{}
	}
	out, err := scanEndpointCreated(tx.QueryRow(ctx, `INSERT INTO tool_endpoint (organization_id, project_id, tool, kind,
			url, risk, timeout_ms, idempotency, forward_headers, updated_by, created_at, updated_at)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $11)
		ON CONFLICT (organization_id, project_id, tool) DO UPDATE SET kind = EXCLUDED.kind, url = EXCLUDED.url,
			risk = EXCLUDED.risk, timeout_ms = EXCLUDED.timeout_ms, idempotency = EXCLUDED.idempotency,
			forward_headers = EXCLUDED.forward_headers, updated_by = EXCLUDED.updated_by, updated_at = EXCLUDED.updated_at
		RETURNING `+endpointCols+`, (xmax = 0)`,
		sc.OrgID, sc.ProjectID, e.Tool, e.Kind, e.URL, e.Risk, e.TimeoutMs, e.Idempotency, e.ForwardHeaders,
		e.UpdatedBy, now), &created)
	return out, created, err
}

func scanEndpointCreated(row pgx.Row, created *bool) (Endpoint, error) {
	var e Endpoint
	err := row.Scan(&e.Tool, &e.Kind, &e.URL, &e.Risk, &e.TimeoutMs, &e.Idempotency, &e.ForwardHeaders,
		&e.UpdatedBy, &e.CreatedAt, &e.UpdatedAt, created)
	if e.ForwardHeaders == nil {
		e.ForwardHeaders = []string{}
	}
	return e, err
}

// DeleteEndpoint removes the tool's endpoint, or returns ErrNotFound.
func (s *Store) DeleteEndpoint(ctx context.Context, tx pgx.Tx, sc Scope, tool string) (Endpoint, error) {
	e, err := scanEndpoint(tx.QueryRow(ctx, `DELETE FROM tool_endpoint
		WHERE organization_id = $1 AND project_id = $2 AND tool = $3 RETURNING `+endpointCols,
		sc.OrgID, sc.ProjectID, tool))
	if errors.Is(err, pgx.ErrNoRows) {
		return Endpoint{}, ErrNotFound
	}
	return e, err
}

// ---------------------------------------------------------------- locks

// Lock takes transaction-scoped advisory locks on keys, in the order given
// (callers keep one global order so two transactions cannot deadlock).
func (s *Store) Lock(ctx context.Context, tx pgx.Tx, keys ...string) error {
	for _, k := range keys {
		if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtextextended($1, 0))`, k); err != nil {
			return err
		}
	}
	return nil
}
