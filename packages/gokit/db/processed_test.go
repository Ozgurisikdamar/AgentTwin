package db_test

import (
	"context"
	"errors"
	"sync"
	"testing"

	"github.com/jackc/pgx/v5"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/db"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/ids"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/testutil"
)

// MarkProcessed is what makes at-least-once delivery safe (spec §64): an
// event is processed once per consumer, whichever delivery comes first,
// also when deliveries race, and a delivery whose transaction fails leaves
// the event to the next one.
func TestMarkProcessedOncePerConsumer(t *testing.T) {
	ctx := context.Background()
	pool, err := db.Connect(ctx, nil, db.PoolConfig{URL: testutil.NewDatabase(t), MaxConns: 10})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(pool.Close)
	if _, err := pool.Exec(ctx, `CREATE SCHEMA svc; CREATE TABLE svc.processed_event (
		consumer text NOT NULL, event_id uuid NOT NULL, processed_at timestamptz NOT NULL DEFAULT now(),
		PRIMARY KEY (consumer, event_id))`); err != nil {
		t.Fatal(err)
	}
	try := func(consumer, id string) (fresh bool, err error) {
		err = pgx.BeginFunc(ctx, pool, func(tx pgx.Tx) error {
			fresh, err = db.MarkProcessed(ctx, tx, "svc", consumer, id)
			return err
		})
		return fresh, err
	}
	mark := func(consumer, id string) bool {
		t.Helper()
		fresh, err := try(consumer, id)
		if err != nil {
			t.Fatal(err)
		}
		return fresh
	}
	id := ids.New()
	if !mark("graph", id) || mark("graph", id) {
		t.Error("the first delivery must be fresh and the second a duplicate")
	}
	if !mark("miner", id) {
		t.Error("another consumer processes the same event on its own")
	}

	// A delivery whose side effects fail rolls its mark back with them.
	failed := ids.New()
	boom := errors.New("side effect failed")
	err = pgx.BeginFunc(ctx, pool, func(tx pgx.Tx) error {
		if fresh, err := db.MarkProcessed(ctx, tx, "svc", "graph", failed); err != nil || !fresh {
			t.Fatalf("fresh=%v err=%v", fresh, err)
		}
		return boom
	})
	if !errors.Is(err, boom) || !mark("graph", failed) {
		t.Error("a rolled-back delivery must leave the event to the next one")
	}

	// Racing deliveries: exactly one is fresh.
	racing := ids.New()
	var wg sync.WaitGroup
	var mu sync.Mutex
	fresh := 0
	for range 10 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			ok, err := try("graph", racing)
			mu.Lock()
			defer mu.Unlock()
			if err != nil {
				t.Errorf("a racing delivery failed: %v", err)
			} else if ok {
				fresh++
			}
		}()
	}
	wg.Wait()
	if fresh != 1 {
		t.Errorf("%d racing deliveries were fresh, want 1", fresh)
	}

	// Not an event id: the error surfaces, it is not taken for a duplicate.
	var markErr error
	_ = pgx.BeginFunc(ctx, pool, func(tx pgx.Tx) error {
		_, markErr = db.MarkProcessed(ctx, tx, "svc", "graph", "not-a-uuid")
		return markErr
	})
	if markErr == nil {
		t.Error("an invalid event id must be an error")
	}
}
