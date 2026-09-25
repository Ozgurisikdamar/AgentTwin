package api

import (
	"errors"
	"fmt"
	"net/http"
	"net/textproto"
	"net/url"
	"regexp"
	"slices"
	"strings"

	"github.com/jackc/pgx/v5"

	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/authn"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/httpx"
	"github.com/Ozgurisikdamar/AgentTwin/packages/gokit/netguard"
	"github.com/Ozgurisikdamar/AgentTwin/services/runtime-gateway/internal/gateway"
	"github.com/Ozgurisikdamar/AgentTwin/services/runtime-gateway/internal/store"
)

// Tool risk tiers (spec §30).
var riskTiers = []string{"READ", "WRITE_REVERSIBLE", "WRITE_IRREVERSIBLE", "EXECUTE", "ADMIN"}

// Bounds of a tool endpoint.
const (
	DefaultTimeoutMs  = 10_000
	MinTimeoutMs      = 100
	MaxTimeoutMs      = 60_000
	MaxForwardHeaders = 10
	maxURL            = 2000
)

// neverForwarded are request headers a tool endpoint may not ask for: the
// caller's credentials, the gateway's own controls and hop-by-hop headers.
// The idempotency key is always forwarded.
var neverForwarded = []string{
	"Authorization", "Cookie", "Proxy-Authorization", "Host", "Content-Length", "Content-Type", "Content-Encoding",
	"Transfer-Encoding", "Connection", "Keep-Alive", "Upgrade", "Te", "Trailer", "Expect", "Forwarded",
	"Idempotency-Key", "Traceparent", "Tracestate", "X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto",
	"X-Request-Id", "X-Agenttwin-Api-Key", "X-Agenttwin-Org", textproto.CanonicalMIMEHeaderKey(gateway.HeaderApprovalToken),
}

var headerNamePattern = regexp.MustCompile(`^[A-Za-z0-9-]{1,100}$`)

type endpointBody struct {
	ProjectID      string   `json:"project_id"`
	Kind           string   `json:"kind"`
	URL            string   `json:"url"`
	Risk           string   `json:"risk"`
	TimeoutMs      *int     `json:"timeout_ms"`
	Idempotency    string   `json:"idempotency"`
	ForwardHeaders []string `json:"forward_headers"`
}

// checkEndpoint validates a tool endpoint and fills its defaults.
func checkEndpoint(egress netguard.Policy, tool string, b endpointBody) (store.Endpoint, error) {
	bad := func(field, message string) error {
		return httpx.Invalid("INVALID_ENDPOINT", message, map[string]any{"field": field})
	}
	e := store.Endpoint{Tool: tool, Kind: b.Kind, URL: strings.TrimSpace(b.URL), Risk: b.Risk, Idempotency: b.Idempotency}
	if e.Kind == "" {
		e.Kind = "http"
	}
	if e.Kind != "http" && e.Kind != "mcp" {
		return e, bad("kind", "kind must be http or mcp.")
	}
	if !slices.Contains(riskTiers, e.Risk) {
		return e, bad("risk", "risk must be one of "+strings.Join(riskTiers, ", ")+".")
	}
	e.TimeoutMs = DefaultTimeoutMs
	if b.TimeoutMs != nil {
		e.TimeoutMs = *b.TimeoutMs
	}
	if e.TimeoutMs < MinTimeoutMs || e.TimeoutMs > MaxTimeoutMs {
		return e, bad("timeout_ms", fmt.Sprintf("timeout_ms must be between %d and %d.", MinTimeoutMs, MaxTimeoutMs))
	}
	switch e.Idempotency {
	case "":
		// An irreversible action is exactly what a retry must not repeat.
		e.Idempotency = "optional"
		if e.Risk == "WRITE_IRREVERSIBLE" {
			e.Idempotency = "required"
		}
	case "required", "optional":
	default:
		return e, bad("idempotency", "idempotency must be required or optional.")
	}
	if len(b.ForwardHeaders) > MaxForwardHeaders {
		return e, bad("forward_headers", fmt.Sprintf("At most %d headers are forwarded.", MaxForwardHeaders))
	}
	e.ForwardHeaders = []string{}
	for i, h := range b.ForwardHeaders {
		if !headerNamePattern.MatchString(h) {
			return e, bad(fmt.Sprintf("forward_headers[%d]", i), "A header name is letters, digits and dashes.")
		}
		h = textproto.CanonicalMIMEHeaderKey(h)
		if slices.Contains(neverForwarded, h) {
			return e, bad(fmt.Sprintf("forward_headers[%d]", i), h+" is never forwarded to a tool.")
		}
		if !slices.Contains(e.ForwardHeaders, h) {
			e.ForwardHeaders = append(e.ForwardHeaders, h)
		}
	}
	slices.Sort(e.ForwardHeaders)
	if e.URL == "" || len(e.URL) > maxURL {
		return e, bad("url", fmt.Sprintf("url is required (at most %d characters).", maxURL))
	}
	u, err := url.Parse(e.URL)
	if err != nil || !u.IsAbs() || u.Opaque != "" || u.Fragment != "" {
		return e, bad("url", "url must be an absolute http(s) URL without a fragment.")
	}
	if err := egress.CheckURL(u); err != nil {
		return e, httpx.Invalid("EGRESS_NOT_ALLOWED",
			"The gateway may not call this URL: "+strings.TrimPrefix(err.Error(), netguard.ErrBlocked.Error()+": ")+
				". Add the host to RUNTIME_EGRESS_ALLOWED_HOSTS (or RUNTIME_EGRESS_PRIVATE_HOSTS for an internal service).",
			map[string]any{"field": "url"})
	}
	return e, nil
}

func (s *Server) listEndpoints(w http.ResponseWriter, r *http.Request) error {
	_, sc, err := projectOf(r, r.URL.Query().Get("project_id"), authn.PermRead)
	if err != nil {
		return err
	}
	list, err := s.Store.Endpoints(r.Context(), sc)
	if err != nil {
		return err
	}
	httpx.WriteJSON(w, http.StatusOK, httpx.Page[store.Endpoint]{Items: list})
	return nil
}

func (s *Server) getEndpoint(w http.ResponseWriter, r *http.Request) error {
	tool, err := pathTool(r)
	if err != nil {
		return err
	}
	_, sc, err := projectOf(r, r.URL.Query().Get("project_id"), authn.PermRead)
	if err != nil {
		return err
	}
	e, err := s.Store.Endpoint(r.Context(), s.Store.Pool, sc, tool)
	if err != nil {
		return found(err, "The tool endpoint")
	}
	httpx.WriteJSON(w, http.StatusOK, e)
	return nil
}

func (s *Server) putEndpoint(w http.ResponseWriter, r *http.Request) error {
	tool, err := pathTool(r)
	if err != nil {
		return err
	}
	var b endpointBody
	if err := httpx.DecodeJSON(w, r, &b, 64<<10); err != nil {
		return err
	}
	p, sc, err := projectOf(r, b.ProjectID, authn.PermPolicyWrite)
	if err != nil {
		return err
	}
	e, err := checkEndpoint(s.Egress, tool, b)
	if err != nil {
		return err
	}
	e.UpdatedBy = p.Actor
	var out store.Endpoint
	var created bool
	err = pgx.BeginFunc(r.Context(), s.Store.Pool, func(tx pgx.Tx) error {
		var err error
		out, created, err = s.Store.PutEndpoint(r.Context(), tx, sc, e, s.now())
		if err != nil {
			return err
		}
		action := "tool_endpoint.updated"
		if created {
			action = "tool_endpoint.registered"
		}
		return s.audit(r.Context(), tx, p, sc.ProjectID, action, "tool_endpoint", tool, "", endpointMeta(out))
	})
	if err != nil {
		return err
	}
	status := http.StatusOK
	if created {
		status = http.StatusCreated
	}
	httpx.WriteJSON(w, status, out)
	return nil
}

func (s *Server) deleteEndpoint(w http.ResponseWriter, r *http.Request) error {
	tool, err := pathTool(r)
	if err != nil {
		return err
	}
	p, sc, err := projectOf(r, r.URL.Query().Get("project_id"), authn.PermPolicyWrite)
	if err != nil {
		return err
	}
	err = pgx.BeginFunc(r.Context(), s.Store.Pool, func(tx pgx.Tx) error {
		e, err := s.Store.DeleteEndpoint(r.Context(), tx, sc, tool)
		if err != nil {
			return err
		}
		return s.audit(r.Context(), tx, p, sc.ProjectID, "tool_endpoint.removed", "tool_endpoint", tool, "", endpointMeta(e))
	})
	if errors.Is(err, store.ErrNotFound) {
		return notFound("The tool endpoint")
	}
	if err != nil {
		return err
	}
	w.WriteHeader(http.StatusNoContent)
	return nil
}

// endpointMeta is what the audit log keeps of an endpoint: its host, not
// its full URL (a query string may carry a key).
func endpointMeta(e store.Endpoint) map[string]any {
	host := ""
	if u, err := url.Parse(e.URL); err == nil {
		host = u.Host
	}
	return map[string]any{"kind": e.Kind, "host": host, "risk": e.Risk, "idempotency": e.Idempotency,
		"timeout_ms": e.TimeoutMs}
}

// forwardable lists the endpoint's forwarded headers present on a request.
func forwardable(e store.Endpoint, h http.Header) http.Header {
	out := http.Header{}
	for _, name := range e.ForwardHeaders {
		if v := h.Values(name); len(v) > 0 {
			out[name] = slices.Clone(v)
		}
	}
	return out
}
