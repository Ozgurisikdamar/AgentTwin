package migrations_test

import (
	"context"
	"testing"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/db"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/testutil"
	"github.com/Ozgurisikdamar/AgentTwin/services/control-plane/migrations"
)

// Every migration after the first can be rolled back and applied again: a
// release that has to be withdrawn leaves the schema as it found it.
func TestMigrationsRollBackAndReapply(t *testing.T) {
	ctx := context.Background()
	pool, err := db.Connect(ctx, nil, db.PoolConfig{URL: testutil.NewDatabase(t), Schema: migrations.Schema, MaxConns: 2})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(pool.Close)
	migs, err := db.LoadMigrations(migrations.FS, ".")
	if err != nil {
		t.Fatal(err)
	}
	m := &db.Migrator{Pool: pool, Schema: migrations.Schema, Migrations: migs}
	if _, err := m.Up(ctx); err != nil {
		t.Fatalf("up: %v", err)
	}
	constraints := func() int {
		var n int
		if err := pool.QueryRow(ctx, `SELECT count(*) FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace
			WHERE n.nspname = $1`, migrations.Schema).Scan(&n); err != nil {
			t.Fatal(err)
		}
		return n
	}
	after := constraints()
	later := len(migs) - 1
	if n, err := m.Down(ctx, later); err != nil || n != later {
		t.Fatalf("down %d: %d, %v", later, n, err)
	}
	var exists bool
	if err := pool.QueryRow(ctx, `SELECT to_regclass($1) IS NOT NULL`, migrations.Schema+".change_set").Scan(&exists); err != nil || exists {
		t.Fatalf("change_set after rollback: exists=%v err=%v", exists, err)
	}
	if n, err := m.Up(ctx); err != nil || n != later {
		t.Fatalf("up again: %d, %v", n, err)
	}
	if got := constraints(); got != after {
		t.Fatalf("constraints after reapplying = %d, want %d", got, after)
	}
}
