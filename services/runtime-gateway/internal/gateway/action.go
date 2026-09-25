// Package gateway is the pure core of the runtime gateway (ADR-0033): the
// action and its hash, the invocation context read from headers, the rules
// of approval tokens and idempotency records, and the redaction and diff of
// arguments shown to people. The service wires it to storage and the tools.
package gateway

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"sort"
	"strings"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/hashx"
)

// MaxArgsBytes is the largest argument body the gateway accepts.
const MaxArgsBytes = 256 << 10

// Args are a tool call's arguments, read once and held two ways.
type Args struct {
	// Values are for policies: JSON numbers are float64.
	Values map[string]any
	// exact keeps numbers as written, for the hash and for forwarding.
	exact map[string]any
}

// ErrArgs is returned for a body that is not a JSON object of arguments.
var ErrArgs = errors.New("the arguments must be a JSON object")

// ParseArgs reads a tool call's body. An empty body is no arguments.
func ParseArgs(body []byte) (Args, error) {
	if len(body) > MaxArgsBytes {
		return Args{}, fmt.Errorf("%w of at most %d bytes", ErrArgs, MaxArgsBytes)
	}
	if len(bytes.TrimSpace(body)) == 0 {
		return Args{Values: map[string]any{}, exact: map[string]any{}}, nil
	}
	exact, err := decodeObject(body, true)
	if err != nil {
		return Args{}, err
	}
	values, err := decodeObject(body, false)
	if err != nil {
		return Args{}, err
	}
	return Args{Values: values, exact: exact}, nil
}

func decodeObject(body []byte, useNumber bool) (map[string]any, error) {
	dec := json.NewDecoder(bytes.NewReader(body))
	if useNumber {
		dec.UseNumber()
	}
	var v any
	if err := dec.Decode(&v); err != nil {
		return nil, fmt.Errorf("%w: %s", ErrArgs, err.Error())
	}
	if _, err := dec.Token(); !errors.Is(err, io.EOF) {
		return nil, fmt.Errorf("%w: the body holds more than one value", ErrArgs)
	}
	m, ok := v.(map[string]any)
	if !ok {
		return nil, ErrArgs
	}
	return m, nil
}

// Canonical is the arguments' canonical JSON. The gateway forwards these
// bytes, not the body it received: the tool acts on exactly what the policy
// decided (a body with a key twice cannot mean one thing to the policy and
// another to the tool).
func (a Args) Canonical() []byte {
	b, err := hashx.CanonicalJSON(a.exact)
	if err != nil {
		panic(fmt.Sprintf("gateway: canonical arguments: %v", err))
	}
	return b
}

// Action is what an approval, an idempotency key and a decision are about.
type Action struct {
	Organization string
	Project      string
	Agent        string
	Tool         string
	Args         Args
}

// Hash is the SHA-256 of the action's canonical JSON. A changed argument,
// tool, agent, project or organization is a different action.
func (a Action) Hash() string {
	return hashx.MustOf(map[string]any{
		"organization": a.Organization, "project": a.Project, "agent": a.Agent,
		"tool": a.Tool, "arguments": a.Args.exact,
	})
}

// MaxSummary bounds a one-line description of an action.
const MaxSummary = 300

// Describe is a one-line description of a call for people, from redacted
// arguments: refund_payment(amount=150, order_id="ORD-1003").
func Describe(tool string, redacted map[string]any) string {
	keys := make([]string, 0, len(redacted))
	for k := range redacted {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	parts := make([]string, len(keys))
	for i, k := range keys {
		parts[i] = k + "=" + compact(redacted[k])
	}
	s := []rune(tool + "(" + strings.Join(parts, ", ") + ")")
	if len(s) > MaxSummary {
		return string(s[:MaxSummary-1]) + "…"
	}
	return string(s)
}

// compact is v as compact JSON, without HTML escaping.
func compact(v any) string {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		return `"?"`
	}
	return strings.TrimSuffix(buf.String(), "\n")
}
