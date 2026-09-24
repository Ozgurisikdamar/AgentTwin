package events

import (
	"context"
	"errors"
	"sync"
	"testing"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/db"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/testutil"
	"github.com/jackc/pgx/v5"
)

type fakePublisher struct {
	mu   sync.Mutex
	got  []Envelope
	fail bool
}

func (f *fakePublisher) Publish(_ context.Context, env Envelope) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.fail {
		return errors.New("broker down")
	}
	f.got = append(f.got, env)
	return nil
}

func TestOutboxRelayPublishesOnlyCommittedRows(t *testing.T) {
	url := testutil.NewDatabase(t)
	ctx := context.Background()
	pool, err := db.Connect(ctx, nil, db.PoolConfig{URL: url})
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	if _, err := pool.Exec(ctx, "CREATE SCHEMA svc; SET search_path TO svc;"+OutboxDDL); err != nil {
		t.Fatal(err)
	}
	mk := func(run string) Envelope {
		env, _ := New("simulation.run_requested.v1", "test", orgID, "", "c", "", map[string]any{"run_id": run})
		return env
	}
	// Committed.
	if err := db.WithTx(ctx, pool, func(tx pgx.Tx) error {
		return WriteOutbox(ctx, tx, "svc", mk("0190f3b4-0000-7000-8000-000000000001"))
	}); err != nil {
		t.Fatal(err)
	}
	// Rolled back: must never be published.
	_ = db.WithTx(ctx, pool, func(tx pgx.Tx) error {
		_ = WriteOutbox(ctx, tx, "svc", mk("0190f3b4-0000-7000-8000-000000000002"))
		return errors.New("business failure")
	})
	// Invalid envelope is refused at write time.
	if err := db.WithTx(ctx, pool, func(tx pgx.Tx) error {
		return WriteOutbox(ctx, tx, "svc", mk("not-a-uuid"))
	}); err == nil {
		t.Fatal("invalid event must be refused by the outbox")
	}

	pub := &fakePublisher{fail: true}
	relay := &OutboxRelay{Pool: pool, Schema: "svc", Publisher: pub}
	if n, err := relay.Tick(ctx); err == nil || n != 0 {
		t.Fatalf("failing publisher: n=%d err=%v", n, err)
	}
	backlog, _ := Backlog(ctx, pool, "svc")
	if backlog != 1 {
		t.Fatalf("backlog after failure = %d", backlog)
	}
	pub.fail = false
	if n, err := relay.Tick(ctx); err != nil || n != 1 {
		t.Fatalf("relay tick: n=%d err=%v", n, err)
	}
	if len(pub.got) != 1 {
		t.Fatalf("published %d events", len(pub.got))
	}
	backlog, _ = Backlog(ctx, pool, "svc")
	if backlog != 0 {
		t.Fatalf("backlog after publish = %d", backlog)
	}
	var attempts int
	_ = pool.QueryRow(ctx, "SELECT attempts FROM svc.outbox").Scan(&attempts)
	if attempts != 2 {
		t.Fatalf("attempts = %d, want 2", attempts)
	}
}
