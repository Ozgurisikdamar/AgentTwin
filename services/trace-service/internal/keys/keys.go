// Package keys authenticates ingest API keys against the control plane with a
// short-lived cache. Only the control plane can read key hashes; the
// trace-service never touches the control schema (ADR-0009).
package keys

import (
	"context"
	"crypto/sha256"
	"errors"
	"sync"
	"time"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/authn"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/svcclient"
)

// Info describes a verified key and its project's data settings.
type Info struct {
	Valid                bool          `json:"valid"`
	KeyID                string        `json:"key_id"`
	OrganizationID       string        `json:"organization_id"`
	ProjectID            string        `json:"project_id"`
	Scopes               []authn.Scope `json:"scopes"`
	ContentMode          string        `json:"content_mode"`
	TraceRetentionDays   int           `json:"trace_retention_days"`
	ContentRetentionDays int           `json:"content_retention_days"`
}

// Principal returns the key as an authorization principal.
func (i Info) Principal() authn.Principal {
	return authn.Principal{OrgID: i.OrganizationID, Actor: "apikey:" + i.KeyID, Role: authn.RoleAPIKey,
		ProjectIDs: []string{i.ProjectID}, Scopes: i.Scopes}
}

// ErrInvalid is returned for unknown, revoked or expired keys.
var ErrInvalid = errors.New("invalid api key")

// Lookup verifies a key remotely.
type Lookup func(ctx context.Context, key string) (Info, error)

// ControlPlaneLookup verifies keys through the control plane internal API.
func ControlPlaneLookup(c *svcclient.Client, service string) Lookup {
	return func(ctx context.Context, key string) (Info, error) {
		var info Info
		err := c.Do(ctx, authn.SystemPrincipal(service), "POST", "/internal/v1/api-keys/verify", map[string]string{"key": key}, &info)
		return info, err
	}
}

type entry struct {
	info    Info
	expires time.Time
}

type call struct {
	done chan struct{}
	info Info
	err  error
}

// Verifier caches verification results. Revocation therefore takes effect on
// ingestion within the positive TTL (documented in the threat model).
type Verifier struct {
	lookup      Lookup
	ttl         time.Duration
	negativeTTL time.Duration
	maxEntries  int
	now         func() time.Time

	mu       sync.Mutex
	cache    map[[32]byte]entry
	inflight map[[32]byte]*call
}

// NewVerifier creates a verifier.
func NewVerifier(lookup Lookup, ttl, negativeTTL time.Duration) *Verifier {
	return &Verifier{lookup: lookup, ttl: ttl, negativeTTL: negativeTTL, maxEntries: 10000, now: time.Now,
		cache: map[[32]byte]entry{}, inflight: map[[32]byte]*call{}}
}

// SetClock overrides the clock (tests).
func (v *Verifier) SetClock(now func() time.Time) { v.now = now }

// Verify returns key info or ErrInvalid. Concurrent verifications of the same
// key share one remote call; transient lookup failures are not cached.
func (v *Verifier) Verify(ctx context.Context, key string) (Info, error) {
	h := sha256.Sum256([]byte(key)) // never keep plaintext keys as map keys
	v.mu.Lock()
	if e, ok := v.cache[h]; ok && v.now().Before(e.expires) {
		v.mu.Unlock()
		if !e.info.Valid {
			return Info{}, ErrInvalid
		}
		return e.info, nil
	}
	if c, ok := v.inflight[h]; ok {
		v.mu.Unlock()
		select {
		case <-c.done:
			return c.info, c.err
		case <-ctx.Done():
			return Info{}, ctx.Err()
		}
	}
	c := &call{done: make(chan struct{})}
	v.inflight[h] = c
	v.mu.Unlock()

	info, err := v.lookup(ctx, key)
	if err == nil && !info.Valid {
		err = ErrInvalid
	}
	v.mu.Lock()
	delete(v.inflight, h)
	if err == nil || errors.Is(err, ErrInvalid) {
		if len(v.cache) >= v.maxEntries {
			for k := range v.cache { // evict an arbitrary entry; the bound matters, not the policy
				delete(v.cache, k)
				break
			}
		}
		ttl := v.ttl
		if err != nil {
			ttl = v.negativeTTL
		}
		v.cache[h] = entry{info: info, expires: v.now().Add(ttl)}
	}
	v.mu.Unlock()
	if err != nil {
		info = Info{}
	}
	c.info, c.err = info, err
	close(c.done)
	return info, err
}
