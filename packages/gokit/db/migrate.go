package db

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io/fs"
	"log/slog"
	"regexp"
	"sort"
	"strings"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

var migrationName = regexp.MustCompile(`^(\d{4})_([a-z0-9_]+)\.(up|down)\.sql$`)

// Migration is one versioned schema change.
type Migration struct {
	Version  string
	Name     string
	Up       string
	Down     string
	Checksum string
}

// LoadMigrations reads NNNN_name.up.sql / NNNN_name.down.sql files from fsys.
func LoadMigrations(fsys fs.FS, dir string) ([]Migration, error) {
	entries, err := fs.ReadDir(fsys, dir)
	if err != nil {
		return nil, fmt.Errorf("read migrations: %w", err)
	}
	byVersion := map[string]*Migration{}
	for _, e := range entries {
		m := migrationName.FindStringSubmatch(e.Name())
		if m == nil {
			if strings.HasSuffix(e.Name(), ".sql") {
				return nil, fmt.Errorf("migration %q does not match NNNN_name.(up|down).sql", e.Name())
			}
			continue
		}
		b, err := fs.ReadFile(fsys, dir+"/"+e.Name())
		if err != nil {
			return nil, err
		}
		mig := byVersion[m[1]]
		if mig == nil {
			mig = &Migration{Version: m[1], Name: m[2]}
			byVersion[m[1]] = mig
		} else if mig.Name != m[2] {
			return nil, fmt.Errorf("migration %s has conflicting names %q and %q", m[1], mig.Name, m[2])
		}
		if m[3] == "up" {
			mig.Up = string(b)
		} else {
			mig.Down = string(b)
		}
	}
	out := make([]Migration, 0, len(byVersion))
	for _, m := range byVersion {
		if m.Up == "" {
			return nil, fmt.Errorf("migration %s_%s has no up script", m.Version, m.Name)
		}
		sum := sha256.Sum256([]byte(m.Up))
		m.Checksum = hex.EncodeToString(sum[:])
		out = append(out, *m)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Version < out[j].Version })
	return out, nil
}

// Migrator applies migrations for one schema under an advisory lock so that
// concurrently starting replicas do not race.
type Migrator struct {
	Pool       *pgxpool.Pool
	Schema     string
	Migrations []Migration
	Log        *slog.Logger
}

func (m *Migrator) ledger() string {
	return pgx.Identifier{m.Schema, "schema_migrations"}.Sanitize()
}

func (m *Migrator) withLock(ctx context.Context, fn func(conn *pgxpool.Conn) error) error {
	conn, err := m.Pool.Acquire(ctx)
	if err != nil {
		return fmt.Errorf("acquire conn: %w", err)
	}
	defer conn.Release()
	if _, err := conn.Exec(ctx, "SELECT pg_advisory_lock(hashtext($1))", "agenttwin-migrate-"+m.Schema); err != nil {
		return fmt.Errorf("advisory lock: %w", err)
	}
	defer func() {
		_, _ = conn.Exec(context.Background(), "SELECT pg_advisory_unlock(hashtext($1))", "agenttwin-migrate-"+m.Schema)
	}()
	if _, err := conn.Exec(ctx, "CREATE SCHEMA IF NOT EXISTS "+pgx.Identifier{m.Schema}.Sanitize()); err != nil {
		return fmt.Errorf("create schema: %w", err)
	}
	if _, err := conn.Exec(ctx, "CREATE TABLE IF NOT EXISTS "+m.ledger()+` (
		version text PRIMARY KEY,
		name text NOT NULL,
		checksum text NOT NULL,
		applied_at timestamptz NOT NULL DEFAULT now())`); err != nil {
		return fmt.Errorf("create ledger: %w", err)
	}
	return fn(conn)
}

func (m *Migrator) applied(ctx context.Context, conn *pgxpool.Conn) (map[string]string, error) {
	rows, err := conn.Query(ctx, "SELECT version, checksum FROM "+m.ledger())
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[string]string{}
	for rows.Next() {
		var v, c string
		if err := rows.Scan(&v, &c); err != nil {
			return nil, err
		}
		out[v] = c
	}
	return out, rows.Err()
}

// Up applies all pending migrations. An already-applied migration whose
// content changed is an error: history must not be rewritten silently.
func (m *Migrator) Up(ctx context.Context) (int, error) {
	count := 0
	err := m.withLock(ctx, func(conn *pgxpool.Conn) error {
		done, err := m.applied(ctx, conn)
		if err != nil {
			return err
		}
		for _, mig := range m.Migrations {
			if sum, ok := done[mig.Version]; ok {
				if sum != mig.Checksum {
					return fmt.Errorf("migration %s_%s was modified after being applied (checksum mismatch)", mig.Version, mig.Name)
				}
				continue
			}
			tx, err := conn.Begin(ctx)
			if err != nil {
				return err
			}
			if _, err := tx.Exec(ctx, "SET LOCAL search_path TO "+pgx.Identifier{m.Schema}.Sanitize()+", public"); err != nil {
				_ = tx.Rollback(ctx)
				return err
			}
			if _, err := tx.Exec(ctx, mig.Up); err != nil {
				_ = tx.Rollback(ctx)
				return fmt.Errorf("apply %s_%s: %w", mig.Version, mig.Name, err)
			}
			if _, err := tx.Exec(ctx, "INSERT INTO "+m.ledger()+" (version, name, checksum) VALUES ($1,$2,$3)", mig.Version, mig.Name, mig.Checksum); err != nil {
				_ = tx.Rollback(ctx)
				return err
			}
			if err := tx.Commit(ctx); err != nil {
				return err
			}
			count++
			if m.Log != nil {
				m.Log.Info("migration applied", "schema", m.Schema, "version", mig.Version, "name", mig.Name)
			}
		}
		return nil
	})
	return count, err
}

// Down rolls back the last n applied migrations using their down scripts.
func (m *Migrator) Down(ctx context.Context, n int) (int, error) {
	count := 0
	err := m.withLock(ctx, func(conn *pgxpool.Conn) error {
		done, err := m.applied(ctx, conn)
		if err != nil {
			return err
		}
		for i := len(m.Migrations) - 1; i >= 0 && count < n; i-- {
			mig := m.Migrations[i]
			if _, ok := done[mig.Version]; !ok {
				continue
			}
			if mig.Down == "" {
				return fmt.Errorf("migration %s_%s has no down script", mig.Version, mig.Name)
			}
			tx, err := conn.Begin(ctx)
			if err != nil {
				return err
			}
			if _, err := tx.Exec(ctx, "SET LOCAL search_path TO "+pgx.Identifier{m.Schema}.Sanitize()+", public"); err != nil {
				_ = tx.Rollback(ctx)
				return err
			}
			if _, err := tx.Exec(ctx, mig.Down); err != nil {
				_ = tx.Rollback(ctx)
				return fmt.Errorf("revert %s_%s: %w", mig.Version, mig.Name, err)
			}
			if _, err := tx.Exec(ctx, "DELETE FROM "+m.ledger()+" WHERE version=$1", mig.Version); err != nil {
				_ = tx.Rollback(ctx)
				return err
			}
			if err := tx.Commit(ctx); err != nil {
				return err
			}
			count++
		}
		return nil
	})
	return count, err
}

// Pending returns the versions not yet applied (for readiness / doctor).
func (m *Migrator) Pending(ctx context.Context) ([]string, error) {
	var pending []string
	err := m.withLock(ctx, func(conn *pgxpool.Conn) error {
		done, err := m.applied(ctx, conn)
		if err != nil {
			return err
		}
		for _, mig := range m.Migrations {
			if _, ok := done[mig.Version]; !ok {
				pending = append(pending, mig.Version+"_"+mig.Name)
			}
		}
		return nil
	})
	return pending, err
}
