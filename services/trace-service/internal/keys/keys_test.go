package keys

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

type fakeLookup struct {
	calls   atomic.Int64
	mu      sync.Mutex
	valid   map[string]bool
	failing bool
	gate    chan struct{} // when set, lookups block until it is closed
}

func (f *fakeLookup) lookup(ctx context.Context, key string) (Info, error) {
	f.calls.Add(1)
	if f.gate != nil {
		<-f.gate
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.failing {
		return Info{}, errors.New("control plane unreachable")
	}
	if !f.valid[key] {
		return Info{Valid: false}, nil
	}
	return Info{Valid: true, KeyID: "k-" + key, OrganizationID: "org", ProjectID: "proj"}, nil
}

func (f *fakeLookup) set(key string, valid bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.valid[key] = valid
}

type clock struct {
	mu  sync.Mutex
	now time.Time
}

func (c *clock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.now
}

func (c *clock) Advance(d time.Duration) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.now = c.now.Add(d)
}

func newTestVerifier(f *fakeLookup) (*Verifier, *clock) {
	c := &clock{now: time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)}
	v := NewVerifier(f.lookup, 30*time.Second, 10*time.Second)
	v.SetClock(c.Now)
	return v, c
}

// A revoked key keeps working only until the cached answer expires: the
// revocation latency on ingestion is bounded by the positive TTL.
func TestRevocationTakesEffectWithinTheCacheTTL(t *testing.T) {
	f := &fakeLookup{valid: map[string]bool{"atk_a": true}}
	v, clk := newTestVerifier(f)
	ctx := context.Background()
	info, err := v.Verify(ctx, "atk_a")
	if err != nil || info.KeyID != "k-atk_a" {
		t.Fatalf("valid key: %v %v", info, err)
	}
	f.set("atk_a", false) // revoked in the control plane
	clk.Advance(29 * time.Second)
	if _, err := v.Verify(ctx, "atk_a"); err != nil {
		t.Fatalf("within the TTL the cached answer is used: %v", err)
	}
	if f.calls.Load() != 1 {
		t.Fatalf("lookups = %d, want 1 (cached)", f.calls.Load())
	}
	clk.Advance(2 * time.Second) // 31 s after the first verification
	if _, err := v.Verify(ctx, "atk_a"); !errors.Is(err, ErrInvalid) {
		t.Fatalf("revoked key accepted after the TTL: %v", err)
	}
}

func TestRejectionsAreCachedBriefly(t *testing.T) {
	f := &fakeLookup{valid: map[string]bool{}}
	v, clk := newTestVerifier(f)
	ctx := context.Background()
	for i := 0; i < 3; i++ {
		if _, err := v.Verify(ctx, "atk_unknown"); !errors.Is(err, ErrInvalid) {
			t.Fatalf("unknown key: %v", err)
		}
	}
	if f.calls.Load() != 1 {
		t.Fatalf("lookups = %d, want 1 (negative cache)", f.calls.Load())
	}
	// A key created a moment later is usable once the short negative TTL passes.
	f.set("atk_unknown", true)
	clk.Advance(11 * time.Second)
	if _, err := v.Verify(ctx, "atk_unknown"); err != nil {
		t.Fatalf("new key after negative TTL: %v", err)
	}
}

func TestTransientFailuresAreNotCached(t *testing.T) {
	f := &fakeLookup{valid: map[string]bool{"atk_a": true}, failing: true}
	v, _ := newTestVerifier(f)
	ctx := context.Background()
	if _, err := v.Verify(ctx, "atk_a"); err == nil || errors.Is(err, ErrInvalid) {
		t.Fatalf("outage must be a transient error, not a rejection: %v", err)
	}
	f.mu.Lock()
	f.failing = false
	f.mu.Unlock()
	if _, err := v.Verify(ctx, "atk_a"); err != nil {
		t.Fatalf("after recovery: %v", err)
	}
	if f.calls.Load() != 2 {
		t.Fatalf("lookups = %d, want 2 (the failure was not cached)", f.calls.Load())
	}
}

func TestConcurrentVerificationsShareOneLookup(t *testing.T) {
	f := &fakeLookup{valid: map[string]bool{"atk_a": true}, gate: make(chan struct{})}
	v, _ := newTestVerifier(f)
	var wg sync.WaitGroup
	errs := make(chan error, 20)
	for i := 0; i < 20; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_, err := v.Verify(context.Background(), "atk_a")
			errs <- err
		}()
	}
	// Wait until the first lookup is in flight, then release it.
	for f.calls.Load() == 0 {
		time.Sleep(time.Millisecond)
	}
	time.Sleep(20 * time.Millisecond)
	close(f.gate)
	wg.Wait()
	close(errs)
	for err := range errs {
		if err != nil {
			t.Fatal(err)
		}
	}
	if f.calls.Load() != 1 {
		t.Fatalf("lookups = %d, want 1", f.calls.Load())
	}
}

func TestCacheIsBounded(t *testing.T) {
	f := &fakeLookup{valid: map[string]bool{}}
	v, _ := newTestVerifier(f)
	v.maxEntries = 50
	for i := 0; i < 500; i++ {
		_, _ = v.Verify(context.Background(), fmt.Sprintf("atk_%d", i))
	}
	v.mu.Lock()
	n := len(v.cache)
	v.mu.Unlock()
	if n > 50 {
		t.Fatalf("cache grew to %d entries, bound is 50", n)
	}
}
