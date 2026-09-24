// Package events implements the AgentTwin event envelope, schema validation,
// RabbitMQ publishing with confirms, consumers with bounded retry and a
// parking DLQ, and a transactional outbox relay (ADR-0002).
package events

import (
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"strings"
	"sync"
	"time"

	"github.com/santhosh-tekuri/jsonschema/v6"

	"github.com/Ozgurisikdamar/AgentTwin/packages/contracts"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/ids"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/jsonschemax"
)

// Exchange is the durable topic exchange all events are published to.
const Exchange = "agenttwin.events"

// RetryExchange routes rejected messages into per-consumer retry queues.
const RetryExchange = "agenttwin.retry"

// Envelope is the common event wrapper (packages/contracts/events/envelope.v1).
type Envelope struct {
	ID             string          `json:"id"`
	Type           string          `json:"type"`
	OccurredAt     time.Time       `json:"occurred_at"`
	OrganizationID string          `json:"organization_id"`
	ProjectID      *string         `json:"project_id"`
	CorrelationID  string          `json:"correlation_id"`
	CausationID    *string         `json:"causation_id"`
	Producer       string          `json:"producer,omitempty"`
	Payload        json.RawMessage `json:"payload"`
}

// New builds an envelope for payload. correlationID should be the request id
// (or the correlation id of the event being handled) so a chain of events can
// be followed end to end.
func New(eventType, producer, orgID, projectID, correlationID string, causationID string, payload any) (Envelope, error) {
	raw, err := json.Marshal(payload)
	if err != nil {
		return Envelope{}, fmt.Errorf("marshal payload: %w", err)
	}
	if correlationID == "" {
		correlationID = ids.New()
	}
	env := Envelope{
		ID:             ids.New(),
		Type:           eventType,
		OccurredAt:     time.Now().UTC(),
		OrganizationID: orgID,
		CorrelationID:  correlationID,
		Producer:       producer,
		Payload:        raw,
	}
	if projectID != "" {
		env.ProjectID = &projectID
	}
	if causationID != "" {
		env.CausationID = &causationID
	}
	return env, nil
}

// Project returns the project id or "".
func (e Envelope) Project() string {
	if e.ProjectID == nil {
		return ""
	}
	return *e.ProjectID
}

// Decode unmarshals the payload into dst.
func (e Envelope) Decode(dst any) error {
	if err := json.Unmarshal(e.Payload, dst); err != nil {
		return Permanent(fmt.Errorf("decode %s payload: %w", e.Type, err))
	}
	return nil
}

// Validator validates envelopes and payloads against the embedded contracts.
type Validator struct {
	envelope *jsonschema.Schema
	mu       sync.Mutex
	payloads map[string]*jsonschema.Schema
	fsys     fs.FS
}

var (
	defaultValidator     *Validator
	defaultValidatorErr  error
	defaultValidatorOnce sync.Once
)

// DefaultValidator returns the validator backed by packages/contracts.
func DefaultValidator() (*Validator, error) {
	defaultValidatorOnce.Do(func() {
		defaultValidator, defaultValidatorErr = NewValidator(contracts.Events)
	})
	return defaultValidator, defaultValidatorErr
}

// NewValidator compiles the envelope schema from fsys (events/ directory).
func NewValidator(fsys fs.FS) (*Validator, error) {
	env, err := jsonschemax.Compile(fsys, "events/envelope.v1.schema.json")
	if err != nil {
		return nil, err
	}
	return &Validator{envelope: env, payloads: map[string]*jsonschema.Schema{}, fsys: fsys}, nil
}

func (v *Validator) payloadSchema(eventType string) (*jsonschema.Schema, error) {
	v.mu.Lock()
	defer v.mu.Unlock()
	if s, ok := v.payloads[eventType]; ok {
		return s, nil
	}
	if strings.ContainsAny(eventType, "/\\") {
		return nil, fmt.Errorf("invalid event type %q", eventType)
	}
	s, err := jsonschemax.Compile(v.fsys, "events/"+eventType+".schema.json")
	if err != nil {
		return nil, fmt.Errorf("unknown event type %q: %w", eventType, err)
	}
	v.payloads[eventType] = s
	return s, nil
}

// ErrUnknownEventType is returned for types without a published schema.
var ErrUnknownEventType = errors.New("unknown event type")

// ValidateRaw parses and validates a raw envelope message.
func (v *Validator) ValidateRaw(raw []byte) (Envelope, error) {
	if err := jsonschemax.ValidateJSON(v.envelope, raw); err != nil {
		return Envelope{}, fmt.Errorf("invalid envelope: %w", err)
	}
	var env Envelope
	if err := json.Unmarshal(raw, &env); err != nil {
		return Envelope{}, fmt.Errorf("decode envelope: %w", err)
	}
	return env, v.ValidatePayload(env)
}

// ValidatePayload validates env.Payload against the schema for env.Type.
func (v *Validator) ValidatePayload(env Envelope) error {
	s, err := v.payloadSchema(env.Type)
	if err != nil {
		return fmt.Errorf("%w: %s", ErrUnknownEventType, env.Type)
	}
	if err := jsonschemax.ValidateJSON(s, env.Payload); err != nil {
		return fmt.Errorf("invalid %s payload: %w", env.Type, err)
	}
	return nil
}

// Validate validates a whole envelope value (used before publishing).
func (v *Validator) Validate(env Envelope) error {
	raw, err := json.Marshal(env)
	if err != nil {
		return err
	}
	_, err = v.ValidateRaw(raw)
	return err
}

// permanentError marks a handler failure that must not be retried.
type permanentError struct{ err error }

func (p permanentError) Error() string { return "permanent: " + p.err.Error() }
func (p permanentError) Unwrap() error { return p.err }

// Permanent wraps err so the consumer parks the message instead of retrying
// (invalid input, deterministic domain rejection).
func Permanent(err error) error { return permanentError{err: err} }

// IsPermanent reports whether err was wrapped with Permanent.
func IsPermanent(err error) bool {
	var p permanentError
	return errors.As(err, &p)
}
