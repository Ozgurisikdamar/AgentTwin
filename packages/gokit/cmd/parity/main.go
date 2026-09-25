// Command parity exposes gokit's canonical JSON, redaction, internal tokens
// and RBAC matrix so the Python and TypeScript code can run differential tests
// against the Go reference implementation.
//
// stdin:  JSON array of requests
//
//	{"op": "canonical", "value": <any JSON>}
//	{"op": "redact", "mode": "all"|"secrets", "strategy": "mask"|"hash", "input": "..."}
//	{"op": "mint", "secret": "...", "audience": "...", "principal": {...}, "request_id": "..."}
//	{"op": "verify", "secret": "...", "audience": "...", "input": "<token>"}
//	{"op": "rbac"}   -> output is JSON {"roles": {role: [perm...]}, "scopes": {scope: [perm...]}}
//	{"op": "declare_topology", "input": "<amqp url>"}  -> declares the event topology with gokit
//
// stdout: JSON array of {"output": "...", "error": "..."} in the same order.
package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"os"

	amqp "github.com/rabbitmq/amqp091-go"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/authn"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/events"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/hashx"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/redact"
)

type request struct {
	Op        string           `json:"op"`
	Value     json.RawMessage  `json:"value"`
	Mode      string           `json:"mode"`
	Strategy  string           `json:"strategy"`
	Input     string           `json:"input"`
	Secret    string           `json:"secret"`
	Audience  string           `json:"audience"`
	RequestID string           `json:"request_id"`
	Principal *authn.Principal `json:"principal"`
}

type response struct {
	Output string `json:"output"`
	Error  string `json:"error,omitempty"`
}

func main() {
	raw, err := io.ReadAll(os.Stdin)
	if err != nil {
		fail(err)
	}
	var reqs []request
	if err := json.Unmarshal(raw, &reqs); err != nil {
		fail(err)
	}
	out := make([]response, len(reqs))
	for i, r := range reqs {
		out[i] = handle(r)
	}
	enc := json.NewEncoder(os.Stdout)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(out); err != nil {
		fail(err)
	}
}

func handle(r request) response {
	switch r.Op {
	case "canonical":
		dec := json.NewDecoder(bytes.NewReader(r.Value))
		dec.UseNumber()
		var v any
		if err := dec.Decode(&v); err != nil {
			return response{Error: err.Error()}
		}
		b, err := hashx.CanonicalJSON(v)
		if err != nil {
			return response{Error: err.Error()}
		}
		return response{Output: string(b)}
	case "redact":
		red := redact.Secrets()
		if r.Mode == "all" {
			red = redact.All()
		}
		if r.Strategy == "hash" {
			red.Strategy = redact.Hash
		}
		s, _ := red.String(r.Input)
		return response{Output: s}
	case "mint":
		ts, err := authn.NewTokenService(r.Secret)
		if err != nil {
			return response{Error: err.Error()}
		}
		if r.Principal == nil {
			return response{Error: "principal required"}
		}
		tok, err := ts.Mint(*r.Principal, r.Audience, r.RequestID)
		if err != nil {
			return response{Error: err.Error()}
		}
		return response{Output: tok}
	case "verify":
		ts, err := authn.NewTokenService(r.Secret)
		if err != nil {
			return response{Error: err.Error()}
		}
		p, rid, err := ts.Verify(r.Input, r.Audience)
		if err != nil {
			return response{Error: err.Error()}
		}
		b, _ := json.Marshal(map[string]any{"principal": p, "request_id": rid})
		return response{Output: string(b)}
	case "declare_topology":
		topo, err := events.LoadTopology()
		if err != nil {
			return response{Error: err.Error()}
		}
		conn, err := amqp.Dial(r.Input)
		if err != nil {
			return response{Error: err.Error()}
		}
		defer func() { _ = conn.Close() }()
		ch, err := conn.Channel()
		if err != nil {
			return response{Error: err.Error()}
		}
		if err := events.DeclareTopology(ch, topo); err != nil {
			return response{Error: err.Error()}
		}
		return response{Output: "ok"}
	case "rbac":
		b, _ := json.Marshal(authn.Matrix())
		return response{Output: string(b)}
	default:
		return response{Error: "unknown op " + r.Op}
	}
}

func fail(err error) {
	fmt.Fprintln(os.Stderr, "parity:", err)
	os.Exit(1)
}
