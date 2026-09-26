package events

import (
	"context"
	"errors"
	"net/url"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/db"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/ids"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/testutil"
)

// Chaos tests (spec §64): RabbitMQ goes away while a service publishes
// through its outbox and consumes. The server keeps running behind a
// testutil.CutProxy, so what it holds can be checked during the outage.

func hostPort(t *testing.T, raw string) string {
	t.Helper()
	u, err := url.Parse(raw)
	if err != nil {
		t.Fatal(err)
	}
	return u.Host
}

// eventually polls cond until it holds or the deadline passes.
func eventually(t *testing.T, within time.Duration, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(within)
	for !cond() {
		if time.Now().After(deadline) {
			t.Fatalf("timed out after %v waiting for %s", within, what)
		}
		time.Sleep(50 * time.Millisecond)
	}
}

func outboxPool(t *testing.T) *pgxpool.Pool {
	t.Helper()
	ctx := context.Background()
	pool, err := db.Connect(ctx, nil, db.PoolConfig{URL: testutil.NewDatabase(t)})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(pool.Close)
	if _, err := pool.Exec(ctx, "CREATE SCHEMA svc; SET search_path TO svc;"+OutboxDDL); err != nil {
		t.Fatal(err)
	}
	return pool
}

// received records every event a consumer handled, by id.
type received struct {
	mu   sync.Mutex
	seen map[string]int
}

func (r *received) handle(_ context.Context, env Envelope) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.seen[env.ID]++
	return nil
}

func (r *received) count(id string) int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.seen[id]
}

func (r *received) all(ids []string) bool {
	for _, id := range ids {
		if r.count(id) == 0 {
			return false
		}
	}
	return true
}

func TestARabbitMQOutageDelaysEventsAndLosesNone(t *testing.T) {
	amqpURL := testutil.AMQPURL(t)
	pool := outboxPool(t)
	proxy := testutil.NewCutProxy(t, hostPort(t, amqpURL))
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()

	const q = "simulation-service.events"
	purge(t, amqpURL, q, q+".retry", q+".dlq")
	b, err := Dial(ctx, proxy.URL(t, amqpURL), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = b.Close() }()

	got := &received{seen: map[string]int{}}
	cctx, stop := context.WithCancel(ctx)
	consumed := make(chan struct{})
	go func() {
		defer close(consumed)
		_ = b.Consume(cctx, ConsumerConfig{Queue: q, Concurrency: 4, Timeout: 5 * time.Second}, got.handle)
	}()
	defer func() { stop(); <-consumed }()

	var failedTicks int
	relay := &OutboxRelay{Pool: pool, Schema: "svc", Publisher: b}
	write := func(n int) []string {
		written := make([]string, 0, n)
		for i := 0; i < n; i++ {
			env, err := New("simulation.run_requested.v1", "chaos", orgID, "", "c", "", map[string]any{"run_id": ids.New()})
			if err != nil {
				t.Fatal(err)
			}
			if err := db.WithTx(ctx, pool, func(tx pgx.Tx) error { return WriteOutbox(ctx, tx, "svc", env) }); err != nil {
				t.Fatal(err)
			}
			written = append(written, env.ID)
		}
		return written
	}
	backlog := func() int64 {
		n, err := Backlog(ctx, pool, "svc")
		if err != nil {
			t.Fatal(err)
		}
		return n
	}
	drain := func() {
		eventually(t, 30*time.Second, "the outbox to drain", func() bool {
			if _, err := relay.Tick(ctx); err != nil {
				failedTicks++
			}
			return backlog() == 0
		})
	}

	before := write(5)
	drain()
	eventually(t, 30*time.Second, "the events before the outage", func() bool { return got.all(before) })

	// RabbitMQ goes away.
	proxy.Cut()
	eventually(t, 10*time.Second, "readiness to report the outage", func() bool { return b.Ping(ctx) != nil })
	during := write(5)
	for i := 0; i < 3; i++ {
		if n, err := relay.Tick(ctx); err == nil || n != 0 {
			t.Fatalf("a tick during the outage published %d events (err %v)", n, err)
		}
	}
	// Nothing is lost or marked published; the failure is on the row.
	if n := backlog(); n != int64(len(during)) {
		t.Fatalf("backlog during the outage = %d, want %d", n, len(during))
	}
	var withError int
	if err := pool.QueryRow(ctx, "SELECT count(*) FROM svc.outbox WHERE published_at IS NULL AND last_error IS NOT NULL AND attempts >= 3").Scan(&withError); err != nil {
		t.Fatal(err)
	}
	if withError != 1 {
		t.Fatalf("rows showing the failed attempts = %d, want the head of the queue", withError)
	}
	for _, id := range during {
		if got.count(id) != 0 {
			t.Fatalf("event %s was consumed during the outage", id)
		}
	}

	// It comes back: the relay publishes the backlog and the consumer, which
	// reconnects on its own, receives every event.
	proxy.Restore()
	eventually(t, 15*time.Second, "readiness to recover", func() bool { return b.Ping(ctx) == nil })
	drain()
	eventually(t, 30*time.Second, "the events written during the outage", func() bool { return got.all(during) })
	for _, id := range append(before, during...) {
		if n := got.count(id); n < 1 {
			t.Fatalf("event %s lost", id)
		}
	}
	if d := dlqDepth(t, amqpURL, q+".dlq"); d != 0 {
		t.Fatalf("dlq depth after the outage = %d", d)
	}
	var published int
	if err := pool.QueryRow(ctx, "SELECT count(*) FROM svc.outbox WHERE published_at IS NOT NULL AND last_error IS NULL").Scan(&published); err != nil {
		t.Fatal(err)
	}
	if published != len(before)+len(during) {
		t.Fatalf("rows published cleanly = %d, want %d", published, len(before)+len(during))
	}
}

func TestDialWaitsForABrokerThatIsNotUpYetAndGivesUpWhenTold(t *testing.T) {
	amqpURL := testutil.AMQPURL(t)
	proxy := testutil.NewCutProxy(t, hostPort(t, amqpURL))
	proxy.Cut()

	// Bounded: a caller that stops waiting gets an error, not a hang.
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	start := time.Now()
	_, err := Dial(ctx, proxy.URL(t, amqpURL), nil)
	cancel()
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("dial while RabbitMQ is down: %v, want the caller's deadline", err)
	}
	if time.Since(start) > 3*time.Second {
		t.Fatalf("dial took %v after its caller gave up", time.Since(start))
	}

	// A broker that comes up while a service starts is picked up.
	time.AfterFunc(1500*time.Millisecond, proxy.Restore)
	ctx, cancel = context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	b, err := Dial(ctx, proxy.URL(t, amqpURL), nil)
	if err != nil {
		t.Fatalf("dial after RabbitMQ came up: %v", err)
	}
	defer func() { _ = b.Close() }()
	if err := b.Ping(ctx); err != nil {
		t.Fatal(err)
	}
}

func TestReadinessDuringAPartitionIsBoundedAndReconnectsOnce(t *testing.T) {
	amqpURL := testutil.AMQPURL(t)
	proxy := testutil.NewCutProxy(t, hostPort(t, amqpURL))
	ctx := context.Background()
	b, err := Dial(ctx, proxy.URL(t, amqpURL), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = b.Close() }()

	// A partition: connections hang instead of failing. The readiness probe
	// answers within its own deadline instead of waiting for a dial timeout.
	proxy.Silence()
	eventually(t, 10*time.Second, "the connection loss to be noticed", func() bool { return b.conn.IsClosed() })
	pctx, cancel := context.WithTimeout(ctx, 500*time.Millisecond)
	start := time.Now()
	err = b.Ping(pctx)
	cancel()
	if err == nil {
		t.Fatal("readiness reported a partitioned broker as ready")
	}
	if took := time.Since(start); took > 2*time.Second {
		t.Fatalf("readiness took %v during a partition", took)
	}

	// Many probes at once after recovery open one connection, not one each.
	proxy.Restore()
	var wg sync.WaitGroup
	errs := make(chan error, 20)
	for i := 0; i < 20; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			pctx, cancel := context.WithTimeout(ctx, 10*time.Second)
			defer cancel()
			errs <- b.Ping(pctx)
		}()
	}
	wg.Wait()
	close(errs)
	ok := 0
	for err := range errs {
		if err == nil {
			ok++
		}
	}
	if ok == 0 {
		t.Fatal("no probe saw the broker recover")
	}
	eventually(t, 5*time.Second, "one connection to RabbitMQ", func() bool { return proxy.Open() == 1 })
	time.Sleep(500 * time.Millisecond)
	if n := proxy.Open(); n != 1 {
		t.Fatalf("connections to RabbitMQ after concurrent reconnects = %d, want 1", n)
	}
	if err := b.Ping(ctx); err != nil {
		t.Fatal(err)
	}
}

func TestAClosedBrokerStaysClosed(t *testing.T) {
	amqpURL := testutil.AMQPURL(t)
	proxy := testutil.NewCutProxy(t, hostPort(t, amqpURL))
	ctx := context.Background()
	b, err := Dial(ctx, proxy.URL(t, amqpURL), nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := b.Close(); err != nil {
		t.Fatal(err)
	}
	dials := proxy.Accepted()
	// Shutdown closes the connection; nothing afterwards dials RabbitMQ again.
	env, _ := New("simulation.run_requested.v1", "chaos", orgID, "", "c", "", map[string]any{"run_id": ids.New()})
	if err := b.Publish(ctx, env); err == nil {
		t.Fatal("a closed broker published")
	}
	if err := b.Ping(ctx); err == nil {
		t.Fatal("a closed broker reported ready")
	}
	eventually(t, 5*time.Second, "the connection to close", func() bool { return proxy.Open() == 0 })
	time.Sleep(300 * time.Millisecond)
	if n := proxy.Open(); n != 0 {
		t.Fatalf("a closed broker has %d connections", n)
	}
	if n := proxy.Accepted() - dials; n != 0 {
		t.Fatalf("a closed broker dialed RabbitMQ %d times", n)
	}
}
