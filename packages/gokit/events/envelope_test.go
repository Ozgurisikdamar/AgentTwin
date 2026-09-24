package events

import (
	"encoding/json"
	"errors"
	"io/fs"
	"sort"
	"strings"
	"testing"

	"github.com/Ozgurisikdamar/AgentTwin/packages/contracts"
)

const orgID = "0190f3b4-8a2c-7b3e-9f10-2a3b4c5d6e7f"

func validRaw(t *testing.T, eventType string, payload any) []byte {
	t.Helper()
	env, err := New(eventType, "test", orgID, "", "corr-1", "", payload)
	if err != nil {
		t.Fatal(err)
	}
	raw, _ := json.Marshal(env)
	return raw
}

func TestValidatorAcceptsValidEvent(t *testing.T) {
	v, err := DefaultValidator()
	if err != nil {
		t.Fatal(err)
	}
	raw := validRaw(t, "trace.flagged.v1", map[string]any{"trace_id": strings.Repeat("a", 32), "reason": "incident", "flagged_by": "user:1"})
	env, err := v.ValidateRaw(raw)
	if err != nil {
		t.Fatalf("valid event rejected: %v", err)
	}
	if env.Type != "trace.flagged.v1" || env.OrganizationID != orgID {
		t.Fatalf("decoded envelope wrong: %+v", env)
	}
}

func TestValidatorToleratesAdditiveFields(t *testing.T) {
	v, _ := DefaultValidator()
	raw := validRaw(t, "trace.flagged.v1", map[string]any{"trace_id": strings.Repeat("b", 32), "reason": "x", "flagged_by": "u", "new_optional_field": 42})
	if _, err := v.ValidateRaw(raw); err != nil {
		t.Fatalf("additive field must be tolerated: %v", err)
	}
}

func TestValidatorRejectsMissingRequiredPayloadField(t *testing.T) {
	v, _ := DefaultValidator()
	raw := validRaw(t, "trace.flagged.v1", map[string]any{"trace_id": strings.Repeat("c", 32), "reason": "x"}) // flagged_by removed
	if _, err := v.ValidateRaw(raw); err == nil {
		t.Fatal("removal of a required field must fail validation")
	}
}

func TestValidatorRejectsUnknownTypeAndBadEnvelope(t *testing.T) {
	v, _ := DefaultValidator()
	raw := validRaw(t, "nothing.happened.v1", map[string]any{})
	if _, err := v.ValidateRaw(raw); !errors.Is(err, ErrUnknownEventType) {
		t.Fatalf("unknown type must be reported, got %v", err)
	}
	if _, err := v.ValidateRaw([]byte(`{"id":"not-a-uuid","type":"x"}`)); err == nil {
		t.Fatal("bad envelope must be rejected")
	}
	if _, err := v.ValidateRaw([]byte(`not json`)); err == nil {
		t.Fatal("non-json must be rejected")
	}
	traversal := validRaw(t, "../../etc/passwd", map[string]any{})
	if _, err := v.ValidateRaw(traversal); err == nil {
		t.Fatal("path-like event type must be rejected")
	}
}

func TestPermanentWrapping(t *testing.T) {
	base := errors.New("bad input")
	err := Permanent(base)
	if !IsPermanent(err) || !errors.Is(err, base) {
		t.Fatal("permanent wrapping broken")
	}
	if IsPermanent(base) {
		t.Fatal("plain error must not be permanent")
	}
}

// TestTopologyCoversEveryEvent enforces the contract rule that every published
// event type has a consumer queue and every binding has a schema.
func TestTopologyCoversEveryEvent(t *testing.T) {
	topo, err := LoadTopology()
	if err != nil {
		t.Fatal(err)
	}
	bound := map[string]bool{}
	for q, keys := range topo.Queues {
		if !strings.HasSuffix(q, ".events") {
			t.Errorf("queue %q must end with .events", q)
		}
		for _, k := range keys {
			bound[k] = true
		}
	}
	entries, err := fs.ReadDir(contracts.Events, "events")
	if err != nil {
		t.Fatal(err)
	}
	var schemas []string
	for _, e := range entries {
		name := strings.TrimSuffix(e.Name(), ".schema.json")
		if name == "envelope.v1" {
			continue
		}
		schemas = append(schemas, name)
		if !bound[name] {
			t.Errorf("event %s has no consumer queue in topology.json", name)
		}
	}
	sort.Strings(schemas)
	have := map[string]bool{}
	for _, s := range schemas {
		have[s] = true
	}
	for k := range bound {
		if !have[k] {
			t.Errorf("topology binds %s but no schema exists", k)
		}
	}
}

func FuzzValidateRawNeverPanics(f *testing.F) {
	f.Add([]byte(`{"id":"0190f3b4-8a2c-7b3e-9f10-2a3b4c5d6e7f","type":"trace.flagged.v1","occurred_at":"2026-01-01T00:00:00Z","organization_id":"0190f3b4-8a2c-7b3e-9f10-2a3b4c5d6e7f","correlation_id":"c","payload":{}}`))
	f.Add([]byte(`{}`))
	f.Add([]byte(`[]`))
	v, err := DefaultValidator()
	if err != nil {
		f.Fatal(err)
	}
	f.Fuzz(func(t *testing.T, raw []byte) {
		_, _ = v.ValidateRaw(raw)
	})
}
