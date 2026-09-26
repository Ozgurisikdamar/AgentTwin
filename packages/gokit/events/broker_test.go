package events

import (
	"testing"
	"time"
)

func TestReconnectDelayIsBoundedAndStartsOverAfterConsuming(t *testing.T) {
	// While RabbitMQ stays down the delay doubles, up to the cap.
	backoff := consumeBackoffMin
	var waits []time.Duration
	for i := 0; i < 8; i++ {
		var wait time.Duration
		wait, backoff = reconnectDelay(backoff, false)
		waits = append(waits, wait)
	}
	want := []time.Duration{500 * time.Millisecond, time.Second, 2 * time.Second, 4 * time.Second, 8 * time.Second, 10 * time.Second, 10 * time.Second, 10 * time.Second}
	for i := range want {
		if waits[i] != want[i] {
			t.Fatalf("waits = %v, want %v", waits, want)
		}
	}
	// A consumer that was consuming when the connection dropped starts over.
	wait, next := reconnectDelay(backoff, true)
	if wait != consumeBackoffMin || next != 2*consumeBackoffMin {
		t.Fatalf("after consuming: wait %v, next %v", wait, next)
	}
}
