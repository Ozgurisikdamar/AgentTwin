// Package policy decides a runtime tool action from versioned policies
// (spec §30, ADR-0005, ADR-0033). It is pure: the caller gathers the action
// and the active policies, the package decides and explains.
//
// A policy is a document that targets one tool: ordered rules, each a CEL
// expression with an effect (allow, allow_with_limits, require_approval,
// deny) and a message; a default effect; a fail mode; limits for
// allow_with_limits; the approval expiry; and the tests it must pass before
// it can be activated. Compiling type-checks every rule against the declared
// variables. Deciding evaluates every rule under a cost limit: the most
// restrictive effect among the rules that match wins, the first such rule in
// order is the one reported, and a rule that fails to evaluate applies the
// policy's fail mode. The same evaluator serves tests, the boundary analysis
// of numeric thresholds, activation and live traffic.
package policy

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"regexp"
	"strings"

	"gopkg.in/yaml.v3"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/hashx"
)

// Document identity.
const (
	APIVersion = "agenttwin.dev/v1"
	Kind       = "Policy"
)

// Effect is what a rule, a policy or a decision allows.
type Effect string

// Effects, from the least to the most restrictive.
const (
	Allow           Effect = "allow"
	AllowWithLimits Effect = "allow_with_limits"
	RequireApproval Effect = "require_approval"
	Deny            Effect = "deny"
)

var effectRank = map[Effect]int{Allow: 0, AllowWithLimits: 1, RequireApproval: 2, Deny: 3}

// Valid reports whether e is a known effect.
func (e Effect) Valid() bool {
	_, ok := effectRank[e]
	return ok
}

// MoreRestrictive reports whether a restricts more than b.
func MoreRestrictive(a, b Effect) bool { return effectRank[a] > effectRank[b] }

// FailMode is what a policy decides when a rule cannot be evaluated.
type FailMode string

// Fail modes.
const (
	FailClosed   FailMode = "fail_closed"
	FailOpen     FailMode = "fail_open"
	FailApproval FailMode = "require_approval"
)

// Valid reports whether m is a known fail mode.
func (m FailMode) Valid() bool { return m == FailClosed || m == FailOpen || m == FailApproval }

// RiskRead is the only risk tier a policy may fail open for.
const RiskRead = "READ"

// Bounds of a document.
const (
	MaxRules          = 50
	MaxTests          = 100
	MaxExpression     = 2000
	MaxMessage        = 500
	MaxDescription    = 2000
	MaxTimeoutMs      = 60_000
	MaxResponseBytes  = 8 << 20
	MinApprovalExpiry = 60
	MaxApprovalExpiry = 86_400
	// DefaultApprovalExpiry is the approval expiry of a policy that sets none.
	DefaultApprovalExpiry = 900
	maxDocument           = 256 << 10
)

var (
	namePattern = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{0,62}$`)
	toolPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$`)
)

// Rule is one condition of a policy.
type Rule struct {
	Name    string `json:"name"`
	When    string `json:"when"`
	Effect  Effect `json:"effect"`
	Message string `json:"message,omitempty"`
}

// Limits constrain an action a policy allows with limits.
type Limits struct {
	TimeoutMs        int `json:"timeoutMs,omitempty"`
	MaxResponseBytes int `json:"maxResponseBytes,omitempty"`
}

// Approval configures the approval requests a policy creates.
type Approval struct {
	ExpiresInSeconds int `json:"expiresInSeconds,omitempty"`
}

// Context is the action's context in a test case (the arguments aside).
type Context struct {
	Agent        string `json:"agent,omitempty"`
	AgentVersion string `json:"agentVersion,omitempty"`
	Environment  string `json:"environment,omitempty"`
	Subject      string `json:"subject,omitempty"`
	Risk         string `json:"risk,omitempty"`
	TraceID      string `json:"traceId,omitempty"`
	TraceCalls   int    `json:"traceCalls,omitempty"`
}

// Test is a case a policy must decide as expected.
type Test struct {
	Name    string         `json:"name"`
	Args    map[string]any `json:"args"`
	Context Context        `json:"context,omitzero"`
	Expect  Effect         `json:"expect"`
	// Rule, when set, is the rule expected to decide.
	Rule string `json:"rule,omitempty"`
}

// Metadata names a policy.
type Metadata struct {
	Name        string `json:"name"`
	Description string `json:"description,omitempty"`
}

// Body is what a policy decides.
type Body struct {
	Tool     string    `json:"tool"`
	Default  Effect    `json:"default,omitempty"`
	FailMode FailMode  `json:"failMode,omitempty"`
	Rules    []Rule    `json:"rules"`
	Limits   *Limits   `json:"limits,omitempty"`
	Approval *Approval `json:"approval,omitempty"`
	Tests    []Test    `json:"tests,omitempty"`
}

// Spec is a policy document.
type Spec struct {
	APIVersion string   `json:"apiVersion"`
	Kind       string   `json:"kind"`
	Metadata   Metadata `json:"metadata"`
	Spec       Body     `json:"spec"`
}

// Problem is one reason a document is invalid, with where it is.
type Problem struct {
	Field   string `json:"field"`
	Message string `json:"message"`
}

// InvalidError lists what is wrong with a document.
type InvalidError struct{ Problems []Problem }

func (e *InvalidError) Error() string {
	parts := make([]string, len(e.Problems))
	for i, p := range e.Problems {
		parts[i] = p.Field + ": " + p.Message
	}
	return "invalid policy: " + strings.Join(parts, "; ")
}

// Parse reads a policy document (YAML or JSON), fills its defaults and
// validates it. Unknown fields are refused: a misspelt key would otherwise
// silently drop a rule's intent.
func Parse(doc []byte) (Spec, error) {
	if len(bytes.TrimSpace(doc)) == 0 {
		return Spec{}, &InvalidError{[]Problem{{"", "the document is empty"}}}
	}
	if len(doc) > maxDocument {
		return Spec{}, &InvalidError{[]Problem{{"", fmt.Sprintf("the document exceeds %d bytes", maxDocument)}}}
	}
	dec := yaml.NewDecoder(bytes.NewReader(doc))
	var generic any
	if err := dec.Decode(&generic); err != nil {
		return Spec{}, &InvalidError{[]Problem{{"", "not valid YAML or JSON: " + yamlMessage(err)}}}
	}
	// A second document would be ignored by a plain unmarshal: refuse it.
	var extra any
	if err := dec.Decode(&extra); !errors.Is(err, io.EOF) {
		return Spec{}, &InvalidError{[]Problem{{"", "the document holds more than one policy"}}}
	}
	raw, err := json.Marshal(normalize(generic))
	if err != nil {
		return Spec{}, &InvalidError{[]Problem{{"", "the document cannot be read as JSON: " + err.Error()}}}
	}
	return Decode(raw)
}

// Decode reads a policy document from JSON, fills its defaults and validates it.
func Decode(raw []byte) (Spec, error) {
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	var s Spec
	if err := dec.Decode(&s); err != nil {
		return Spec{}, &InvalidError{[]Problem{{"", "the document does not match the policy schema: " + err.Error()}}}
	}
	if dec.More() {
		return Spec{}, &InvalidError{[]Problem{{"", "the document holds more than one value"}}}
	}
	s.fill()
	if ps := s.problems(); len(ps) > 0 {
		return Spec{}, &InvalidError{ps}
	}
	return s, nil
}

func (s *Spec) fill() {
	if s.Spec.Default == "" {
		s.Spec.Default = Allow
	}
	if s.Spec.FailMode == "" {
		s.Spec.FailMode = FailClosed
	}
	for i := range s.Spec.Tests {
		if s.Spec.Tests[i].Args == nil {
			s.Spec.Tests[i].Args = map[string]any{}
		}
	}
}

// ApprovalExpiry is how long an approval request of this policy stays open.
func (s Spec) ApprovalExpiry() int {
	if s.Spec.Approval != nil && s.Spec.Approval.ExpiresInSeconds > 0 {
		return s.Spec.Approval.ExpiresInSeconds
	}
	return DefaultApprovalExpiry
}

// Hash is the SHA-256 of the document's canonical JSON, defaults filled.
func (s Spec) Hash() string {
	raw, err := json.Marshal(s)
	if err != nil {
		panic(fmt.Sprintf("policy: marshal: %v", err))
	}
	var v any
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	if err := dec.Decode(&v); err != nil {
		panic(fmt.Sprintf("policy: decode: %v", err))
	}
	return hashx.MustOf(v)
}

func (s Spec) problems() []Problem {
	var ps []Problem
	add := func(field, format string, args ...any) {
		ps = append(ps, Problem{field, fmt.Sprintf(format, args...)})
	}
	if s.APIVersion != APIVersion {
		add("apiVersion", "must be %q", APIVersion)
	}
	if s.Kind != Kind {
		add("kind", "must be %q", Kind)
	}
	if !namePattern.MatchString(s.Metadata.Name) {
		add("metadata.name", "must be lowercase letters, digits and dashes (1 to 63 characters)")
	}
	if len(s.Metadata.Description) > MaxDescription {
		add("metadata.description", "must be at most %d characters", MaxDescription)
	}
	b := s.Spec
	if !toolPattern.MatchString(b.Tool) {
		add("spec.tool", "must name a tool (letters, digits, _ . : -; up to 128 characters)")
	}
	if !b.Default.Valid() {
		add("spec.default", "must be one of allow, allow_with_limits, require_approval, deny")
	}
	if !b.FailMode.Valid() {
		add("spec.failMode", "must be one of fail_closed, fail_open, require_approval")
	}
	switch {
	case len(b.Rules) == 0:
		add("spec.rules", "a policy needs at least one rule")
	case len(b.Rules) > MaxRules:
		add("spec.rules", "at most %d rules", MaxRules)
	}
	names := map[string]bool{}
	for i, r := range b.Rules {
		f := fmt.Sprintf("spec.rules[%d]", i)
		if !namePattern.MatchString(r.Name) {
			add(f+".name", "must be lowercase letters, digits and dashes (1 to 63 characters)")
		} else if names[r.Name] {
			add(f+".name", "%q is used by another rule", r.Name)
		}
		names[r.Name] = true
		switch {
		case strings.TrimSpace(r.When) == "":
			add(f+".when", "the condition is empty")
		case len(r.When) > MaxExpression:
			add(f+".when", "must be at most %d characters", MaxExpression)
		}
		if !r.Effect.Valid() {
			add(f+".effect", "must be one of allow, allow_with_limits, require_approval, deny")
		}
		if len(r.Message) > MaxMessage {
			add(f+".message", "must be at most %d characters", MaxMessage)
		}
	}
	if l := b.Limits; l != nil {
		if l.TimeoutMs < 0 || l.TimeoutMs > MaxTimeoutMs {
			add("spec.limits.timeoutMs", "must be between 1 and %d", MaxTimeoutMs)
		}
		if l.MaxResponseBytes < 0 || l.MaxResponseBytes > MaxResponseBytes {
			add("spec.limits.maxResponseBytes", "must be between 1 and %d", MaxResponseBytes)
		}
		if l.TimeoutMs == 0 && l.MaxResponseBytes == 0 {
			add("spec.limits", "set timeoutMs or maxResponseBytes, or leave limits out")
		}
	}
	if a := b.Approval; a != nil && (a.ExpiresInSeconds < MinApprovalExpiry || a.ExpiresInSeconds > MaxApprovalExpiry) {
		add("spec.approval.expiresInSeconds", "must be between %d and %d", MinApprovalExpiry, MaxApprovalExpiry)
	}
	if len(b.Tests) > MaxTests {
		add("spec.tests", "at most %d tests", MaxTests)
	}
	tests := map[string]bool{}
	for i, t := range b.Tests {
		f := fmt.Sprintf("spec.tests[%d]", i)
		switch {
		case strings.TrimSpace(t.Name) == "" || len(t.Name) > 200:
			add(f+".name", "must be 1 to 200 characters")
		case tests[t.Name]:
			add(f+".name", "%q is used by another test", t.Name)
		}
		tests[t.Name] = true
		if !t.Expect.Valid() {
			add(f+".expect", "must be one of allow, allow_with_limits, require_approval, deny")
		}
		if t.Rule != "" && !names[t.Rule] {
			add(f+".rule", "names no rule of this policy")
		}
		if t.Context.TraceCalls < 0 {
			add(f+".context.traceCalls", "must not be negative")
		}
	}
	return ps
}

// normalize turns YAML's decoded values into JSON-encodable ones (YAML maps
// may have non-string keys).
func normalize(v any) any {
	switch x := v.(type) {
	case map[string]any:
		for k, e := range x {
			x[k] = normalize(e)
		}
		return x
	case map[any]any:
		out := make(map[string]any, len(x))
		for k, e := range x {
			out[fmt.Sprint(k)] = normalize(e)
		}
		return out
	case []any:
		for i, e := range x {
			x[i] = normalize(e)
		}
		return x
	default:
		return v
	}
}

func yamlMessage(err error) string {
	var te *yaml.TypeError
	if errors.As(err, &te) {
		return strings.Join(te.Errors, "; ")
	}
	return strings.TrimPrefix(err.Error(), "yaml: ")
}
