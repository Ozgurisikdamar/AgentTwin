// Package svcclient calls internal services with a freshly minted internal
// JWT (ADR-0009). Callee error envelopes are surfaced as *httpx.Error so that a
// handler can pass a meaningful status to the public caller.
package svcclient

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/authn"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/httpx"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/logx"
)

// MaxResponseBytes bounds responses read from internal services.
const MaxResponseBytes = 32 << 20

// Client calls one internal service.
type Client struct {
	Service string // audience of minted tokens, e.g. "control-plane"
	BaseURL *url.URL
	Tokens  *authn.TokenService
	HTTP    *http.Client
}

// New creates a client. Internal calls never use environment proxies.
func New(service, baseURL string, tokens *authn.TokenService, timeout time.Duration) (*Client, error) {
	u, err := url.Parse(strings.TrimRight(baseURL, "/"))
	if err != nil || u.Scheme == "" || u.Host == "" {
		return nil, fmt.Errorf("invalid %s base URL %q", service, baseURL)
	}
	if timeout <= 0 {
		timeout = 10 * time.Second
	}
	return &Client{
		Service: service,
		BaseURL: u,
		Tokens:  tokens,
		HTTP: &http.Client{
			Timeout: timeout,
			Transport: otelhttp.NewTransport(&http.Transport{
				Proxy:               nil,
				MaxIdleConnsPerHost: 32,
				IdleConnTimeout:     90 * time.Second,
			}),
			CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
		},
	}, nil
}

// Do sends in (JSON, may be nil) as principal p and decodes a 2xx JSON
// response into out (may be nil).
func (c *Client) Do(ctx context.Context, p authn.Principal, method, path string, in, out any) error {
	var body io.Reader
	if in != nil {
		b, err := json.Marshal(in)
		if err != nil {
			return fmt.Errorf("encode request: %w", err)
		}
		body = bytes.NewReader(b)
	}
	req, err := http.NewRequestWithContext(ctx, method, c.BaseURL.String()+path, body)
	if err != nil {
		return err
	}
	if in != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	req.Header.Set("Accept", "application/json")
	rid := logx.RequestID(ctx)
	tok, err := c.Tokens.Mint(p, c.Service, rid)
	if err != nil {
		return fmt.Errorf("mint internal token: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+tok)
	if rid != "" {
		req.Header.Set(httpx.RequestIDHeader, rid)
	}
	resp, err := c.HTTP.Do(req)
	if err != nil {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		return httpx.NewError(http.StatusBadGateway, "UPSTREAM_UNAVAILABLE",
			fmt.Sprintf("The %s is not reachable right now; retry shortly.", c.Service)).
			WithDetails(map[string]any{"service": c.Service})
	}
	defer func() { _ = resp.Body.Close() }()
	raw, err := io.ReadAll(io.LimitReader(resp.Body, MaxResponseBytes+1))
	if err != nil {
		return httpx.NewError(http.StatusBadGateway, "UPSTREAM_UNAVAILABLE", fmt.Sprintf("Reading the %s response failed.", c.Service))
	}
	if len(raw) > MaxResponseBytes {
		return httpx.NewError(http.StatusBadGateway, "UPSTREAM_RESPONSE_TOO_LARGE", fmt.Sprintf("The %s response exceeded the size limit.", c.Service))
	}
	if resp.StatusCode >= 200 && resp.StatusCode < 300 {
		if out == nil || len(raw) == 0 {
			return nil
		}
		if err := json.Unmarshal(raw, out); err != nil {
			return fmt.Errorf("decode %s response: %w", c.Service, err)
		}
		return nil
	}
	return upstreamError(c.Service, resp.StatusCode, raw)
}

func upstreamError(service string, status int, raw []byte) error {
	// Internal authentication failures are platform misconfiguration, not the
	// public caller's fault.
	if status == http.StatusUnauthorized {
		return httpx.NewError(http.StatusBadGateway, "INTERNAL_AUTH_FAILED",
			fmt.Sprintf("The %s rejected the internal service token; check AGENTTWIN_INTERNAL_TOKEN_SECRET is identical on all services.", service))
	}
	var env struct {
		Error struct {
			Code    string         `json:"code"`
			Message string         `json:"message"`
			Details map[string]any `json:"details"`
		} `json:"error"`
	}
	if json.Unmarshal(raw, &env) == nil && env.Error.Code != "" {
		e := httpx.NewError(status, env.Error.Code, env.Error.Message)
		if env.Error.Details != nil {
			e = e.WithDetails(env.Error.Details)
		}
		return e
	}
	if status >= 500 {
		return httpx.NewError(http.StatusBadGateway, "UPSTREAM_ERROR", fmt.Sprintf("The %s failed (HTTP %d).", service, status))
	}
	return httpx.NewError(status, "UPSTREAM_ERROR", fmt.Sprintf("The %s answered HTTP %d.", service, status))
}

// IsStatus reports whether err is an *httpx.Error with the given status.
func IsStatus(err error, status int) bool {
	var he *httpx.Error
	return errors.As(err, &he) && he.Status == status
}
