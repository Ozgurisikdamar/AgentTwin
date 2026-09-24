package events

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"math"
	"sort"
	"sync"
	"time"

	amqp "github.com/rabbitmq/amqp091-go"

	"github.com/Ozgurisikdamar/AgentTwin/packages/contracts"
)

// Topology mirrors packages/contracts/topology.json.
type Topology struct {
	Exchange    string              `json:"exchange"`
	MaxAttempts int                 `json:"max_attempts"`
	RetryTTLMS  int                 `json:"retry_ttl_ms"`
	Queues      map[string][]string `json:"queues"`
}

// LoadTopology parses the shared topology contract.
func LoadTopology() (Topology, error) {
	var t Topology
	if err := json.Unmarshal(contracts.Topology, &t); err != nil {
		return t, fmt.Errorf("parse topology: %w", err)
	}
	if t.Exchange == "" || t.MaxAttempts < 1 || len(t.Queues) == 0 {
		return t, errors.New("topology is incomplete")
	}
	return t, nil
}

// attemptHeader counts deliveries of a message to its consumer queue.
const attemptHeader = "x-agenttwin-attempt"

// Broker owns one AMQP connection with a confirm-mode publishing channel and
// reconnects when the connection drops.
type Broker struct {
	url      string
	log      *slog.Logger
	topology Topology
	validate *Validator

	mu     sync.Mutex
	conn   *amqp.Connection
	pubCh  *amqp.Channel
	closed bool
}

// Dial connects (retrying with backoff) and declares the complete topology.
// Declaring every queue from every service means no event can be published
// before its consumer queue exists, regardless of container start order.
func Dial(ctx context.Context, url string, log *slog.Logger) (*Broker, error) {
	topo, err := LoadTopology()
	if err != nil {
		return nil, err
	}
	v, err := DefaultValidator()
	if err != nil {
		return nil, err
	}
	b := &Broker{url: url, log: log, topology: topo, validate: v}
	backoff := 250 * time.Millisecond
	for attempt := 1; ; attempt++ {
		if err = b.connect(); err == nil {
			return b, nil
		}
		if attempt >= 15 {
			return nil, fmt.Errorf("connect rabbitmq: %w", err)
		}
		if log != nil {
			log.Warn("rabbitmq not ready", "attempt", attempt, "error", err.Error())
		}
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(backoff):
		}
		if backoff < 5*time.Second {
			backoff *= 2
		}
	}
}

func (b *Broker) connect() error {
	conn, err := amqp.DialConfig(b.url, amqp.Config{Heartbeat: 10 * time.Second, Properties: amqp.Table{"connection_name": "agenttwin"}})
	if err != nil {
		return err
	}
	ch, err := conn.Channel()
	if err != nil {
		_ = conn.Close()
		return err
	}
	if err := DeclareTopology(ch, b.topology); err != nil {
		_ = conn.Close()
		return err
	}
	if err := ch.Confirm(false); err != nil {
		_ = conn.Close()
		return fmt.Errorf("enable confirms: %w", err)
	}
	b.mu.Lock()
	b.conn, b.pubCh = conn, ch
	b.mu.Unlock()
	return nil
}

// DeclareTopology declares the exchange, every consumer queue, its retry queue
// and its parking DLQ.
func DeclareTopology(ch *amqp.Channel, t Topology) error {
	if err := ch.ExchangeDeclare(t.Exchange, "topic", true, false, false, false, nil); err != nil {
		return fmt.Errorf("declare exchange: %w", err)
	}
	names := make([]string, 0, len(t.Queues))
	for q := range t.Queues {
		names = append(names, q)
	}
	sort.Strings(names)
	for _, q := range names {
		if _, err := ch.QueueDeclare(q, true, false, false, false, nil); err != nil {
			return fmt.Errorf("declare %s: %w", q, err)
		}
		// Messages in the retry queue expire and dead-letter straight back into
		// the consumer's own queue (default exchange), never to other consumers.
		if _, err := ch.QueueDeclare(q+".retry", true, false, false, false, amqp.Table{
			"x-message-ttl":             clampInt32(t.RetryTTLMS),
			"x-dead-letter-exchange":    "",
			"x-dead-letter-routing-key": q,
		}); err != nil {
			return fmt.Errorf("declare %s.retry: %w", q, err)
		}
		if _, err := ch.QueueDeclare(q+".dlq", true, false, false, false, nil); err != nil {
			return fmt.Errorf("declare %s.dlq: %w", q, err)
		}
		for _, key := range t.Queues[q] {
			if err := ch.QueueBind(q, key, t.Exchange, false, nil); err != nil {
				return fmt.Errorf("bind %s to %s: %w", q, key, err)
			}
		}
	}
	return nil
}

func (b *Broker) channel() (*amqp.Channel, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.closed {
		return nil, errors.New("broker closed")
	}
	if b.conn == nil || b.conn.IsClosed() || b.pubCh == nil || b.pubCh.IsClosed() {
		b.mu.Unlock()
		err := b.connect()
		b.mu.Lock()
		if err != nil {
			return nil, fmt.Errorf("reconnect rabbitmq: %w", err)
		}
	}
	return b.pubCh, nil
}

// Publish validates env and publishes it persistently, waiting for the broker
// confirm. Only a confirmed publish counts as delivered.
func (b *Broker) Publish(ctx context.Context, env Envelope) error {
	if err := b.validate.Validate(env); err != nil {
		return fmt.Errorf("refusing to publish invalid event: %w", err)
	}
	body, err := json.Marshal(env)
	if err != nil {
		return err
	}
	return b.publishRaw(ctx, b.topology.Exchange, env.Type, env.ID, body, nil)
}

func (b *Broker) publishRaw(ctx context.Context, exchange, key, msgID string, body []byte, headers amqp.Table) error {
	ch, err := b.channel()
	if err != nil {
		return err
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	dc, err := ch.PublishWithDeferredConfirmWithContext(ctx, exchange, key, false, false, amqp.Publishing{
		ContentType:  "application/json",
		DeliveryMode: amqp.Persistent,
		MessageId:    msgID,
		Timestamp:    time.Now().UTC(),
		Headers:      headers,
		Body:         body,
	})
	if err != nil {
		return fmt.Errorf("publish %s: %w", key, err)
	}
	waitCtx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	acked, err := dc.WaitContext(waitCtx)
	if err != nil {
		return fmt.Errorf("publish %s: waiting for confirm: %w", key, err)
	}
	if !acked {
		return fmt.Errorf("publish %s: broker nacked", key)
	}
	return nil
}

// Ping reports whether the connection is usable (readiness).
func (b *Broker) Ping(context.Context) error {
	_, err := b.channel()
	return err
}

// Close closes the connection.
func (b *Broker) Close() error {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.closed = true
	if b.conn != nil {
		return b.conn.Close()
	}
	return nil
}

// Handler processes one event. Returning Permanent(err) parks the message in
// the DLQ; any other error schedules a bounded retry.
type Handler func(ctx context.Context, env Envelope) error

// ConsumerConfig configures a consumer.
type ConsumerConfig struct {
	Queue       string
	Concurrency int
	Timeout     time.Duration
	// Observe receives the outcome of every delivery (metrics).
	Observe func(eventType, outcome string, dur time.Duration)
}

// Consume processes messages from cfg.Queue until ctx is cancelled,
// reconnecting on connection loss.
func (b *Broker) Consume(ctx context.Context, cfg ConsumerConfig, handler Handler) error {
	if _, ok := b.topology.Queues[cfg.Queue]; !ok {
		return fmt.Errorf("queue %q is not part of the topology contract", cfg.Queue)
	}
	if cfg.Concurrency <= 0 {
		cfg.Concurrency = 4
	}
	if cfg.Timeout <= 0 {
		cfg.Timeout = 60 * time.Second
	}
	backoff := 500 * time.Millisecond
	for {
		err := b.consumeOnce(ctx, cfg, handler)
		if ctx.Err() != nil {
			return nil
		}
		if b.log != nil {
			b.log.Warn("consumer stopped; reconnecting", "queue", cfg.Queue, "error", fmt.Sprint(err))
		}
		select {
		case <-ctx.Done():
			return nil
		case <-time.After(backoff):
		}
		if backoff < 10*time.Second {
			backoff *= 2
		}
	}
}

func (b *Broker) consumeOnce(ctx context.Context, cfg ConsumerConfig, handler Handler) error {
	conn, err := amqp.Dial(b.url)
	if err != nil {
		return err
	}
	defer func() { _ = conn.Close() }()
	ch, err := conn.Channel()
	if err != nil {
		return err
	}
	if err := DeclareTopology(ch, b.topology); err != nil {
		return err
	}
	if err := ch.Qos(cfg.Concurrency, 0, false); err != nil {
		return err
	}
	deliveries, err := ch.ConsumeWithContext(ctx, cfg.Queue, "", false, false, false, false, nil)
	if err != nil {
		return err
	}
	closed := conn.NotifyClose(make(chan *amqp.Error, 1))
	sem := make(chan struct{}, cfg.Concurrency)
	var wg sync.WaitGroup
	defer wg.Wait()
	for {
		select {
		case <-ctx.Done():
			return nil
		case e := <-closed:
			return fmt.Errorf("connection closed: %w", e)
		case d, ok := <-deliveries:
			if !ok {
				return errors.New("delivery channel closed")
			}
			sem <- struct{}{}
			wg.Add(1)
			go func(d amqp.Delivery) {
				defer wg.Done()
				defer func() { <-sem }()
				b.handleDelivery(ctx, cfg, handler, d)
			}(d)
		}
	}
}

func attemptOf(d amqp.Delivery) int {
	switch v := d.Headers[attemptHeader].(type) {
	case int32:
		return int(v)
	case int64:
		return int(v)
	case int:
		return v
	}
	return 1
}

func (b *Broker) handleDelivery(ctx context.Context, cfg ConsumerConfig, handler Handler, d amqp.Delivery) {
	start := time.Now()
	env, verr := b.validate.ValidateRaw(d.Body)
	observe := func(outcome string) {
		if cfg.Observe != nil {
			cfg.Observe(env.Type, outcome, time.Since(start))
		}
	}
	if verr != nil {
		// Poison message: never retried, parked for inspection.
		b.park(ctx, cfg.Queue, d, "invalid: "+verr.Error())
		observe("poison")
		return
	}
	hctx, cancel := context.WithTimeout(ctx, cfg.Timeout)
	err := safeHandle(hctx, handler, env)
	cancel()
	if err == nil {
		_ = d.Ack(false)
		observe("ok")
		return
	}
	attempt := attemptOf(d)
	if IsPermanent(err) || attempt >= b.topology.MaxAttempts {
		b.park(ctx, cfg.Queue, d, err.Error())
		observe("dead_lettered")
		return
	}
	headers := amqp.Table{}
	for k, v := range d.Headers {
		headers[k] = v
	}
	headers[attemptHeader] = clampInt32(attempt + 1)
	headers["x-agenttwin-last-error"] = truncate(err.Error(), 500)
	if perr := b.publishRaw(ctx, "", cfg.Queue+".retry", d.MessageId, d.Body, headers); perr != nil {
		// Could not schedule a retry: requeue so the message is not lost.
		_ = d.Nack(false, true)
		observe("requeued")
		return
	}
	_ = d.Ack(false)
	if b.log != nil {
		b.log.Warn("event handling failed; retry scheduled", "type", env.Type, "event_id", env.ID, "attempt", attempt, "error", err.Error())
	}
	observe("retry")
}

func (b *Broker) park(ctx context.Context, queue string, d amqp.Delivery, reason string) {
	headers := amqp.Table{}
	for k, v := range d.Headers {
		headers[k] = v
	}
	headers["x-agenttwin-dead-reason"] = truncate(reason, 1000)
	if err := b.publishRaw(ctx, "", queue+".dlq", d.MessageId, d.Body, headers); err != nil {
		_ = d.Nack(false, true)
		return
	}
	_ = d.Ack(false)
	if b.log != nil {
		b.log.Error("event parked in DLQ", "queue", queue, "message_id", d.MessageId, "reason", truncate(reason, 300))
	}
}

func safeHandle(ctx context.Context, h Handler, env Envelope) (err error) {
	defer func() {
		if r := recover(); r != nil {
			err = fmt.Errorf("handler panic: %v", r)
		}
	}()
	return h(ctx, env)
}

func clampInt32(n int) int32 {
	if n > math.MaxInt32 {
		return math.MaxInt32
	}
	if n < math.MinInt32 {
		return math.MinInt32
	}
	return int32(n)
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}
