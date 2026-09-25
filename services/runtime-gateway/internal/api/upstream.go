package api

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"mime"
	"net/http"
	"strings"
	"time"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/netguard"
	"github.com/Ozgurisikdamar/AgentTwin/services/runtime-gateway/internal/store"
)

// DefaultMaxResponseBytes caps a tool's answer unless a policy's limits
// lower it.
const DefaultMaxResponseBytes = 4 << 20

// call is one forwarded tool call.
type call struct {
	Endpoint       store.Endpoint
	Tool           string
	Args           json.RawMessage
	IdempotencyKey string
	Traceparent    string
	Headers        http.Header
	Timeout        time.Duration
	MaxBytes       int64
	RequestID      string
}

// answer is what the tool answered.
type answer struct {
	Status      int
	Body        []byte
	ContentType string
	// session is an MCP server's session id.
	session string
	// stream is set for a server-sent event stream.
	stream bool
}

// Upstream failures, each with the code the caller sees.
var (
	errTimeout  = errors.New("TOOL_TIMEOUT")
	errTooLarge = errors.New("TOOL_RESPONSE_TOO_LARGE")
	errBlocked  = errors.New("EGRESS_BLOCKED")
	errProtocol = errors.New("TOOL_PROTOCOL_ERROR")
	errFailed   = errors.New("TOOL_UNAVAILABLE")
)

// upstreamError keeps what went wrong for the logs; the caller sees only
// its code.
type upstreamError struct {
	code  error
	cause error
}

func (e *upstreamError) Error() string { return e.code.Error() + ": " + e.cause.Error() }
func (e *upstreamError) Unwrap() error { return e.code }

func classify(err error) error {
	var ue *upstreamError
	if errors.As(err, &ue) {
		return err
	}
	code := errFailed
	switch {
	case errors.Is(err, context.DeadlineExceeded):
		code = errTimeout
	case errors.Is(err, netguard.ErrBlocked):
		code = errBlocked
	}
	return &upstreamError{code, err}
}

// codeOf is the error code of an upstream failure.
func codeOf(err error) string {
	for _, c := range []error{errTimeout, errTooLarge, errBlocked, errProtocol} {
		if errors.Is(err, c) {
			return c.Error()
		}
	}
	return errFailed.Error()
}

func (s *Server) client() *http.Client {
	if s.Client != nil {
		return s.Client
	}
	return s.Egress.Client()
}

// forward calls the tool (http: POST the arguments; mcp: a JSON-RPC
// tools/call) within the call's timeout.
func (s *Server) forward(ctx context.Context, c call) (answer, error) {
	ctx, cancel := context.WithTimeout(ctx, c.Timeout)
	defer cancel()
	var a answer
	var err error
	if c.Endpoint.Kind == "mcp" {
		a, err = s.forwardMCP(ctx, c)
	} else {
		a, err = s.post(ctx, c, c.Endpoint.URL, c.Args, "application/json", "")
	}
	if err != nil {
		return answer{}, classify(err)
	}
	return a, nil
}

// post sends body and reads the answer, bounded by the call's cap.
func (s *Server) post(ctx context.Context, c call, url string, body []byte, accept, session string) (answer, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return answer{}, err
	}
	for name, values := range c.Headers {
		for _, v := range values {
			req.Header.Add(name, v)
		}
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", accept)
	req.Header.Set("User-Agent", "agenttwin-runtime-gateway")
	if c.IdempotencyKey != "" {
		req.Header.Set("Idempotency-Key", c.IdempotencyKey)
	}
	if c.Traceparent != "" {
		req.Header.Set("Traceparent", c.Traceparent)
	}
	if c.RequestID != "" {
		req.Header.Set("X-Request-Id", c.RequestID)
	}
	if session != "" {
		req.Header.Set("Mcp-Session-Id", session)
	}
	resp, err := s.client().Do(req)
	if err != nil {
		return answer{}, err
	}
	defer func() { _ = resp.Body.Close() }()
	b, err := netguard.ReadLimited(resp.Body, c.MaxBytes)
	if err != nil {
		if ctx.Err() != nil {
			return answer{}, ctx.Err()
		}
		return answer{}, &upstreamError{errTooLarge, err}
	}
	a := answer{Status: resp.StatusCode, Body: b, ContentType: safeContentType(resp.Header.Get("Content-Type")),
		stream: strings.HasPrefix(resp.Header.Get("Content-Type"), "text/event-stream")}
	if session == "" {
		a.session = resp.Header.Get("Mcp-Session-Id")
	}
	return a, nil
}

// safeContentType is the content type the gateway answers a tool's body
// with: JSON as application/json, text as text/plain, anything else as
// application/octet-stream (a tool cannot make the gateway serve HTML to a
// browser).
func safeContentType(ct string) string {
	mt, params, err := mime.ParseMediaType(ct)
	switch {
	case err != nil:
		return "application/octet-stream"
	case mt == "application/json", strings.HasSuffix(mt, "+json"):
		return "application/json"
	case mt == "text/plain", mt == "text/csv", mt == "text/markdown", mt == "text/event-stream":
		if cs := params["charset"]; cs != "" {
			return mime.FormatMediaType("text/plain", map[string]string{"charset": cs})
		}
		return "text/plain"
	}
	return "application/octet-stream"
}

// ---------------------------------------------------------------- MCP

// mcpVersion is the MCP protocol revision the gateway speaks.
const mcpVersion = "2025-06-18"

type rpcResponse struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      any             `json:"id"`
	Result  json.RawMessage `json:"result"`
	Error   *struct {
		Code    int    `json:"code"`
		Message string `json:"message"`
	} `json:"error"`
}

// forwardMCP runs one tool call over MCP's streamable HTTP transport:
// initialize, the initialized notification, then tools/call. The tool's
// result is answered as JSON (an error result keeps the tool's isError).
func (s *Server) forwardMCP(ctx context.Context, c call) (answer, error) {
	accept := "application/json, text/event-stream"
	init, _ := json.Marshal(map[string]any{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": map[string]any{
		"protocolVersion": mcpVersion, "capabilities": map[string]any{},
		"clientInfo": map[string]any{"name": "agenttwin-runtime-gateway", "version": "1"},
	}})
	a, err := s.post(ctx, c, c.Endpoint.URL, init, accept, "")
	if err != nil {
		return answer{}, err
	}
	if _, err := rpcResult(a); err != nil {
		return answer{}, err
	}
	session := a.session
	note, _ := json.Marshal(map[string]any{"jsonrpc": "2.0", "method": "notifications/initialized"})
	if _, err := s.post(ctx, c, c.Endpoint.URL, note, accept, session); err != nil {
		return answer{}, err
	}
	var args map[string]any
	if err := json.Unmarshal(c.Args, &args); err != nil {
		return answer{}, err
	}
	callBody, _ := json.Marshal(map[string]any{"jsonrpc": "2.0", "id": 2, "method": "tools/call",
		"params": map[string]any{"name": c.Tool, "arguments": args}})
	a, err = s.post(ctx, c, c.Endpoint.URL, callBody, accept, session)
	if err != nil {
		return answer{}, err
	}
	result, err := rpcResult(a)
	if err != nil {
		return answer{}, err
	}
	return answer{Status: http.StatusOK, Body: result, ContentType: "application/json"}, nil
}

// rpcResult reads a JSON-RPC answer, plain or as the first event of a
// server-sent stream.
func rpcResult(a answer) (json.RawMessage, error) {
	if a.Status < 200 || a.Status > 299 {
		return nil, &upstreamError{errProtocol, fmt.Errorf("MCP server answered HTTP %d", a.Status)}
	}
	body := a.Body
	if a.stream {
		body = nil
		sc := bufio.NewScanner(bytes.NewReader(a.Body))
		sc.Buffer(make([]byte, 64<<10), len(a.Body)+1)
		for sc.Scan() {
			if line, ok := strings.CutPrefix(sc.Text(), "data:"); ok {
				body = []byte(strings.TrimSpace(line))
				break
			}
		}
	}
	var r rpcResponse
	if err := json.NewDecoder(bytes.NewReader(body)).Decode(&r); err != nil && !errors.Is(err, io.EOF) {
		return nil, &upstreamError{errProtocol, fmt.Errorf("not a JSON-RPC answer: %w", err)}
	}
	if r.Error != nil {
		return nil, &upstreamError{errProtocol, fmt.Errorf("MCP error %d: %s", r.Error.Code, r.Error.Message)}
	}
	if len(r.Result) == 0 {
		return nil, &upstreamError{errProtocol, errors.New("the MCP answer has no result")}
	}
	return r.Result, nil
}
