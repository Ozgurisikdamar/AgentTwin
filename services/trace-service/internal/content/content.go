// Package content enforces the project's capture setting on normalized spans
// (ADR-0008). SDKs redact client-side; this is the server-side enforcement
// and defense in depth: content the project does not allow is dropped,
// secrets are masked in every mode, and oversized values are truncated.
package content

import (
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/redact"
	"github.com/Ozgurisikdamar/AgentTwin/services/trace-service/internal/model"
)

// Mode is the project's content capture mode.
type Mode string

const (
	Off      Mode = "off"
	Redacted Mode = "redacted"
	Full     Mode = "full"
)

// Valid reports whether m is a known mode.
func Valid(m Mode) bool { return m == Off || m == Redacted || m == Full }

// Policy configures enforcement.
type Policy struct {
	Mode            Mode
	MaxContentBytes int // per content field
	MaxExtraBytes   int // per extra/metadata string attribute
	MaxMessageBytes int // status and exception messages
}

// ForMode returns the default limits for a mode.
func ForMode(m Mode) Policy {
	p := Policy{Mode: m, MaxContentBytes: 16 << 10, MaxExtraBytes: 256, MaxMessageBytes: 512}
	if m == Full {
		p.MaxExtraBytes = 2048
		p.MaxMessageBytes = 2048
	}
	return p
}

var (
	secrets = redact.Secrets()
	all     = redact.All()
)

// Apply enforces p on s in place.
func Apply(p Policy, s *model.Span) {
	metaRedactor := secrets
	if p.Mode != Full {
		metaRedactor = all
	}
	// Content fields.
	if !s.Content.Empty() {
		switch {
		case p.Mode == Off:
			s.Content = nil
			s.ContentDropped = true
		case p.Mode == Redacted && !s.Resource.ContentRedacted:
			// Redacted mode requires client-side redaction: never rely on the
			// server alone for sensitive environments.
			s.Content = nil
			s.ContentDropped = true
		default:
			r := secrets
			if p.Mode == Redacted {
				r = all // defense in depth on top of the SDK's redaction
			}
			for _, f := range []*string{&s.Content.Input, &s.Content.InputContext, &s.Content.Output, &s.Content.SystemInstructions, &s.Content.ToolArgs, &s.Content.ToolResult} {
				if *f == "" {
					continue
				}
				v, _ := r.String(*f)
				v, cut := redact.Truncate(v, p.MaxContentBytes)
				*f = v
				s.Truncated = s.Truncated || cut
			}
		}
	}
	// Metadata strings can still carry secrets or personal data by accident.
	for _, f := range []*string{&s.StatusMessage, &s.Attrs.ExceptionMessage} {
		if *f == "" {
			continue
		}
		v, _ := metaRedactor.String(*f)
		v, cut := redact.Truncate(v, p.MaxMessageBytes)
		*f = v
		s.Truncated = s.Truncated || cut
	}
	for k, v := range s.Attrs.Extra {
		str, ok := v.(string)
		if !ok {
			continue
		}
		str, _ = metaRedactor.String(str)
		str, cut := redact.Truncate(str, p.MaxExtraBytes)
		s.Attrs.Extra[k] = str
		s.Truncated = s.Truncated || cut
	}
	for i := range s.Events {
		for k, v := range s.Events[i].Attrs {
			if str, ok := v.(string); ok {
				str, _ = metaRedactor.String(str)
				str, _ = redact.Truncate(str, p.MaxExtraBytes)
				s.Events[i].Attrs[k] = str
			}
		}
	}
}
