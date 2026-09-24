package events

import (
	"context"
	"errors"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	amqp "github.com/rabbitmq/amqp091-go"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/testutil"
)

func purge(t *testing.T, url string, queues ...string) {
	t.Helper()
	conn, err := amqp.Dial(url)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = conn.Close() }()
	ch, _ := conn.Channel()
	for _, q := range queues {
		_, _ = ch.QueuePurge(q, false)
	}
}

func dlqDepth(t *testing.T, url, queue string) int {
	t.Helper()
	conn, err := amqp.Dial(url)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = conn.Close() }()
	ch, _ := conn.Channel()
	q, err := ch.QueueDeclarePassive(queue, true, false, false, false, nil)
	if err != nil {
		t.Fatal(err)
	}
	return q.Messages
}

func TestBrokerPublishConsumeRetryAndDLQ(t *testing.T) {
	url := testutil.AMQPURL(t)
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	b, err := Dial(ctx, url, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = b.Close() }()
	const q = "simulation-service.events"
	purge(t, url, q, q+".retry", q+".dlq")

	var calls sync.Map
	var okCount atomic.Int32
	done := make(chan struct{}, 10)
	handler := func(_ context.Context, env Envelope) error {
		var p struct {
			RunID string `json:"run_id"`
		}
		_ = env.Decode(&p)
		n, _ := calls.LoadOrStore(p.RunID, new(atomic.Int32))
		c := n.(*atomic.Int32).Add(1)
		switch {
		case strings.HasPrefix(p.RunID, "0190f3b4-0000-7000-8000-00000000000a"): // flaky: fail first attempt
			if c == 1 {
				return errors.New("transient")
			}
		case strings.HasPrefix(p.RunID, "0190f3b4-0000-7000-8000-00000000000b"): // permanent
			return Permanent(errors.New("bad"))
		}
		okCount.Add(1)
		done <- struct{}{}
		return nil
	}
	cctx, ccancel := context.WithCancel(ctx)
	defer ccancel()
	go func() {
		_ = b.Consume(cctx, ConsumerConfig{Queue: q, Concurrency: 2, Timeout: 5 * time.Second}, handler)
	}()

	publish := func(runID string) {
		env, _ := New("simulation.run_requested.v1", "test", orgID, "", "c", "", map[string]any{"run_id": runID})
		if err := b.Publish(ctx, env); err != nil {
			t.Fatal(err)
		}
	}
	publish("0190f3b4-0000-7000-8000-000000000001")
	publish("0190f3b4-0000-7000-8000-00000000000a")
	publish("0190f3b4-0000-7000-8000-00000000000b")

	deadline := time.After(20 * time.Second)
	for okCount.Load() < 2 {
		select {
		case <-done:
		case <-deadline:
			t.Fatalf("timed out; ok=%d", okCount.Load())
		}
	}
	// Flaky message was delivered twice (retry through TTL queue).
	v, _ := calls.Load("0190f3b4-0000-7000-8000-00000000000a")
	if got := v.(*atomic.Int32).Load(); got != 2 {
		t.Fatalf("flaky handler calls = %d, want 2", got)
	}
	// Permanent failure parked without retry.
	time.Sleep(500 * time.Millisecond)
	if d := dlqDepth(t, url, q+".dlq"); d != 1 {
		t.Fatalf("dlq depth = %d, want 1", d)
	}
	v, _ = calls.Load("0190f3b4-0000-7000-8000-00000000000b")
	if got := v.(*atomic.Int32).Load(); got != 1 {
		t.Fatalf("permanent failure retried %d times", got)
	}

	// Poison message (invalid payload) goes straight to DLQ without calling the handler.
	conn, _ := amqp.Dial(url)
	ch, _ := conn.Channel()
	_ = ch.PublishWithContext(ctx, Exchange, "simulation.run_requested.v1", false, false, amqp.Publishing{Body: []byte(`{"garbage":true}`)})
	_ = conn.Close()
	time.Sleep(1500 * time.Millisecond)
	if d := dlqDepth(t, url, q+".dlq"); d != 2 {
		t.Fatalf("poison message not parked: dlq depth = %d", d)
	}
}

func TestPublishRefusesInvalidEvent(t *testing.T) {
	url := testutil.AMQPURL(t)
	ctx := context.Background()
	b, err := Dial(ctx, url, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = b.Close() }()
	env, _ := New("simulation.run_requested.v1", "test", orgID, "", "c", "", map[string]any{"run_id": "not-a-uuid"})
	if err := b.Publish(ctx, env); err == nil {
		t.Fatal("invalid payload must not be published")
	}
}
