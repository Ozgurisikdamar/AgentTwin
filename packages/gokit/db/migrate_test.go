package db_test

import (
	"context"
	"strings"
	"testing"
	"testing/fstest"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/db"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/testutil"
)

func TestLoadMigrationsValidatesNames(t *testing.T) {
	fsys := fstest.MapFS{
		"m/0001_init.up.sql":   {Data: []byte("CREATE TABLE a(id int);")},
		"m/0001_init.down.sql": {Data: []byte("DROP TABLE a;")},
		"m/0002_more.up.sql":   {Data: []byte("CREATE TABLE b(id int);")},
		"m/README.md":          {Data: []byte("ignored")},
	}
	migs, err := db.LoadMigrations(fsys, "m")
	if err != nil {
		t.Fatal(err)
	}
	if len(migs) != 2 || migs[0].Version != "0001" || migs[1].Version != "0002" || migs[0].Down == "" {
		t.Fatalf("unexpected migrations: %+v", migs)
	}
	bad := fstest.MapFS{"m/1_bad.sql": {Data: []byte("x")}}
	if _, err := db.LoadMigrations(bad, "m"); err == nil {
		t.Fatal("badly named migration must be rejected")
	}
	noUp := fstest.MapFS{"m/0001_x.down.sql": {Data: []byte("x")}}
	if _, err := db.LoadMigrations(noUp, "m"); err == nil {
		t.Fatal("migration without up must be rejected")
	}
}

func TestMigratorUpDownAndChecksum(t *testing.T) {
	url := testutil.NewDatabase(t)
	ctx := context.Background()
	pool, err := db.Connect(ctx, nil, db.PoolConfig{URL: url})
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	fsys := fstest.MapFS{
		"m/0001_init.up.sql":   {Data: []byte("CREATE TABLE widget(id int PRIMARY KEY);")},
		"m/0001_init.down.sql": {Data: []byte("DROP TABLE widget;")},
		"m/0002_col.up.sql":    {Data: []byte("ALTER TABLE widget ADD COLUMN name text;")},
		"m/0002_col.down.sql":  {Data: []byte("ALTER TABLE widget DROP COLUMN name;")},
	}
	migs, err := db.LoadMigrations(fsys, "m")
	if err != nil {
		t.Fatal(err)
	}
	m := &db.Migrator{Pool: pool, Schema: "testschema", Migrations: migs}
	n, err := m.Up(ctx)
	if err != nil || n != 2 {
		t.Fatalf("up: n=%d err=%v", n, err)
	}
	// Idempotent.
	if n, err := m.Up(ctx); err != nil || n != 0 {
		t.Fatalf("second up: n=%d err=%v", n, err)
	}
	if _, err := pool.Exec(ctx, "INSERT INTO testschema.widget(id,name) VALUES (1,'a')"); err != nil {
		t.Fatalf("table not usable: %v", err)
	}
	if n, err := m.Down(ctx, 1); err != nil || n != 1 {
		t.Fatalf("down: n=%d err=%v", n, err)
	}
	pending, err := m.Pending(ctx)
	if err != nil || len(pending) != 1 || pending[0] != "0002_col" {
		t.Fatalf("pending=%v err=%v", pending, err)
	}
	if _, err := m.Up(ctx); err != nil {
		t.Fatal(err)
	}
	// Editing an applied migration must be detected.
	migs[0].Checksum = "tampered"
	if _, err := m.Up(ctx); err == nil || !strings.Contains(err.Error(), "checksum") {
		t.Fatalf("expected checksum error, got %v", err)
	}
}
