package cli

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

// Client calls the AgentTwin API through the control plane.
type Client struct {
	BaseURL string
	// APIKey (a project key, sent as X-AgentTwin-Api-Key) or Token (a
	// session, sent as a bearer token) authenticates the calls.
	APIKey, Token string
	// Org names the organization to act in, for credentials in several.
	Org       string
	HTTP      *http.Client
	UserAgent string
}

// APIError is an error the API answered.
type APIError struct {
	Status    int
	Code      string
	Message   string
	RequestID string
	Details   map[string]any
}

func (e *APIError) Error() string {
	msg := fmt.Sprintf("%d %s: %s", e.Status, e.Code, e.Message)
	if f, ok := e.Details["field"].(string); ok && f != "" {
		msg += " (field " + f + ")"
	}
	if e.RequestID != "" {
		msg += " [request " + e.RequestID + "]"
	}
	return msg
}

// Transient reports whether retrying the same request may succeed.
func Transient(err error) bool {
	var ae *APIError
	if errors.As(err, &ae) {
		switch ae.Status {
		case http.StatusTooManyRequests, http.StatusBadGateway, http.StatusServiceUnavailable, http.StatusGatewayTimeout:
			return true
		}
		// The first request with the same Idempotency-Key is still running.
		return ae.Code == "IDEMPOTENCY_IN_PROGRESS"
	}
	var ne *networkError
	return errors.As(err, &ne)
}

// networkError is a request that got no answer.
type networkError struct{ err error }

func (e *networkError) Error() string { return e.err.Error() }
func (e *networkError) Unwrap() error { return e.err }

// request is one API call.
type request struct {
	method, path string
	body         any    // JSON-encoded unless raw is set
	raw          []byte // sent as is with contentType
	contentType  string
	headers      map[string]string
}

// do sends req and decodes a successful answer into out.
func (c *Client) do(ctx context.Context, req request, out any) error {
	var rdr io.Reader
	ct := req.contentType
	switch {
	case req.raw != nil:
		rdr = bytes.NewReader(req.raw)
	case req.body != nil:
		b, err := json.Marshal(req.body)
		if err != nil {
			return err
		}
		rdr, ct = bytes.NewReader(b), "application/json"
	}
	hr, err := http.NewRequestWithContext(ctx, req.method, strings.TrimRight(c.BaseURL, "/")+req.path, rdr)
	if err != nil {
		return err
	}
	if ct != "" {
		hr.Header.Set("Content-Type", ct)
	}
	hr.Header.Set("Accept", "application/json")
	if c.UserAgent != "" {
		hr.Header.Set("User-Agent", c.UserAgent)
	}
	switch {
	case c.APIKey != "":
		hr.Header.Set("X-AgentTwin-Api-Key", c.APIKey)
	case c.Token != "":
		hr.Header.Set("Authorization", "Bearer "+c.Token)
	}
	if c.Org != "" {
		hr.Header.Set("X-AgentTwin-Org", c.Org)
	}
	for k, v := range req.headers {
		hr.Header.Set(k, v)
	}
	hc := c.HTTP
	if hc == nil {
		hc = &http.Client{Timeout: 60 * time.Second}
	}
	res, err := hc.Do(hr)
	if err != nil {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		return &networkError{err: fmt.Errorf("%s %s: %w", req.method, c.BaseURL, err)}
	}
	defer func() { _ = res.Body.Close() }()
	raw, err := io.ReadAll(io.LimitReader(res.Body, 64<<20))
	if err != nil {
		return &networkError{err: err}
	}
	if res.StatusCode >= 300 {
		ae := &APIError{Status: res.StatusCode, Code: http.StatusText(res.StatusCode), Message: strings.TrimSpace(string(raw)),
			RequestID: res.Header.Get("X-Request-Id")}
		var env struct {
			Error struct {
				Code      string         `json:"code"`
				Message   string         `json:"message"`
				RequestID string         `json:"request_id"`
				Details   map[string]any `json:"details"`
			} `json:"error"`
		}
		if json.Unmarshal(raw, &env) == nil && env.Error.Code != "" {
			ae.Code, ae.Message, ae.Details = env.Error.Code, env.Error.Message, env.Error.Details
			if env.Error.RequestID != "" {
				ae.RequestID = env.Error.RequestID
			}
		}
		if len(ae.Message) > 500 {
			ae.Message = ae.Message[:500] + "…"
		}
		return ae
	}
	if out == nil {
		return nil
	}
	if err := json.Unmarshal(raw, out); err != nil {
		return fmt.Errorf("%s %s: unexpected answer: %w", req.method, req.path, err)
	}
	return nil
}
