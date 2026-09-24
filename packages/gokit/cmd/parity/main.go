// Command parity exposes gokit's canonical JSON and redaction so the Python
// and TypeScript SDKs can run differential tests against the Go reference
// implementation on randomly generated inputs.
//
// stdin:  JSON array of requests
//
//	{"op": "canonical", "value": <any JSON>}
//	{"op": "redact", "mode": "all"|"secrets", "strategy": "mask"|"hash", "input": "..."}
//
// stdout: JSON array of {"output": "...", "error": "..."} in the same order.
package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"os"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/hashx"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/redact"
)

type request struct {
	Op       string          `json:"op"`
	Value    json.RawMessage `json:"value"`
	Mode     string          `json:"mode"`
	Strategy string          `json:"strategy"`
	Input    string          `json:"input"`
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
	default:
		return response{Error: "unknown op " + r.Op}
	}
}

func fail(err error) {
	fmt.Fprintln(os.Stderr, "parity:", err)
	os.Exit(1)
}
